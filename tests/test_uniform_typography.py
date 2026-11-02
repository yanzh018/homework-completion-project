"""Mixed source sizes should not create random body heights or baselines."""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-uniform-')
os.environ['HANDWRITING_DATA']=temporary.name
from rendering import Composer,_glyph_alpha,validate_settings
from storage import DATA,dumps,execute,init


def add(label,index,height,baseline=.96):
    path=DATA/'glyphs'/f'{label}-{index}.png'
    image=Image.new('RGBA',(60,150),(0,0,0,0));d=ImageDraw.Draw(image)
    d.line((20,10,20,10+height),fill='black',width=5)
    d.line((20,10,38,10),fill='black',width=5)
    image.save(path)
    metrics={'width':20,'height':height/2,'baseline':baseline,'stroke_width':.8,
             'slant_degrees':0,'crop_safety':{'clipped':False}}
    execute('INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (label+str(index),'p',label,label,dumps([0,0,20,height/2]),str(path.relative_to(DATA)),dumps(metrics),'handwriting','accepted','extracted'))
    return path


def main():
    init();execute("INSERT INTO profiles(id,name) VALUES('p','typography')")
    for label in 'ace':add(label,0,35)
    for label,height in [('d',90),('h',50),('l',65),('0',35),('1',70),('2',45),('3',90)]:add(label,0,height)
    add('g',0,65,.76)
    add('the',0,65)
    add('as',0,85);add('012',0,90)
    for i,baseline in enumerate((.55,.90,.93,.96)):add('f',i,80,baseline)
    c=Composer('p',0,validate_settings({}));size=16
    def h(label):
        r=c.glyph(label,size).ops[0]['ink_rect'];return r[3]-r[1]
    digits=[h(x) for x in '0123'];ascenders=[h(x) for x in 'dhl']
    assert max(digits)-min(digits)<.7,digits
    assert max(ascenders)-min(ascenders)<.7,ascenders
    def word_height(label):
        r=c.glyph(label,size,word=True).ops[0]['ink_rect'];return r[3]-r[1]
    assert abs(word_height('as')-word_height('a'))<.7,'a short real ligature should match neighboring short letters'
    assert abs(h('012')-h('0'))<.7,'a real digit sequence should match individual digits'
    assert c.word_size_profile()['f_baseline']>.85,'short and non-descending f samples must participate in the median'
    assert c.glyph('f',size).descent<size*.4,'one long source crop must not inflate every f'
    a=c.glyph('a',size,word=True);g=c.glyph('g',size,word=True)
    assert abs(a.ascent-g.ascent)<1.0,(a.ascent,g.ascent)
    assert g.descent>0,'normalizing the body must preserve a real descender'
    def visible_bottom(box):
        op=box.ops[0];alpha=_glyph_alpha(op,1);ys=np.where(alpha>100)[0];r=op['rect']
        return r[1]+float(np.quantile(ys,.95))/max(1,alpha.shape[0]-1)*(r[3]-r[1])
    assert abs(visible_bottom(a)-visible_bottom(c.glyph('the',size,word=True)))<size*.08,\
        'a real multi-letter word and neighboring letters should share a visible baseline'
    source=DATA/'glyphs'/'a-0.png';before=hashlib.sha256(source.read_bytes()).hexdigest()
    assert a.ops[0]['origin']=='extracted' and a.ops[0].get('padding',0)>0
    assert not Composer('p',0,validate_settings({'uniform_stroke':False})).glyph('a',size).ops[0].get('padding')
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    print('PASS mixed-source digit and ascender sizing, descender baselines, and non-destructive real-ink padding')


if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
