"""Keep real E distinct from membership and disclose source-derived glyphs."""
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-derived-')
os.environ['HANDWRITING_DATA']=temporary.name

from extraction import _separate_marks_safe, glyph_signature, glyph_verified
from rendering import Composer, parse_document, validate_settings
from storage import DATA, dumps, execute, glyph_record, init, one


def add(gid,label,origin,draw,**extra):
    path=DATA/'glyphs'/f'{gid}.png'
    image=Image.new('RGBA',(60,80),(0,0,0,0));draw(ImageDraw.Draw(image));image.save(path)
    metrics={'width':30,'height':40,'baseline':.96,'stroke_width':.8,'slant_degrees':0,
             'crop_safety':{'clipped':False},**extra}
    execute('INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (gid,'p',label,label,dumps([0,0,30,40]),str(path.relative_to(DATA)),dumps(metrics),
             'handwriting' if origin=='extracted' else 'inferred','accepted',origin))
    return path


def main():
    init();execute("INSERT INTO profiles(id,name) VALUES('p','test')")
    def E(d):
        d.line((6,5,51,5),fill='black',width=4);d.line((6,5,6,72),fill='black',width=4)
        d.line((17,42,47,42),fill='black',width=4);d.line((17,71,47,71),fill='black',width=4)
    e=add('real-e','E','extracted',E)
    assert _separate_marks_safe(str(e),'E',e.stat().st_mtime_ns)
    def unrelated(d):
        d.line((6,5,30,5),fill='black',width=4);d.line((6,5,6,72),fill='black',width=4)
        d.ellipse((48,35,56,43),fill='black');d.line((17,71,47,71),fill='black',width=4)
    bad=add('unsafe-e','E','extracted',unrelated)
    assert not _separate_marks_safe(str(bad),'E',bad.stat().st_mtime_ns)
    add('real-member','∈','extracted',lambda d:d.arc((7,12,50,68),90,270,fill='black',width=4))
    add('generic-m','M','inferred',lambda d:d.line((6,70,15,8,30,60,45,8,54,70),fill='black',width=4))
    generic=glyph_record(one("SELECT * FROM glyphs WHERE id='generic-m'"));generic['metrics']['automatic_verification']={'accepted':True,'signature':glyph_signature(generic)}
    execute('UPDATE glyphs SET metrics=? WHERE id=?',(dumps(generic['metrics']),generic['id']))
    add('derived-m','M','inferred',lambda d:d.line((6,70,15,15,30,50,45,15,54,70),fill='black',width=4),
        source_type='derived_from_real',preferred_derived=True)
    add('g-contaminated','g','extracted',lambda d:d.line((5,10,45,70),fill='black',width=4))
    execute("UPDATE glyphs SET status='rejected' WHERE id='g-contaminated'")
    add('g-clean','g','extracted',lambda d:d.ellipse((12,9,42,62),outline='black',width=4))
    for label in (',','⇒'):
        add('real-'+label,label,'extracted',lambda d:d.line((8,20,45,50),fill='black',width=4))
    c=Composer('p',0,validate_settings({}))
    ops,issues=c.layout(parse_document(r'$E(x),\;x\in D$'),[0,0,500,160],16)
    labels={op.get('label'):op.get('glyph_id') for op in ops if op.get('label') in ('E','∈')}
    assert labels=={'E':'real-e','∈':'real-member'},labels
    assert not issues
    m=c.glyph('M',16).ops[0]
    assert m['glyph_id']=='derived-m' and m['origin']=='inferred'
    assert 'M' in c.derived and 'M' not in c.inferred
    assert not glyph_verified(glyph_record(one("SELECT * FROM glyphs WHERE id='derived-m'")))
    assert c.glyph('g',16).ops[0]['glyph_id']=='g-clean','excluded neighboring ink must not return to rendering'
    comma=c.glyph(',',16).ops[0]
    assert comma['rect'][3]>0,'a comma tail must sit below the baseline'
    assert c.glyph('⇒',16).ops[0]['glyph_id']=='real-⇒'
    print('PASS distinct E/membership, separated-E safety, source-derived preference and review')


if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
