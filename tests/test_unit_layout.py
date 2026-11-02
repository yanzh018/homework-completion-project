"""Numbers and their units must remain together through layout and preview."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-unit-layout-')
os.environ['HANDWRITING_DATA']=temporary.name
from app import app,TOKEN
from rendering import Composer,parse_document,unit_line_runs,validate_settings
from storage import execute


def main():
    execute("INSERT INTO profiles(id,name) VALUES('p','单位测试')")
    settings=validate_settings({'font_size':16})
    def layout(text,width):
        ast=parse_document(text);before=json.dumps(ast,ensure_ascii=False)
        composer=Composer('p',0,settings);ops,issues=composer.layout(ast,[0,0,width,500],16)
        assert json.dumps(ast,ensure_ascii=False)==before
        return ops,issues
    def bottom(ops,label):return [o['rect'][3] for o in ops if o.get('label')==label]
    source="$v'=3(12)^2(3)=1296$\n$\\mathrm{in}^3/\\mathrm{s}$"
    original=parse_document(source)
    assert len(original['lines'])==2 and len(unit_line_runs(original['lines']))==1
    ops,issues=layout(source,500)
    assert not issues and abs(bottom(ops,'6')[-1]-bottom(ops,'i')[0])<.01
    assert abs(bottom(ops,'6')[-1]-bottom(ops,'s')[-1])<.01
    composer=Composer('p',0,settings)
    size=16;space=size*composer.word_gap
    width=composer.text_word('1296',size).width+space+composer.text_word('kg',size).width+1
    ops,issues=layout('x 1296 kg',width)
    assert not issues and bottom(ops,'x')[0]<bottom(ops,'6')[-1]
    assert abs(bottom(ops,'6')[-1]-bottom(ops,'k')[0])<.01
    parts=['1296','in','/','s'];width=sum(composer.text_word(p,size).width for p in parts)+3*space+1
    ops,issues=layout('x 1296 in / s',width)
    assert not issues and bottom(ops,'x')[0]<bottom(ops,'6')[-1]
    assert abs(bottom(ops,'6')[-1]-bottom(ops,'s')[-1])<.01
    ops,_=layout('$x=1296$\n$y=3$',500)
    assert bottom(ops,'6')[-1]<bottom(ops,'y')[0]
    _,issues=layout('1296 kg',5)
    assert any('宽度超出作答区域' in issue for issue in issues)
    with app.test_client() as client:
        response=client.post('/api/format-preview',json={'texts':[source]},headers={'X-Local-Token':TOKEN})
        assert response.status_code==200 and len(response.json[0]['ast']['lines'])==1
    print('PASS numeric results stay with units across explicit and automatic line breaks')
    print('PASS unrelated lines and saved answer AST remain unchanged; oversized groups are reported')

if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
