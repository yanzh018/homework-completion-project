"""Selecting actual vector paths must not carry neighboring ink into a glyph."""
import os
import sys
import tempfile
import base64
import copy
import io
import json
from pathlib import Path

import fitz
from PIL import Image,ImageOps

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-vector-selection-')
os.environ['HANDWRITING_DATA']=temporary.name
from app import app,TOKEN
from extraction import _legacy_crop_safe,save_glyph
import review_ai
from review_ai import fingerprint
from storage import DATA,dumps,execute,page_record


def main():
    document=fitz.open();page=document.new_page(width=120,height=100)
    for points in (((15,20),(25,20),(25,35),(15,35)),((42,20),(52,20),(52,35),(42,35))):
        shape=page.new_shape()
        for a,b in zip(points,points[1:]+points[:1]):shape.draw_line(a,b)
        shape.finish(color=None,fill=(0,0,0),closePath=True);shape.commit()
    source=DATA/'originals'/'vector.pdf';document.save(source);document.close()
    with fitz.open(source) as original:
        pix=original[0].get_pixmap(matrix=fitz.Matrix(2,2),colorspace=fitz.csGRAY,alpha=False)
        ink=ImageOps.invert(Image.frombytes('L',(pix.width,pix.height),pix.samples))
        ink.save(DATA/'pages'/'page-ink.png')
    execute("INSERT INTO profiles(id,name) VALUES('p','隔离测试')")
    execute("INSERT INTO documents(id,profile_id,name,kind,original,sha256,status) VALUES('d','p','vector.pdf','sample','originals/vector.pdf','test','ready')")
    execute("INSERT INTO pages(document_id,number,width,height,image,ink,meta) VALUES('d',0,120,100,'','pages/page-ink.png',?)",
            (dumps({'vector_ink':True,'vector_path':'pages/vector.pdf','printed':[]}),))
    g=save_glyph('d',0,'√',[10,15,60,40],classification='handwriting',isolate_vectors=True,vector_sequences=[0])
    assert g['metrics']['vector_explicit_selection'] and g['metrics']['source_path_sequence_numbers']==[0]
    assert g['metrics']['image_scale']==8 and g['metrics']['image_size'][0]>50
    vector=g['metrics']['selected_vector_pdf']
    assert _legacy_crop_safe('d',0,(15,20,25,35),vector),'a complete selected path at the boundary must remain usable'
    assert not _legacy_crop_safe('d',0,(16,21,24,34),vector),'a selected path crossing the boundary must remain excluded'
    touching=save_glyph('d',0,'x',[15,20,25,35],classification='handwriting',status='accepted',isolate_vectors=True,vector_sequences=[0])
    assert touching['metrics']['crop_safety']['edge_touch_only'],'a complete edge-touching path should be accepted without trimming'
    try:save_glyph('d',0,'x',[16,21,24,34],classification='handwriting',status='accepted',isolate_vectors=True,vector_sequences=[0])
    except ValueError as error:assert '切断笔画' in str(error)
    else:raise AssertionError('a truly clipped selected path must stay pending')
    edited={**g,'metrics':{**g['metrics'],'source_path_sequence_numbers':[1]}}
    assert fingerprint(g)!=fingerprint(edited)
    assert g['metrics']['ink_bbox'][2]<30
    p=page_record('d',0)
    whole=review_ai._verification_crop(ink,g['bbox'])
    selected=review_ai._verification_crop(ink,g['bbox'],DATA/g['metrics']['selected_vector_pdf'])
    assert whole.crop((60,5,90,45)).getbbox(),'the page crop contains its neighboring stroke'
    assert selected.crop((5,5,35,45)).getbbox() and not selected.crop((60,5,90,45)).getbbox()
    original_chat=review_ai.cloud.chat
    def mock_chat(action,content,summary,consent):
        sheet=Image.open(io.BytesIO(base64.b64decode(content[1]['image_url']['url'].split(',',1)[1]))).convert('L')
        dark=sheet.point(lambda x:255 if x<100 else 0)
        assert dark.crop((273,79,293,110)).getbbox(),'selected stroke must reach the independent review'
        assert not dark.crop((327,79,347,110)).getbbox(),'neighboring stroke must not reach the saved-crop side of independent review'
        return json.dumps({'checks':[{'id':0,'label':'√','complete':True,'confidence':.99}]}),{'model':'local-mock'}
    try:
        review_ai.cloud.chat=mock_chat
        suggestions={0:{'confidence':.99,'decision':'accept','label':'√','bbox':g['bbox'],'reason':''}}
        event=review_ai._verify_candidates([g],suggestions,p,True)
        assert event and suggestions[0]['decision']=='accept' and suggestions[0]['verification'][0]['label']=='√'
        missing=copy.deepcopy(g);missing['metrics']['selected_vector_pdf']='missing-vector.pdf'
        suggestions={0:{'confidence':.99,'decision':'accept','label':'√','bbox':g['bbox'],'reason':''}}
        assert review_ai._verify_candidates([missing],suggestions,p,True) is None
        assert suggestions[0]['decision']=='uncertain'
        changed=copy.deepcopy(g);changed['metrics']['vector_explicit_selection']=False
        suggestions={0:{'confidence':.99,'decision':'accept','label':'√','bbox':[9,15,60,40],'reason':''}}
        assert review_ai._verify_candidates([changed],suggestions,p,True) is None
        assert suggestions[0]['decision']=='uncertain'
    finally:review_ai.cloud.chat=original_chat
    slices={'head':[0,0,6,10],'bar':[5,1,10,3],'anchor_y':.1}
    g['metrics']['radical_slices']=slices
    execute('UPDATE glyphs SET metrics=? WHERE id=?',(dumps(g['metrics']),g['id']))
    with app.test_client() as client:
        saved=client.patch('/api/glyphs/'+g['id'],json={'note':'保留选择'},headers={'X-Local-Token':TOKEN})
        assert saved.status_code==200,saved.json
        assert saved.json['metrics']['source_path_sequence_numbers']==[0]
        assert saved.json['metrics']['radical_slices']==slices
        assert saved.json['metrics']['ink_bbox'][2]<30
    try:save_glyph('d',0,'x',[10,15,60,40],isolate_vectors=True,vector_sequences=[99])
    except ValueError:pass
    else:raise AssertionError('invalid vector path was accepted')
    print('PASS selected vector paths exclude nearby ink in both AI verification and saved samples')
    print('PASS missing or unstable vector previews remain pending without a model call')

if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
