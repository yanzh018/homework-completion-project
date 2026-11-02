"""Keep letters within a word closer than separate words, without touching ink."""
import os
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-word-spacing-')
os.environ['HANDWRITING_DATA']=temporary.name
from rendering import Composer,parse_document,validate_settings
from storage import init


def main():
    init();size=16
    def compose(settings):return Composer('empty',0,validate_settings(settings))
    c=compose({});ops,issues=c.layout(parse_document('any let undefined'),[0,0,500,100],size)
    assert not issues
    letters=[o for o in ops if o.get('label')]
    gap=lambda a,b:b.get('ink_rect',b['rect'])[0]-a.get('ink_rect',a['rect'])[2]
    within=gap(letters[0],letters[1]);between=gap(letters[2],letters[3])
    assert 0<within<between,(within,between)
    assert abs(within-c.gap*size*.45)<.01
    assert abs(between-c.word_gap*size)<.01
    for left,right in zip(letters[6:],letters[7:]):assert gap(left,right)>0
    compact=compose({'letter_spacing':.6}).text_word('undefined',size)
    spacious=compose({'letter_spacing':1.5}).text_word('undefined',size)
    assert compact.width<spacious.width
    small=compose({'word_spacing':.5});large=compose({'word_spacing':1.2})
    assert small.word_gap<large.word_gap
    print('PASS letters stay closer within words; words remain distinct and spacing settings work')

if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
