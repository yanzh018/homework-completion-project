"""Prefer a plausible real word to inferred pieces without bypassing review."""
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-real-words-')
os.environ['HANDWRITING_DATA']=temporary.name
from extraction import glyph_verified
from rendering import Composer,validate_settings
from storage import DATA,dumps,execute,glyph_record,init,one


def add(label,origin,width=20,height=30,clipped=False):
    name=label+'-'+origin
    image=Image.new('RGBA',(width,height),(0,0,0,0));draw=ImageDraw.Draw(image)
    if label=='point':
        baseline=height//2-2
        draw.line((2,baseline,width-3,baseline),fill=(0,0,0,255),width=2)
        draw.line((2,baseline,2,height-2),fill=(0,0,0,255),width=2)
        draw.line((width-5,1,width-5,baseline),fill=(0,0,0,255),width=2)
    else:draw.line((1,height-2,width-2,1),fill=(0,0,0,255),width=2)
    path=DATA/'glyphs'/f'{name}.png';image.save(path)
    metrics={'width':width/2,'height':height/2,'baseline':.96,'stroke_width':.8,'slant_degrees':0,
             'crop_safety':{'clipped':clipped}}
    execute('INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (name,'p',label,label,dumps([0,0,width/2,height/2]),str(path.relative_to(DATA)),dumps(metrics),
             'handwriting' if origin=='extracted' else 'inferred','accepted',origin))


def main():
    init();execute("INSERT INTO profiles(id,name) VALUES('p','test')")
    for label in 'oinde':add(label,'extracted')
    for label in 'pth':add(label,'inferred')
    add('point','extracted',100,30)
    add('Ada.Smith','extracted',100,30)
    add('thin','extracted',15,40)
    add('note','extracted',100,30,clipped=True)
    c=Composer('p',0,validate_settings({}))
    word_box=c.text_word('point',16);word=word_box.ops
    assert len(word)==1 and word[0]['label']=='point' and word[0]['origin']=='extracted'
    assert word_box.descent>16*.35,'a real word descender must remain below the writing baseline'
    assert not glyph_verified(glyph_record(one("SELECT * FROM glyphs WHERE id='point-extracted'"))), 'selection must not invent independent verification'
    assert all(op.get('label')!='point' for op in c.text_word('endpoint',16).ops),'do not substitute a full word inside another word'
    name=c.text_word('Ada.Smith',16)
    assert len(name.ops)==1 and name.ops[0]['label']=='Ada.Smith' and name.ops[0]['origin']=='extracted'
    assert name.ascent>=15.9,'a dotted handwritten name must use text height rather than punctuation height'
    assert 'thin' not in c.library,'a suspiciously narrow word must not replace available characters'
    assert 'note' not in c.library,'a clipped word must remain excluded'
    assert any(op.get('origin')=='inferred' for op in c.text_word('thin',16).ops)
    add('p','extracted');add('t','extracted')
    c=Composer('p',0,validate_settings({}))
    assert all(op.get('origin')=='extracted' and len(op.get('label',''))==1 for op in c.text_word('point',16).ops),\
        'an unusually deep word crop can use complete real letters to restore its baseline'
    assert one("SELECT status FROM glyphs WHERE id='point-extracted'")['status']=='accepted',\
        'choosing a different rendering must not delete the original ligature'
    print('PASS real complete word preference, boundaries, unsafe-word exclusion and independent review')


if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
