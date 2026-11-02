"""Formula words keep text spacing without treating adjacent variables as words."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-math-text-')
os.environ['HANDWRITING_DATA']=temporary.name
from app import app,TOKEN
from rendering import Composer,parse_document,validate_settings
from storage import dumps,execute,one


def main():
    def body(text):return parse_document('$'+text+'$')['lines'][0]['runs'][0]['body']
    c=Composer('empty',0,validate_settings({}));size=16
    word=c.math(body(r'\text{by}'),size);variables=c.math(body('by'),size)
    def gap(box):
        a,b=box.ops
        return b.get('ink_rect',b['rect'])[0]-a.get('ink_rect',a['rect'])[2]
    assert 0<gap(word)<gap(variables)
    mixed=c.math(body(r'n\,\text{by}\,m'),size);n,b,y,m=mixed.ops
    assert b['rect'][0]-n['rect'][2]>gap(word) and m['rect'][0]-y['rect'][2]>gap(word)
    assert body(r'\text{one by two}')['children'][0]=={'type':'text','value':'one by two'}
    assert body('by')['children']==[{'type':'char','value':'b'},{'type':'char','value':'y'}]
    assert body(r'\text{a \& b}')['children'][0]['value']=='a & b'
    text=r'$\text{(a) }\frac{x}{2}$';previous=parse_document(text,legacy_text=True)
    with app.test_client() as client:
        headers={'X-Local-Token':TOKEN}
        pid=client.post('/api/profiles',json={'name':'隔离测试'},headers=headers).json['id']
        assignment=client.post('/api/assignments',json={'profile_id':pid},headers=headers).json
        answers=assignment['answers'];answers[0].update(text=text,ast=previous,reviewed=False)
        execute('UPDATE assignments SET answers=? WHERE id=?',(dumps(answers),assignment['id']))
        response=client.post('/api/assignments/'+assignment['id']+'/render',json={},headers=headers)
        assert response.status_code==200,response.json
        assert json.loads(one('SELECT answers FROM assignments WHERE id=?',(assignment['id'],))['answers'])[0]['ast']==previous
    print('PASS text words, separate variables, word boundaries, and older stored formulas')


if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
