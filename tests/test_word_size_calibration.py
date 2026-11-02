"""Calibrate word letter proportions from each profile's real glyphs."""
import json
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-word-sizes-')
os.environ['HANDWRITING_DATA']=temporary.name
from rendering import Composer,_glyphs_overlap,validate_settings
from storage import DATA,dumps,execute,init,one


def add(profile,label,height,baseline=.96):
    image=Image.new('RGBA',(80,90),(0,0,0,0));ImageDraw.Draw(image).rectangle((12,8,25,7+height),fill=(0,0,0,255))
    path=DATA/'glyphs'/f'{profile}-{label}.png';image.save(path)
    metrics={'width':7,'height':height/2,'baseline':baseline,'stroke_width':.8,'slant_degrees':0,'ink_bbox':[0,0,7,height/2]}
    execute("INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (profile+'-'+label,profile,label,label,dumps([0,0,7,height/2]),str(path.relative_to(DATA)),dumps(metrics),'handwriting','accepted','extracted'))


def main():
    init()
    for profile,short in [('small',20),('large',32)]:
        execute('INSERT INTO profiles(id,name) VALUES(?,?)',(profile,profile))
        for label in 'une':add(profile,label,short)
        for label in 'dl':add(profile,label,40)
        add(profile,'i',32);add(profile,'f',64,.6)
    small=Composer('small',0,validate_settings({}));large=Composer('large',0,validate_settings({}))
    assert abs(small.word_size_profile()['x_height']-.5)<.01
    assert abs(large.word_size_profile()['x_height']-.8)<.01
    for c in (small,large):
        ops=c.text_word('undefined',16).ops
        by_label={o['label']:o for o in ops};height=lambda o:o['ink_rect'][3]-o['ink_rect'][1]
        assert abs(height(by_label['u'])/height(by_label['d'])-c.word_size_profile()['x_height'])<.01
        assert by_label['f']['ink_rect'][3]>16*.5,'a real descender baseline must survive'
        assert all(not _glyphs_overlap(a,b,1) for i,a in enumerate(ops) for b in ops[i+1:])
    assert small.glyph('u',16).ops[0]['ink_rect'][3]-small.glyph('u',16).ops[0]['ink_rect'][1]>16*.78
    execute("INSERT INTO profiles(id,name) VALUES('sparse','sparse')");add('sparse','u',20);add('sparse','d',40)
    fallback=Composer('sparse',0,validate_settings({})).word_size_profile()
    assert not fallback['calibrated'] and fallback['f_baseline'] is None
    for label in 'bacs':add('small',label,32)
    add('small','because',40)
    c=Composer('small',0,validate_settings({}))
    assert 'because' not in c.library
    assert ''.join(op['label'] for op in c.text_word('because',16).ops)=='because'
    metric=json.loads(one("SELECT metrics FROM glyphs WHERE id='small-because'")['metrics']);metric['width']=60
    execute("UPDATE glyphs SET metrics=? WHERE id='small-because'",(dumps(metric),))
    c=Composer('small',0,validate_settings({}))
    assert ''.join(op['label'] for op in c.text_word('because',16).ops)=='because'
    assert all(len(op['label'])==1 for op in c.text_word('because',16).ops)
    print('PASS profile-specific word proportions, f descender and sparse-sample fallback')


if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
