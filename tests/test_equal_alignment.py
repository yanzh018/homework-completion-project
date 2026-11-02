"""Only an explicit leading equals continues the preceding equation."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-equal-alignment-')
os.environ['HANDWRITING_DATA']=temporary.name
from rendering import Composer,parse_document,validate_settings
from storage import init


def main():
    init();settings=validate_settings({'font_size':16})
    def layout(text,width=500):
        ast=parse_document(text);before=json.dumps(ast,ensure_ascii=False)
        composer=Composer('empty',0,settings);ops,issues=composer.layout(ast,[0,0,width,500],16)
        assert json.dumps(ast,ensure_ascii=False)==before
        return ops,issues
    def equals(ops):return [o['rect'][0] for o in ops if o.get('label')=='=']
    # Only the last two rows form a continuation; do not shift the first equation.
    ops,issues=layout('$y=1$\n$long=1+1$\n$=2$')
    positions=equals(ops)
    assert not issues and len(positions)==3 and positions[0]!=positions[1] and abs(positions[1]-positions[2])<.01
    ops,issues=layout('x = 1\nlong = 1 + 1\n= 2')
    positions=equals(ops)
    assert not issues and positions[0]!=positions[1] and abs(positions[1]-positions[2])<.01
    # The original example: one derivative followed by a different equation
    # and its rearranged result should not be grouped as a single calculation.
    ops,issues=layout("$(4,1):\\quad y'=-\\frac{2}{17}$\n$y-1=-\\frac{2}{17}(x-4)$\n$2x+17y=25$")
    positions=equals(ops)
    assert not issues and len({round(x,2) for x in positions})==3
    source="$u=x^3,\\quad u'=3x^2$\n$y'=\\frac{u'}{\\sqrt{1-u^2}}$\n$=\\frac{3x^2}{\\sqrt{1-x^6}},\\quad -1<x<1$"
    ops,issues=layout(source);positions=equals(ops)
    assert not issues and len(positions)==4 and abs(positions[2]-positions[3])<.01 and positions[0]!=positions[2]
    ops,_=layout('$\\frac{a=b}{c}=1+1$\n$=2$')
    positions=equals(ops)
    assert len(positions)==3 and positions[0]!=positions[1] and abs(positions[1]-positions[2])<.01
    ops,_=layout('f(x = 1)=1+1\n=2')
    positions=equals(ops)
    assert len(positions)==3 and positions[0]!=positions[1] and abs(positions[1]-positions[2])<.01
    ops,_=layout('$long=2$\n\n$=2$')
    assert equals(ops)[0]>equals(ops)[1] and equals(ops)[1]==0
    ops,_=layout('Let $long=2$\n$=2$')
    assert equals(ops)[0]>equals(ops)[1] and equals(ops)[1]==0
    c=Composer('empty',0,settings)
    parts=parse_document('$x=2$ $=2$')['lines'][0]['runs']
    width=max(c.math(run['body'],16).width for run in parts if run['type']=='math')+1
    ops,issues=layout('$x=2$ $=2$',width)
    assert not issues and abs(equals(ops)[0]-equals(ops)[1])<.01
    assert len({round(o['rect'][1],2) for o in ops if o.get('label')=='='})==2
    c=Composer('empty',0,settings)
    boxes=[c.math(line['runs'][0]['body'],16) for line in parse_document('$long=1$\n$=abcdefgh$')['lines']]
    _,issues=layout('$long=1$\n$=abcdefgh$',max(box.width for box in boxes)+1)
    assert any('等号对齐后超出作答区域' in issue for issue in issues)
    print('PASS only leading equals continue prior equations; separate calculations remain left aligned')
    print('PASS nested equals, wrapped continuations, source text, and overflow are preserved')

if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
