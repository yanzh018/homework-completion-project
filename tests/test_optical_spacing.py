"""Tighten handwriting by actual ink contours while keeping a stable visual baseline."""
import io
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-optical-spacing-')
os.environ['HANDWRITING_DATA']=temporary.name
from rendering import Composer,_glyphs_overlap,_safe_math_gap,image_stream,parse_document,shift,validate_settings
from storage import DATA,dumps,execute,init


def main():
    init();execute("INSERT INTO profiles(id,name) VALUES('p','光学字距测试')")
    for label,points in [('a',[(6,70),(60,10)]),('n',[(10,70),(64,10)])]:
        im=Image.new('RGBA',(80,80),(0,0,0,0));ImageDraw.Draw(im).line(points,fill=(0,0,0,255),width=5)
        path=DATA/'glyphs'/f'{label}.png';im.save(path)
        metrics={'width':40,'height':40,'baseline':.95,'stroke_width':1,'slant_degrees':0,'ink_bbox':[0,0,40,40]}
        execute("INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (label,'p',label,label,dumps([0,0,40,40]),str(path.relative_to(DATA)),dumps(metrics),'handwriting','accepted','extracted'))
    c=Composer('p',0,validate_settings({}));word=c.text_word('an',16);a,n=word.ops
    assert n['ink_rect'][0]-a['ink_rect'][2]<0,'diagonal strokes should be allowed to use empty side space'
    assert not _glyphs_overlap(a,n,1),'letter ink must stay separate'
    assert _glyphs_overlap(a,a,1),'the overlap check must detect actual contact'
    compact=Composer('p',0,validate_settings({'letter_spacing':.6})).text_word('an',16)
    spacious=Composer('p',0,validate_settings({'letter_spacing':1.5})).text_word('an',16)
    assert compact.width<spacious.width,'letter spacing must remain adjustable for real glyphs'
    def visual_bottom(op):
        alpha=np.asarray(Image.open(io.BytesIO(image_stream(op))).convert('RGBA'))[:,:,3]
        ys=np.where(alpha>100)[0];r=op['rect']
        return r[1]+np.quantile(ys,.95)/max(1,alpha.shape[0]-1)*(r[3]-r[1])
    assert abs(visual_bottom(a)-visual_bottom(n))<16*.05
    lines,issues=c.layout(parse_document('an an'),[0,0,300,80],16)
    assert not issues
    assert lines[2]['ink_rect'][0]-lines[1]['ink_rect'][2]>c.gap*16
    single=Composer('p',0,validate_settings({}));box=single.text_word('an',16)
    needed=max(box.ascent,16*.85)+max(box.descent,16*.15)
    _,issues=Composer('p',0,validate_settings({})).layout(parse_document('an'),[0,0,300,needed],16)
    assert not any('高度超出' in issue for issue in issues),'the unused gap after the last line must not count as ink'
    _,issues=Composer('p',0,validate_settings({})).layout(parse_document('an'),[0,0,300,needed-1],16)
    assert any('高度超出' in issue for issue in issues),'actual content overflow must still be reported'
    # Actual touching in a math expression is opened automatically; an
    # overlapping transparent box alone must not change the spacing.
    for label in 'st':
        im=Image.new('RGBA',(50,60),(0,0,0,0));ImageDraw.Draw(im).rectangle((15,5,25,50),fill=(0,0,0,255))
        path=DATA/'glyphs'/f'{label}.png';im.save(path)
        metrics={'width':25,'height':30,'baseline':.95,'stroke_width':1,'slant_degrees':0,'ink_bbox':[0,0,25,30]}
        execute("INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (label,'p',label,label,dumps([0,0,25,30]),str(path.relative_to(DATA)),dumps(metrics),'handwriting','accepted','extracted'))
    math=Composer('p',0,validate_settings({}));math.gap=-.08
    left=math.glyph('s',16);right=math.glyph('t',16);initial=math.gap*16
    assert _glyphs_overlap(left.ops[0],shift(right,left.width+initial)[0],1)
    adjusted=_safe_math_gap(left,right,initial,16,1)
    assert adjusted>initial and not _glyphs_overlap(left.ops[0],shift(right,left.width+adjusted)[0],1)
    for offset in (.025,.075,.125,.175,.225,.25):
        assert not _glyphs_overlap(shift(left,offset)[0],shift(right,left.width+adjusted+offset)[0],1),\
            'subpixel page position must not introduce a collision'
    result=math.math(parse_document('$st$')['lines'][0]['runs'][0]['body'],16)
    assert [op['label'] for op in result.ops]==['s','t'] and not _glyphs_overlap(*result.ops,1)
    assert _safe_math_gap(left,right,2,16,1)==2
    print('PASS optical word tightening, ink separation, word boundaries and visual baseline')
    print('PASS touching math glyphs automatically gain space without changing their content')


if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
