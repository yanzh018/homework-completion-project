"""Check readable math spacing and source-aware expandable symbols."""
import os
import sys
import tempfile
from pathlib import Path
import cv2
import fitz
import numpy as np
from PIL import Image, ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-math-spacing-')
os.environ['HANDWRITING_DATA']=temporary.name
from app import app,TOKEN
from rendering import Composer,draw_ops,parse_document,shift,validate_settings
from storage import DATA,dumps,execute


def main():
    execute("INSERT INTO profiles(id,name) VALUES('p','字距测试')")
    for i,label in enumerate(['x','y','1','2','3','+','-','=','.', '(',')','√']):
        image=Image.new('RGBA',(18,26),(0,0,0,0));draw=ImageDraw.Draw(image)
        points=[(2,16),(5,14),(7,19),(10,2),(15,2)] if label=='√' else [(4,20),(8,3),(13,20)]
        draw.line(points,fill=(0,0,0,255),width=2)
        path=DATA/'glyphs'/f'{i}.png';image.save(path)
        metrics={'width':9,'height':13,'baseline':.9,'stroke_width':.7,'slant_degrees':0,'ink_bbox':[0,0,9,13]}
        execute("""INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",(str(i),'p',label,label,dumps([0,0,9,13]),str(path.relative_to(DATA)),dumps(metrics),'handwriting','accepted','extracted'))
    def render_math(text,settings=None):
        composer=Composer('p',0,validate_settings(settings))
        return composer.math(parse_document('$'+text+'$')['lines'][0]['runs'][0]['body'],16)
    def glyphs(box):return [op for op in box.ops if op['kind']=='glyph']
    def gap(a,b):return b.get('ink_rect',b['rect'])[0]-a.get('ink_rect',a['rect'])[2]
    ordinary=glyphs(render_math('xy'));binary=glyphs(render_math('x+y'));unary=glyphs(render_math('-x'))
    pair_gap=gap(ordinary[0],ordinary[1])
    binary_gap=gap(binary[0],binary[1])
    unary_gap=gap(unary[0],unary[1])
    assert binary_gap>pair_gap and unary_gap<binary_gap,(pair_gap,binary_gap,unary_gap)
    decimal=glyphs(render_math('1.2'))
    assert gap(decimal[0],decimal[1])<pair_gap
    assert gap(decimal[1],decimal[2])<pair_gap
    compact=render_math('xy',{'letter_spacing':.6});spacious=render_math('xy',{'letter_spacing':1.5})
    assert spacious.width>compact.width
    for text in ('x+y','x=y',r'\left(x+1\right)\left(y-1\right)'):
        assert render_math(text,{'letter_spacing':.6}).width<render_math(text,{'letter_spacing':1.5}).width,text
    adjacent=sorted((op for op in render_math('(x)(y)').ops if op.get('role')=='parenthesis'),key=lambda op:op['rect'][0])
    assert 0<adjacent[2]['rect'][0]-adjacent[1]['rect'][2]<16*.18
    radical=render_math(r'\sqrt[3]{x^2+1}')
    root=next(op for op in radical.ops if op.get('role')=='radical_head')
    bar=next(op for op in radical.ops if op.get('role')=='radical')
    assert root['label']=='√' and root['origin']=='extracted' and bar['points'][-1][0]>root['rect'][2]
    for text in (r'\sqrt{x}',r'\sqrt{\frac{x}{y}}',r'\sqrt[3]{x^2+1}'):
        box=render_math(text);head=next(op for op in box.ops if op.get('role')=='radical_head');line=next(op for op in box.ops if op.get('role')=='radical')
        doc=fitz.open();page=doc.new_page(width=200,height=150);draw_ops(page,shift(type(box)(ops=[head,line]),20,60))
        pix=page.get_pixmap(matrix=fitz.Matrix(5,5),alpha=False);mask=(np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3).min(axis=2)<150).astype(np.uint8)
        count,_,stats,_=cv2.connectedComponentsWithStats(mask,8);assert sum(row[4]>5 for row in stats[1:])==1,text
        doc.close()
    fenced=render_math(r'\left(\frac{x}{y}\right)')
    brackets=[op for op in fenced.ops if op.get('role')=='parenthesis']
    fraction=next(op for op in fenced.ops if op.get('role')=='fraction')
    assert len(brackets)==2 and all(op['kind']=='glyph' and op['origin']=='extracted' for op in brackets)
    assert brackets[0]['rect'][2]<fraction['points'][0][0]<fraction['points'][-1][0]<brackets[1]['rect'][0]
    assert brackets[0]['rect'][3]-brackets[0]['rect'][1]>16
    fallback=Composer('empty',0,validate_settings({})).math(parse_document(r'$\sqrt{x}$')['lines'][0]['runs'][0]['body'],16)
    assert any(op.get('role')=='radical' and op['kind']=='line' for op in fallback.ops)
    assert not any(op.get('label')=='√' for op in fallback.ops)
    image=Image.new('RGBA',(42,25),(0,0,0,0));draw=ImageDraw.Draw(image)
    draw.line([(1,20),(5,18),(8,23),(14,2),(40,4)],fill=(0,0,0,255),width=2)
    path=DATA/'glyphs'/'full-root.png';image.save(path)
    metrics={'width':20,'height':12,'baseline':.9,'stroke_width':.7,'slant_degrees':0,
             'ink_bbox':[0,0,20,12],'radical_slices':{'head':[0,0,16,25],'bar':[14,1,41,6],'anchor_y':.1}}
    execute("""INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",('full-root','p','√','√',dumps([0,0,20,12]),str(path.relative_to(DATA)),dumps(metrics),'handwriting','accepted','extracted'))
    for text in (r'\sqrt{x}',r'\sqrt{\frac{x}{y}}'):
        box=render_math(text);head=next(o for o in box.ops if o.get('role')=='radical_head');bar=next(o for o in box.ops if o.get('role')=='radical')
        assert head['glyph_id']=='full-root' and bar['kind']=='stroke_image' and bar['origin']=='extracted'
        doc=fitz.open();page=doc.new_page(width=200,height=150);draw_ops(page,shift(type(box)(ops=[head,bar]),20,60))
        pix=page.get_pixmap(matrix=fitz.Matrix(5,5),alpha=False);mask=(np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3).min(axis=2)<150).astype(np.uint8)
        _,_,stats,_=cv2.connectedComponentsWithStats(mask,8);assert sum(row[4]>5 for row in stats[1:])==1,text
        doc.close()
    with app.test_client() as client:
        headers={'X-Local-Token':TOKEN}
        a=client.post('/api/assignments',json={'profile_id':'p'},headers=headers).json
        a['settings']['letter_spacing']=1.35
        saved=client.put('/api/assignments/'+a['id'],json=a,headers=headers)
        assert saved.status_code==200 and saved.json['settings']['letter_spacing']==1.35
        a=saved.json;a['settings']['letter_spacing']=2.5
        assert client.put('/api/assignments/'+a['id'],json=a,headers=headers).status_code==400
    print('PASS relation, binary, unary and adjustable letter spacing')
    print('PASS personal radical and parentheses stretch with formula; structural fallback')
    print('PASS letter spacing persists and invalid values are rejected')

if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
