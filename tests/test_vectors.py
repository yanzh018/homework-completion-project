"""Vector arrows stay above the vector and survive scripts and matrix layout."""
import os
import sys
import tempfile
from pathlib import Path

import fitz
from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-vectors-')
os.environ['HANDWRITING_DATA']=temporary.name
from app import app,TOKEN
from rendering import Composer,draw_ops,parse_document,shift,validate_settings
from storage import DATA,dumps,execute


def main():
    execute("INSERT INTO profiles(id,name) VALUES('p','向量测试')")
    for i,label in enumerate('uvbT12'):
        im=Image.new('RGBA',(18,26),(0,0,0,0));ImageDraw.Draw(im).line([(3,19),(8,3),(13,19)],fill=(0,0,0,255),width=2)
        path=DATA/'glyphs'/f'{i}.png';im.save(path)
        metrics={'width':9,'height':13,'baseline':.9,'stroke_width':.7,'slant_degrees':0,'ink_bbox':[0,0,9,13]}
        execute("INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(i),'p',label,label,dumps([0,0,9,13]),str(path.relative_to(DATA)),dumps(metrics),'handwriting','accepted','extracted'))
    def box(formula):
        ast=parse_document('$'+formula+'$');return Composer('p',0,validate_settings({'font_size':16})).math(ast['lines'][0]['runs'][0]['body'],16)
    for source in (r'\vec{u}',r'\vec{u}^{T}',r'\vec{b}_1',r'\overrightarrow{uv}'):
        b=box(source);shaft=next(o for o in b.ops if o.get('role')=='vector_arrow');head=next(o for o in b.ops if o.get('role')=='vector_arrowhead')
        first=next(o for o in b.ops if o.get('kind')=='glyph')
        assert shaft['points'][0][0]<shaft['points'][1][0] and head['points'][1]==shaft['points'][1]
        assert shaft['points'][0][1]<first['rect'][1] and b.ascent>=-head['points'][0][1]
        doc=fitz.open();page=doc.new_page(width=200,height=100);draw_ops(page,shift(b,20,50));assert page.get_pixmap().width==200;doc.close()
    b=box(r'\vec{u}^{T}\vec{v}=\vec{v}^{T}\vec{u}')
    assert sum(o.get('role')=='vector_arrow' for o in b.ops)==4
    for i,label in enumerate(('→','[',']')):
        image=Image.new('RGBA',(30,24),(0,0,0,0));draw=ImageDraw.Draw(image)
        points={'→':[(2,12),(26,12),(20,8),(26,12),(20,16)],
                '[':[(18,2),(6,2),(5,21),(18,21)],']':[(5,2),(17,2),(18,21),(5,21)]}[label]
        draw.line(points,fill=(0,0,0,255),width=2)
        path=DATA/'glyphs'/f'symbol-{i}.png';image.save(path)
        metrics={'width':15,'height':12,'baseline':.9,'stroke_width':.7,'slant_degrees':0,'ink_bbox':[0,0,15,12]}
        execute("INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ('symbol-'+str(i),'p',label,label,dumps([0,0,15,12]),str(path.relative_to(DATA)),dumps(metrics),'handwriting','accepted','extracted'))
    b=box(r'\vec{u}^{T}\vec{v}')
    arrows=[o for o in b.ops if o.get('role')=='vector_arrow']
    assert len(arrows)==2 and all(o['kind']=='glyph' and o['origin']=='extracted' and o['label']=='→' for o in arrows)
    bases=[o for o in b.ops if o.get('label') in ('u','v')]
    assert len(bases)==2 and all(a['rect'][3]<g['rect'][1] for a,g in zip(arrows,bases))
    matrix=box(r'\begin{bmatrix}1&2\\2&1\end{bmatrix}')
    brackets=[o for o in matrix.ops if o.get('role')=='bracket']
    assert len(brackets)==6 and all(o['kind']=='stroke_image' and o['origin']=='extracted' for o in brackets)
    assert [o['bracket_part'] for o in brackets]==['top','middle','bottom']*2
    doc=fitz.open();page=doc.new_page(width=200,height=100);draw_ops(page,shift(matrix,20,50));assert page.get_pixmap().width==200;doc.close()
    with app.test_client() as client:
        response=client.post('/api/format-preview',json={'texts':[r'$\vec{u}$']},headers={'X-Local-Token':TOKEN})
        assert response.status_code==200 and response.json[0]['ast']['lines'][0]['runs'][0]['body']['children'][0]['type']=='vector'
    print('PASS fallback and real handwritten arrows, real matrix brackets, and scripts')

if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
