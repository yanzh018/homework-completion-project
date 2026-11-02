"""Exercise local preview progress and readable formula parsing in an isolated database."""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import fitz

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-preview-progress-')
os.environ['HANDWRITING_DATA']=temporary.name
import app as server
from rendering import Composer,answer_region_rect,parse_document,visible_text
from storage import DATA,one

HEADERS={'X-Local-Token':server.TOKEN}


def main():
    page_doc=fitz.open();page=page_doc.new_page();page.insert_text((50,100),'Solution.',fontsize=12)
    printed=page.get_text('blocks')[0];region={'source':'ai','bbox':[45,printed[1]+1,300,200]}
    adjusted=answer_region_rect(region,page)
    assert adjusted[1]>=printed[3]+4 and region['bbox'][1]==printed[1]+1
    region['user_edited']=True;assert answer_region_rect(region,page)==region['bbox']
    page_doc.close()
    c=server.app.test_client()
    pid=c.post('/api/profiles',json={'name':'隔离预览测试'},headers=HEADERS).json['id']
    assignment=c.post('/api/assignments',json={'profile_id':pid},headers=HEADERS).json
    answer=r'Let \(u=3x^3+2x\). Then \(D_xu=9x^2+2\). $\frac{1}{u}$.'
    question=r'Find the derivative: $D_x \ln(3x^3+2x)$'
    assignment['answers'][0].update(text=answer,question=question)
    assignment=c.put('/api/assignments/'+assignment['id'],json=assignment,headers=HEADERS).json
    preview=c.post('/api/format-preview',json={'texts':[question,answer,r'$\unknown{x}$']},headers=HEADERS)
    assert preview.status_code==200
    assert any(run['type']=='math' for line in preview.json[0]['ast']['lines'] for run in line['runs'])
    assert any(run['type']=='math' for line in preview.json[1]['ast']['lines'] for run in line['runs'])
    assert '不支持' in preview.json[2]['error']
    assert c.get('/api/assignments/'+assignment['id']).json['answers'][0]['text']==answer
    marked='**Result:** $x^2$'
    ops,_=Composer(pid,0,{}).layout(parse_document(marked),[20,20,550,800],16)
    assert visible_text('**Result:**',True)=='Result:' and not any(op.get('label')=='*' for op in ops)
    assert ''.join(op.get('label','') for op in ops).startswith('Result:')
    compact=c.post('/api/assignments',json={'profile_id':pid},headers=HEADERS).json
    original_text='$x$\n\n$y$\n\n$z$'
    compact['answers'][0].update(text=original_text,source='ai',bbox=[50,65,545,155])
    compact=c.put('/api/assignments/'+compact['id'],json=compact,headers=HEADERS).json
    result=c.post('/api/assignments/'+compact['id']+'/render',json={},headers=HEADERS).json
    assert result['regions'][0]['auto_compacted_blank_lines']
    assert not any('答案高度超出' in e for e in result['errors'])
    assert ''.join(o.get('label','') for o in result['regions'][0]['placements'])=='xyz'
    assert c.get('/api/assignments/'+compact['id']).json['answers'][0]['text']==original_text
    compact['answers'][0]['user_edited']=True
    compact=c.put('/api/assignments/'+compact['id'],json=compact,headers=HEADERS).json
    result=c.post('/api/assignments/'+compact['id']+'/render',json={},headers=HEADERS).json
    assert not result['regions'][0]['auto_compacted_blank_lines']
    assert any('答案高度超出' in e for e in result['errors'])
    compact['answers'][0].update(text='$x$\n$y$\n$z$\n$w$',user_edited=False,bbox=[50,65,545,153])
    compact=c.put('/api/assignments/'+compact['id'],json=compact,headers=HEADERS).json
    result=c.post('/api/assignments/'+compact['id']+'/render',json={},headers=HEADERS).json
    assert 12.8<=result['effective_font_size']<16 and not any('答案高度超出' in e for e in result['errors'])
    assert any('统一将字高' in w for w in result['warnings'])
    assert result['settings']['font_size']==16 and result['regions'][0]['text']==compact['answers'][0]['text']
    compact['answers'].append({'id':'second','page':0,'bbox':[50,200,545,300],'text':'$x$','source':'ai','reviewed':False,'seed':0})
    compact=c.put('/api/assignments/'+compact['id'],json=compact,headers=HEADERS).json
    result=c.post('/api/assignments/'+compact['id']+'/render',json={},headers=HEADERS).json
    assert all(op['size']==result['effective_font_size'] for region in result['regions'] for op in region['placements'] if op['kind']=='fallback')
    compact['settings']['auto_fit']=False
    compact=c.put('/api/assignments/'+compact['id'],json=compact,headers=HEADERS).json
    result=c.post('/api/assignments/'+compact['id']+'/render',json={},headers=HEADERS).json
    assert result['effective_font_size']==16 and any('答案高度超出' in e for e in result['errors'])
    compact['settings']['auto_fit']=True;compact['answers'][0]['text']='\n'.join(['$x$']*20)
    compact=c.put('/api/assignments/'+compact['id'],json=compact,headers=HEADERS).json
    result=c.post('/api/assignments/'+compact['id']+'/render',json={},headers=HEADERS).json
    assert result['effective_font_size']==16 and any('答案高度超出' in e for e in result['errors'])
    assert result['regions'][0]['text'].count('$x$')==20

    entered=threading.Event();release=threading.Event();observed=[]
    original=server.render_assignment
    def held(*args,**kwargs):
        callback=kwargs['progress'];blocked=False
        def probe(value,message):
            nonlocal blocked
            observed.append((value,message));callback(value,message)
            if value>=.16 and not blocked:
                blocked=True;entered.set();assert release.wait(20)
        kwargs['progress']=probe
        return original(*args,**kwargs)
    server.render_assignment=held
    def start():
        r=c.post('/api/assignments/'+assignment['id']+'/render/start',json={'revision':assignment['revision']},headers=HEADERS)
        assert r.status_code==202,r.json
        return r.json['task_id']
    def finish(task):
        end=time.monotonic()+25
        while time.monotonic()<end:
            t=c.get('/api/tasks/'+task).json
            if t['status']!='running':return t
            time.sleep(.05)
        raise AssertionError('preview task did not finish')
    try:
        task=start();assert entered.wait(15)
        current=c.get('/api/tasks/'+task).json
        assert current['status']=='running' and 0<current['progress']<1
        assert current['result']['assignment_id']==assignment['id']
        assert start()==task,'same assignment and revision must share the running task'
        release.set();done=finish(task)
        assert done['status']=='complete' and done['progress']==1,done
        audit=json.loads((DATA/'exports'/done['result']['export_id']/'audit.json').read_text())
        assert audit['revision']==assignment['revision'] and audit['regions'][0]['text']==answer and audit['format_version']==1
        assert observed and all(a[0]<=b[0] for a,b in zip(observed,observed[1:]))
        assert any('排版' in m for _,m in observed) and any('检查' in m for _,m in observed) and any('预览图片' in m for _,m in observed)

        entered.clear();release.clear();observed.clear()
        task=start();assert entered.wait(15)
        assignment['answers'][0]['text']='Changed $x=2$.'
        updated=c.put('/api/assignments/'+assignment['id'],json=assignment,headers=HEADERS)
        assert updated.status_code==200,updated.json
        assignment=updated.json
        release.set();done=finish(task)
        assert done['status']=='error' and '发生变化' in done['message'],done
        persisted=one('SELECT answers,latest FROM assignments WHERE id=?',(assignment['id'],))
        assert persisted['latest'] is None and json.loads(persisted['answers'])[0]['text']=='Changed $x=2$.'
        assert c.post('/api/assignments/'+assignment['id']+'/render/start',json={'revision':assignment['revision']-1},headers=HEADERS).status_code==409
        def failed(*args,**kwargs):
            kwargs['progress'](.25,'正在检查测试页面')
            raise ValueError('测试中的预览错误')
        server.render_assignment=failed
        failed_task=start();failure=finish(failed_task)
        assert failure['status']=='error' and failure['message']=='测试中的预览错误' and failure['progress']==.25
        assert one('SELECT latest FROM assignments WHERE id=?',(assignment['id'],))['latest'] is None
        print('PASS real rendering stages, monotonic progress, task reuse and final result')
        print('PASS concurrent edits and failed renders preserve the current answer and reject stale previews')
        print('PASS local formula previews parse and preserve editable source text')
    finally:
        release.set();server.render_assignment=original


if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
