"""Check normalized real glyph sizes and preservation of disconnected strokes."""
import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temporary=tempfile.TemporaryDirectory(prefix='ink-size-')
os.environ['HANDWRITING_DATA']=temporary.name
from extraction import _separate_marks_safe,glyph_signature,glyph_usable
import inference
from rendering import Composer,_cached_image_stream,_dilation_preserves_strokes,_erosion_preserves_strokes,_glyphs_overlap,_ink_clip,_letter_vertical_size,image_stream,parse_document,validate_settings
from storage import DATA,dumps,execute,glyph_record,init,rows


def main():
    init();execute("INSERT INTO profiles(id,name) VALUES('p','隔离测试')")
    def glyph(label,body,extras=(),origin='extracted'):
        path=DATA/'glyphs'/f'{label}-{origin}.png'
        image=Image.new('RGBA',(22,30),(0,0,0,0));draw=ImageDraw.Draw(image)
        draw.rectangle(body,fill=(0,0,0,255))
        for extra in extras:draw.rectangle(extra,fill=(0,0,0,255))
        image.save(path)
        metrics={'width':11,'height':15,'baseline':.5,'stroke_width':.8,'slant_degrees':0,'ink_bbox':[0,0,11,15]}
        execute("""INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (label+'-'+origin,'p',label,label,dumps([0,0,11,15]),str(path.relative_to(DATA)),dumps(metrics),
                 'handwriting' if origin=='extracted' else 'inferred','accepted',origin))
        return path
    e=glyph('e',(4,12,16,26))
    a=glyph('a',(3,8,17,25))
    i=glyph('i',(8,20,11,25),[(9,1,10,2)])
    g=glyph('g',(3,3,16,8),[(6,25,9,27)])
    equals=glyph('=',(3,8,17,9),[(3,18,17,19)])
    glyph('-',(8,13,10,15))
    glyph('ln',(3,8,17,25))
    glyph('t',(10,4,11,25))
    glyph('w',(6,4,11,25))
    glyph('z',(4,8,16,24),origin='inferred')
    original_hash=hashlib.sha256(e.read_bytes()).hexdigest()
    def clip(path,label):return _ink_clip(str(path),label,path.stat().st_mtime_ns)
    contaminated=DATA/'glyphs'/'contaminated-e.png'
    image=Image.new('RGBA',(22,30),(0,0,0,0));draw=ImageDraw.Draw(image)
    draw.rectangle((4,12,16,26),fill=(0,0,0,255));draw.rectangle((4,1,5,2),fill=(0,0,0,255));image.save(contaminated)
    assert clip(contaminated,'e')[1]==0,'output clipping must retain every ink component'
    assert not _separate_marks_safe(str(contaminated),'e',contaminated.stat().st_mtime_ns),'isolated marks must quarantine the sample'
    source=np.zeros((20,20),np.uint8);source[2:14,4:8]=255;source[2:5,12:15]=255
    eroded=source.copy();eroded[2:5,12:15]=0
    assert not _erosion_preserves_strokes(source,eroded),'stroke normalization must not erase a detached stroke'
    separate=np.zeros((20,20),np.uint8);separate[3:8,8:11]=255;separate[10:16,8:11]=255
    joined=cv2.dilate(separate,np.ones((3,3),np.uint8))
    assert not _dilation_preserves_strokes(separate,joined),'stroke normalization must not join separate marks'
    counter=np.zeros((20,20),np.uint8);counter[4:15,4:15]=255;counter[6:13,6:13]=0
    closed=cv2.dilate(counter,np.ones((9,9),np.uint8))
    assert not _dilation_preserves_strokes(counter,closed),'stroke normalization must not fill an enclosed counter'
    execute("INSERT INTO profiles(id,name) VALUES('legacy','旧样本')")
    execute("INSERT INTO documents(id,profile_id,name,kind,original,sha256,status) VALUES('legacy-doc','legacy','sample.pdf','sample','originals/sample.pdf','test','ready')")
    page=Image.new('L',(80,80),0);ImageDraw.Draw(page).rectangle((20,20,30,40),fill=255);page.save(DATA/'pages'/'legacy-ink.png')
    execute("INSERT INTO pages(document_id,number,width,height,image,ink,meta) VALUES('legacy-doc',0,40,40,'','pages/legacy-ink.png','{}')")
    legacy={'label':'a','document_id':'legacy-doc','page':0,'image':str(a.relative_to(DATA)),'metrics':{'source_type':'raster'},'bbox':[11,11,15,20]}
    assert not glyph_usable(legacy),'an old accepted sample clipped at its source edge must not enter rendering'
    legacy['bbox']=[10,10,16,21]
    assert glyph_usable(legacy),'a complete stroke touching the crop edge must remain usable'
    legacy['bbox']=[8,8,18,23]
    assert glyph_usable(legacy),'an intact old sample must remain usable'
    assert clip(i,'i')[1]<=1 and clip(i,'i')[3]>25,'the dot on i must be preserved'
    assert clip(g,'g')[1]<=3 and clip(g,'g')[3]>27,'a disconnected descender must be preserved'
    assert clip(a,'a')==(2,7,19,27),'one pixel of padding must remain for stroke normalization'
    assert clip(equals,'=')==(0,0,22,30),'both strokes of a symbol must be preserved'
    composer=Composer('p',42,validate_settings({}))
    assert composer.glyph('=',16).width>=8 and composer.glyph('-',16).width>=6.72
    function=composer.math(parse_document(r'$\ln x$')['lines'][0]['runs'][0]['body'],16)
    assert [op['label'] for op in function.ops if op['kind']=='glyph']==['ln']
    assert any(op.get('label')=='x' for op in function.ops),'function composition must preserve its argument'
    ebox=composer.glyph('e',16);abox=composer.glyph('a',16)
    er=ebox.ops[0]['ink_rect'];ar=abox.ops[0]['ink_rect']
    assert abs((er[3]-er[1])-(ar[3]-ar[1]))<.15,'stroke calibration may move an ink edge by less than a fifth of a point'
    def visual_baseline(box):
        op=box.ops[0];alpha=np.asarray(Image.open(io.BytesIO(image_stream(op))).convert('RGBA'))[:,:,3]
        ys=np.where(alpha>100)[0];return op['rect'][1]+np.quantile(ys,.95)/max(1,alpha.shape[0]-1)*(op['rect'][3]-op['rect'][1])
    assert abs(visual_baseline(ebox)-visual_baseline(abox))<16*.06
    assert abox.ops[0]['rect'][0]<abox.ops[0]['ink_rect'][0] and abox.ops[0]['ink_rect'][2]==abox.width
    pair=composer.text_word('aa',16).ops
    assert pair[1]['ink_rect'][0]-pair[0]['ink_rect'][2]<16*.15
    assert not _glyphs_overlap(pair[0],pair[1],1)
    assert ebox.ops[0]['ink_clip']==clip(e,'e') and len(image_stream(ebox.ops[0]))>0
    hits=_cached_image_stream.cache_info().hits;image_stream(ebox.ops[0]);image_stream(ebox.ops[0])
    assert _cached_image_stream.cache_info().hits>hits,'repeated glyph drawing should reuse the verified image'
    assert hashlib.sha256(e.read_bytes()).hexdigest()==original_hash,'source glyph must stay unchanged'
    assert composer.glyph('g',16).descent>0
    assert _letter_vertical_size('A',16)[0]>_letter_vertical_size('a',16)[0]
    assert _letter_vertical_size('a',16)[0]>=.84*_letter_vertical_size('A',16)[0]
    formula=composer.math(parse_document('$a^2$')['lines'][0]['runs'][0]['body'],16)
    base=next(op for op in formula.ops if op.get('label')=='a')
    assert base['rect'][3]-base['rect'][1]>0 and len(formula.ops)>=2
    assert composer.glyph('z',16).ops[0]['origin']=='inferred'
    assert 'z' not in Composer('p',42,{'allow_inferred':False}).library
    def stroke_width(op,normalize):
        op={**op,"stroke_target":op["stroke_target"] if normalize else None}
        im=Image.open(io.BytesIO(image_stream(op,1))).convert('RGBA');rect=op['rect']
        size=(max(2,round((rect[2]-rect[0])*8)),max(2,round((rect[3]-rect[1])*8)))
        alpha=cv2.resize(np.asarray(im)[:,:,3],size,interpolation=cv2.INTER_CUBIC)
        mask=np.pad((alpha>128).astype(np.uint8),2)
        dist=cv2.distanceTransform(mask,cv2.DIST_L2,cv2.DIST_MASK_PRECISE)
        return float(np.percentile(dist[dist>0],70)*2/8)
    ink=[composer.glyph('t',16).ops[0],composer.glyph('w',10).ops[0]]
    before=[stroke_width(op,False) for op in ink];after=[stroke_width(op,True) for op in ink]
    assert abs(after[0]-after[1])<abs(before[0]-before[1]),(before,after)
    metrics={'width':11,'height':15,'baseline':.96,'stroke_width':.8,'slant_degrees':0,'crop_safety':{'clipped':False}}
    execute("INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ('dirty-h','p','h','h',dumps([0,0,11,15]),str(contaminated.relative_to(DATA)),dumps(metrics),'handwriting','accepted','extracted'))
    for row in rows("SELECT * FROM glyphs WHERE profile_id='p' AND origin='extracted'"):
        sample=glyph_record(row);sample['metrics']['automatic_verification']={'signature':glyph_signature(sample),'accepted':True,'image_checked':True}
        execute('UPDATE glyphs SET metrics=? WHERE id=?',(dumps(sample['metrics']),sample['id']))
    refs,_=inference.reference_sheet('p','h');assert all(g['id']!='dirty-h' for g in refs)
    shape={'glyphs':[{'label':'h','paths':[{'width':3,'fill':False,'commands':[['M',25,15],['L',25,110],['L',60,55],['L',70,110]]}]}]}
    def fake_chat(action,*args):
        result=shape if action=='infer_glyphs' else {'checks':[{'id':0,'label':'h','complete':True,'confidence':.98}]}
        return json.dumps(result),{'model':'local-mock'}
    with patch.object(inference.cloud,'config',return_value={'supports_images':True}),patch.object(inference.cloud,'chat',side_effect=fake_chat):
        result=inference.complete('p','h',True)
    assert result['glyphs'][0]['origin']=='inferred' and result['glyphs'][0]['status']=='accepted'
    mixed=Composer('p',0,validate_settings({}));hbox=mixed.glyph('h',16);hop=hbox.ops[0]
    assert hop['origin']=='inferred' and hop.get('padding',0)>0
    source_path=DATA/hop['image'];source_hash=hashlib.sha256(source_path.read_bytes()).hexdigest()
    raw=dict(hop,stroke_target=None)
    thin=np.asarray(Image.open(io.BytesIO(image_stream(raw))).convert('RGBA'))[:,:,3]
    thick=np.asarray(Image.open(io.BytesIO(image_stream(hop))).convert('RGBA'))[:,:,3]
    assert thin.shape==thick.shape and _dilation_preserves_strokes(thin,thick)
    assert not any((thick[0,:]>100).tolist()+(thick[-1,:]>100).tolist()+(thick[:,0]>100).tolist()+(thick[:,-1]>100).tolist())
    before=stroke_width(hop,False);after=stroke_width(hop,True)
    assert after>before and abs(after-hop['stroke_target'])<abs(before-hop['stroke_target']),(before,after,hop['stroke_target'])
    assert hashlib.sha256(source_path.read_bytes()).hexdigest()==source_hash,'stroke tuning must not alter inferred source'
    mixed_ops=mixed.text_word('ah',16).ops
    assert not _glyphs_overlap(mixed_ops[0],mixed_ops[1],1),'collision checks must use the thickened ink'
    assert not Composer('p',0,validate_settings({'uniform_stroke':False})).glyph('h',16).ops[0].get('padding')
    ring=Image.new('RGBA',(40,50),(0,0,0,0));ImageDraw.Draw(ring).ellipse((0,0,39,49),outline=(0,0,0,255),width=2)
    ring_path=DATA/'glyphs'/'o-inferred.png';ring.save(ring_path)
    execute("INSERT INTO glyphs(id,profile_id,label,original_label,bbox,image,metrics,classification,status,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ('o-inferred','p','o','o',dumps([0,0,20,25]),str(ring_path.relative_to(DATA)),dumps(metrics),'inferred','accepted','inferred'))
    ring_op=Composer('p',0,validate_settings({})).glyph('o',16).ops[0]
    ring_raw=np.asarray(Image.open(io.BytesIO(image_stream(dict(ring_op,stroke_target=None)))).convert('RGBA'))[:,:,3]
    ring_thick=np.asarray(Image.open(io.BytesIO(image_stream(ring_op))).convert('RGBA'))[:,:,3]
    assert _dilation_preserves_strokes(ring_raw,ring_thick),'thickening must retain enclosed counters'
    shape['glyphs'][0]['label']='q'
    with patch.object(inference.cloud,'config',return_value={'supports_images':True}),patch.object(inference.cloud,'chat',side_effect=fake_chat):
        mismatch=inference.complete('p','q',True)
    assert mismatch['glyphs'][0]['status']=='pending','independent label disagreement must not be auto-accepted'
    print('PASS uniform x-height and baseline; ascenders, descenders and symbols preserved')
    print('PASS ambiguous extra ink is quarantined and disconnected strokes are preserved')
    print('PASS AI completion is enabled by default and can be disabled')
    print('PASS body and superscript stroke widths move closer together')
    print('PASS tight AI glyphs can thicken without clipping, merging or changing the source')


if __name__=='__main__':
    try:main()
    finally:temporary.cleanup()
