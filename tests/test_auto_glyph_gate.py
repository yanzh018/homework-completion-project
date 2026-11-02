"""A used candidate must be verified, and a failed or changed candidate cannot pass preflight."""
import base64, io, json, os, sys, tempfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
tmp=tempfile.TemporaryDirectory(prefix='ink-glyph-gate-');os.environ['HANDWRITING_DATA']=tmp.name
import app as server
from extraction import glyph_signature, glyph_usable, glyph_verified
from rendering import Composer,validate_settings
from storage import DATA, dumps, execute, glyph_record, one


def main():
    execute("INSERT INTO profiles(id,name) VALUES('p','test')")
    for label in ('x','e'):
        im=Image.new('RGBA',(24,30),(0,0,0,0));ImageDraw.Draw(im).line((4,4,18,25),fill='black',width=3);im.save(DATA/'glyphs'/f'{label}.png')
        execute("INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",(label,'p',label,label,dumps([0,0,12,15]),f'glyphs/{label}.png',dumps({'width':12,'height':15,'baseline':.96,'stroke_width':.8,'slant_degrees':0}),'inferred','accepted','inferred'))
    execute("INSERT INTO assignments(id,profile_id,title,settings,answers,revision,latest) VALUES('a','p','test','{}','[]',1,'exp')")
    folder=DATA/'exports'/'exp';folder.mkdir();(folder/'audit.json').write_text(dumps({'glyph_checks':[{'id':'x'},{'id':'e'}]}))
    client=server.app.test_client();headers={'X-Local-Token':server.TOKEN};payload={'revision':1,'export_id':'exp','ids':['x','e'],'consent':True}
    with patch.object(server.inference,'verify_proposals',return_value=({'x'},{'model':'mock'})):
        response=client.post('/api/assignments/a/verify-used-glyphs',json=payload,headers=headers)
    assert response.status_code==200,response.json
    assert response.json['accepted']==1 and response.json['excluded']==1
    x=glyph_record(one("SELECT * FROM glyphs WHERE id='x'"));e=glyph_record(one("SELECT * FROM glyphs WHERE id='e'"))
    assert x['metrics']['automatic_verification']['signature']==glyph_signature(x)
    assert not glyph_usable(e),'detected label mismatch must be excluded'
    execute("INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",('x-unverified','p','x','x',dumps([0,0,12,15]),'glyphs/x.png',dumps({'width':12,'height':15,'baseline':.96,'stroke_width':.8,'slant_degrees':0}),'inferred','accepted','inferred'))
    composer=Composer('p',5,validate_settings({}))
    assert all(composer.glyph('x',12).ops[0]['glyph_id']=='x' for _ in range(4)),'prefer verified variants to avoid needless rechecks'
    assert client.post('/api/assignments/a/verify-used-glyphs',json={**payload,'ids':['other']},headers=headers).status_code==400
    assert client.post('/api/assignments/a/verify-used-glyphs',json={**payload,'revision':2},headers=headers).status_code==400
    execute("UPDATE glyphs SET label='q' WHERE id='x'")
    changed=glyph_record(one("SELECT * FROM glyphs WHERE id='x'"))
    assert changed['metrics']['automatic_verification']['signature']!=glyph_signature(changed),'editing the label must invalidate evidence'
    execute("INSERT INTO documents(id,profile_id,name,kind,original,sha256,status) VALUES('d','p','old.pdf','sample','originals/old.pdf','test','ready')")
    Image.new('L',(80,80),0).save(DATA/'pages'/'ink.png')
    execute("INSERT INTO pages(document_id,number,width,height,image,ink,meta) VALUES('d',0,40,40,'','pages/ink.png','{}')")
    execute("INSERT INTO glyphs(id,profile_id,document_id,page,label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?,?)",('real','p','d',0,'a',dumps([0,0,12,15]),'glyphs/x.png',dumps({'width':12,'height':15,'baseline':.96,'stroke_width':.8,'slant_degrees':0}),'handwriting','accepted','extracted'))
    (folder/'audit.json').write_text(dumps({'glyph_checks':[{'id':'real'}]}))
    real=glyph_record(one("SELECT * FROM glyphs WHERE id='real'"))
    real['metrics']['automatic_verification']={'signature':glyph_signature(real),'accepted':True}
    assert not glyph_verified(real),'legacy source-only checks do not verify the saved image'
    real['metrics']['automatic_verification']['accepted']=False
    assert glyph_usable(real) and not glyph_verified(real),'legacy source-only rejection must permit a new review of the actual crop'
    def inspect_saved(action,content,summary,consent):
        image=Image.open(io.BytesIO(base64.b64decode(content[1]['image_url']['url'].split(',',1)[1]))).convert('L')
        assert image.crop((218,32,408,157)).getextrema()[0]<128,'verification must show saved ink even when the source page is blank'
        return json.dumps({'checks':[{'id':0,'label':'a','complete':True,'confidence':.99}]}),{'model':'mock'}
    proposed={0:{'confidence':1.,'decision':'accept','bbox':real['bbox'],'label':'a','reason':''}}
    with patch.object(server.review_ai.cloud,'chat',side_effect=inspect_saved):
        server.review_ai._verify_candidates([real],proposed,server.page_record('d',0),True,use_saved_crop=True)
    assert proposed[0]['decision']=='accept'
    def reject_crop(candidates,proposed,page,consent,use_saved_crop=False):
        assert use_saved_crop,'preflight must verify the actual saved crop'
        proposed[0]['decision']='uncertain';proposed[0]['verification']=[{'label':'b','complete':False,'confidence':.95}]
        return {'model':'mock'}
    with patch.object(server.review_ai,'_verify_candidates',side_effect=reject_crop):
        response=client.post('/api/assignments/a/verify-used-glyphs',json={**payload,'ids':['real']},headers=headers)
    assert response.status_code==200 and response.json['excluded']==1,response.json
    assert not glyph_usable(glyph_record(one("SELECT * FROM glyphs WHERE id='real'"))),'cut or mismatched real crop must be excluded automatically'
    print('PASS used glyph verification, rejection, stale revision and changed-label protection')

if __name__=='__main__':
    try:main()
    finally:tmp.cleanup()
