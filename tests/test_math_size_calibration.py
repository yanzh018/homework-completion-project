"""Use same-line real samples for mathematical variable size, with a safe fallback."""
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-math-sizes-')
os.environ['HANDWRITING_DATA']=temporary.name
from rendering import Composer,validate_settings
from storage import DATA,dumps,execute,init


def add(profile,document,page,label,index,height,ink_box,origin='extracted'):
    image=Image.new('RGBA',(48,80),(0,0,0,0))
    ImageDraw.Draw(image).rectangle((8,5,20,4+height),fill=(0,0,0,255))
    identifier=f'{profile}-{label}-{index}'
    path=DATA/'glyphs'/f'{identifier}.png';image.save(path)
    metrics={'height':height/2,'baseline':.96,'stroke_width':.8,'slant_degrees':0,'image_ink_bbox':ink_box}
    execute("INSERT INTO glyphs(id,profile_id,document_id,page,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (identifier,profile,document,page,label,label,dumps(ink_box),str(path.relative_to(DATA)),dumps(metrics),'handwriting','accepted',origin))


def main():
    init()
    for profile,variable_height in [('small',8),('large',9)]:
        execute('INSERT INTO profiles(id,name) VALUES(?,?)',(profile,profile))
        execute("INSERT INTO documents(id,profile_id,name,kind,original,sha256,status) VALUES(?,?,?,'sample','test.pdf','test','ready')",(profile+'-doc',profile,profile))
        for page in range(3):
            execute('INSERT INTO pages(document_id,number,width,height,meta) VALUES(?,?,100,100,?)',(profile+'-doc',page,'{}'))
            add(profile,profile+'-doc',page,'x',page,variable_height*2,[10,30-variable_height,15,30])
            add(profile,profile+'-doc',page,'2',page,20,[18,20,23,30])
        # A digit on a different baseline must never be paired, nor may an inferred one.
        add(profile,profile+'-doc',0,'3','other-line',20,[15,50,20,60])
        add(profile,profile+'-doc',0,'4','inferred',20,[15,20,18,30],origin='inferred')
        c=Composer(profile,0,validate_settings({}))
        measured=c.word_size_profile()
        assert measured['math_pairs']==3
        assert abs(measured['math_x_height']-variable_height/10)<.01
        x=c.glyph('x',12);digit=c.glyph('2',12)
        height=lambda box:box.ops[0]['ink_rect'][3]-box.ops[0]['ink_rect'][1]
        assert abs(height(x)/height(digit)-variable_height/10)<.01
        assert abs(height(c.glyph('x',12,word=True))/height(digit)-.72)<.01
    execute("INSERT INTO profiles(id,name) VALUES('sparse','sparse')")
    c=Composer('sparse',0,validate_settings({}))
    assert c.word_size_profile()['math_pairs']==0 and c.word_size_profile()['math_x_height']==.86
    print('PASS same-line math ratios per profile, inferred exclusion and sparse fallback')


if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
