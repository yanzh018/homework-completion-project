"""Check the saved missing-glyph choices and size boundaries in an isolated library."""
import os
import sys
import tempfile
from pathlib import Path

import fitz
from PIL import Image, ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-style-options-')
os.environ['HANDWRITING_DATA']=temporary.name

import app as server
from rendering import fitted_font_size,validate_settings
from extraction import glyph_signature
from storage import DATA,dumps,execute,glyph_record,one


def main():
    client=server.app.test_client()
    headers={'X-Local-Token':server.TOKEN}
    profile=client.post('/api/profiles',json={'name':'隔离样式测试'},headers=headers).json['id']
    def glyph(label,origin):
        path=DATA/'glyphs'/f'{label}-{origin}.png'
        image=Image.new('RGBA',(22,30),(0,0,0,0))
        ImageDraw.Draw(image).line([(4,25),(11,5),(18,25)],fill=(0,0,0,255),width=3)
        image.save(path)
        metrics={'width':11,'height':15,'baseline':.8,'stroke_width':.8,'slant_degrees':0}
        execute('''INSERT INTO glyphs(id,profile_id,label,bbox,image,metrics,classification,status,origin)
                   VALUES(?,?,?,?,?,?,?,?,?)''',
                (f'{label}-{origin}',profile,label,dumps([0,0,11,15]),str(path.relative_to(DATA)),
                 dumps(metrics),'handwriting' if origin=='extracted' else 'inferred','accepted',origin))
    glyph('a','extracted');glyph('a','inferred');glyph('z','inferred')
    assignment=client.post('/api/assignments',json={'profile_id':profile},headers=headers).json
    assignment['answers'][0].update(text='$a$\n$z$',reviewed=True)
    def render():
        nonlocal assignment
        saved=client.put('/api/assignments/'+assignment['id'],json=assignment,headers=headers)
        assert saved.status_code==200,saved.json
        assignment=saved.json
        response=client.post('/api/assignments/'+assignment['id']+'/render',json={},headers=headers)
        assert response.status_code==200,response.json
        return response.json

    result=render()
    assert len(result['glyph_checks'])==2 and not result.get('pdf_url'),'unverified used glyphs must block export'
    for item in result['glyph_checks']:
        g=glyph_record(one('SELECT * FROM glyphs WHERE id=?',(item['id'],)))
        g['metrics']['automatic_verification']={'signature':glyph_signature(g),'accepted':True,'image_checked':True,'event':{'model':'mock'}}
        execute('UPDATE glyphs SET metrics=? WHERE id=?',(dumps(g['metrics']),g['id']))
    result=render()
    assert not result['missing'] and result.get('pdf_url'),result['errors']
    origins={op['label']:op['origin'] for op in result['regions'][0]['placements'] if op['kind']=='glyph'}
    assert origins=={'a':'extracted','z':'inferred'},origins
    assert any('AI 推测' in warning for warning in result['warnings'])

    assignment['settings']['allow_inferred']=False
    result=render()
    assert result['missing']==['z'] and not result.get('pdf_url')
    assert any('缺少可用字形：z' in error for error in result['errors'])

    assignment['settings']['missing_policy']='reference'
    result=render()
    assert result['missing']==['z'] and not result.get('pdf_url'),result['errors']
    assert any(op['kind']=='fallback' and op['label']=='z' for op in result['regions'][0]['placements'])
    assert any('灰色印刷参考字' in warning for warning in result['warnings'])

    assignment['settings']['allow_inferred']=True
    assignment['answers'][0]['text']='$a$\n$z$\n$q$'
    result=render()
    assert result['missing']==['q'] and not result.get('pdf_url'),result['errors']
    assert any(op.get('origin')=='inferred' and op['label']=='z' for op in result['regions'][0]['placements'])
    assignment['settings']['missing_policy']='strict'
    result=render()
    assert result['missing']==['q'] and not result.get('pdf_url')

    assignment['settings']['missing_policy']='reference'
    assignment['answers'][0]['text']=r'$a$'+'\n'+r'$\alpha$'
    result=render()
    assert result['missing']==['α'] and not result.get('pdf_url'),result['errors']
    assert any('输出为问号：α' in warning for warning in result['warnings'])
    assert any(op['kind']=='fallback' and op['label']=='α' for op in result['regions'][0]['placements'])

    doc=fitz.open();doc.new_page()
    answer={'source':'ai','text':'\n'.join(['$a$']*10),'page':0,'bbox':[50,65,545,100]}
    for size in (10,12):
        settings=validate_settings({'font_size':size,'auto_fit':True})
        assert fitted_font_size(profile,[answer],doc,settings)==size
    doc.close()
    print('PASS real glyphs take precedence; inferred glyphs and both missing policies work as described')
    print('PASS automatic fitting does not increase or shrink a 10 or 12 pt setting')


if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
