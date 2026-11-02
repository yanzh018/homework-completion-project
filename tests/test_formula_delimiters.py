"""Verify mixed AI formula delimiters and safe rendering of older saved drafts."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-formulas-')
os.environ['HANDWRITING_DATA']=temporary.name
from app import app,TOKEN
from rendering import parse_document,parse_document_legacy
from storage import dumps,execute,one

HEADERS={'X-Local-Token':TOKEN}

def main():
    canonical=parse_document(r'$\frac{x}{2}$')
    for text in (r'\(\frac{x}{2}\)',r'\[\frac{x}{2}\]', '$\n\\frac{x}{2}\n$', '$$\n\\frac{x}{2}\n$$'):
        assert parse_document(text)==canonical,text
    usual='Text $x^{2}$ and $y_{1}$.\nNext line.'
    assert parse_document(usual)==parse_document_legacy(usual)
    mixed=r'Let \(u=x^{2}\). Then $u=4$.'
    ast=parse_document(mixed)
    assert sum(r['type']=='math' for line in ast['lines'] for r in line['runs'])==2
    sets=parse_document(r'$A,B\in\mathbb{R}^{3\times2}$')
    assert all(c in json.dumps(sets,ensure_ascii=False) for c in ('∈','ℝ','×'))
    assert '∉' in json.dumps(parse_document(r'$x\notin[1,2]$'),ensure_ascii=False)
    for invalid in (r'\(x',r'\[x',r'\(\notImplemented{x}\)',r'$\mathbb{X}$', '$x\ntext $ trailing'):
        try:parse_document(invalid)
        except ValueError:pass
        else:raise AssertionError('invalid or unsupported formula was accepted')
    print('PASS inline, display, multiline and invalid math delimiters')

    with app.test_client() as c:
        pid=c.post('/api/profiles',json={'name':'临时测试'},headers=HEADERS).json['id']
        a=c.post('/api/assignments',json={'profile_id':pid},headers=HEADERS).json
        for text,old_ast,old_error in ((r'\(\frac{x}{2}\)',parse_document_legacy(r'\(\frac{x}{2}\)'),None),
                                       ('$\n\\frac{x}{2}\n$',None,'旧版不支持跨行公式')):
            answers=a['answers'];answers[0].update(text=text,ast=old_ast,reviewed=False)
            if old_error:answers[0]['parse_error']=old_error
            else:answers[0].pop('parse_error',None)
            execute('UPDATE assignments SET answers=? WHERE id=?',(dumps(answers),a['id']))
            result=c.post(f"/api/assignments/{a['id']}/render",json={},headers=HEADERS)
            assert result.status_code==200,result.json
            region=result.json['regions'][0]
            assert any(op.get('role')=='fraction' for op in region['placements'])
            assert '\\' not in result.json['missing'] and not any('公式需要修正' in x for x in result.json['errors'])
            saved=json.loads(one('SELECT answers FROM assignments WHERE id=?',(a['id'],))['answers'])[0]
            assert saved['text']==text and saved['ast']==old_ast
        text=r'$A,B\in\mathbb{R}^{3\times2}$'
        answers[0].update(text=text,ast=None,parse_error='旧版不支持集合符号',reviewed=False)
        execute('UPDATE assignments SET answers=? WHERE id=?',(dumps(answers),a['id']))
        result=c.post(f"/api/assignments/{a['id']}/render",json={},headers=HEADERS)
        assert result.status_code==200 and {'ℝ','×'}<=set(result.json['missing']) and '∈' not in result.json['missing']
        assert any(op.get('role')=='membership' for op in result.json['regions'][0]['placements'])
        assert not any('公式需要修正' in x for x in result.json['errors'])
        assert json.loads(one('SELECT answers FROM assignments WHERE id=?',(a['id'],))['answers'])[0]['text']==text
        answers[0].update(text=r'$x\notin[1,2]$',ast=None,parse_error='旧版不支持不属于符号',reviewed=False)
        execute('UPDATE assignments SET answers=? WHERE id=?',(dumps(answers),a['id']))
        result=c.post(f"/api/assignments/{a['id']}/render",json={},headers=HEADERS)
        assert result.status_code==200 and '∉' not in result.json['missing']
        assert any(op.get('role')=='membership_not' for op in result.json['regions'][0]['placements'])
        assert not any('公式需要修正' in x for x in result.json['errors'])
        answers[0].update(text='$x$',ast={'type':'unexpected'});answers[0].pop('parse_error',None)
        execute('UPDATE assignments SET answers=? WHERE id=?',(dumps(answers),a['id']))
        assert c.post(f"/api/assignments/{a['id']}/render",json={},headers=HEADERS).status_code==400
        print('PASS old drafts render as structured formulas without changing stored answers; tampered AST rejected')

if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
