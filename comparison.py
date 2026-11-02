"""Compare a held-out real glyph to the same label in an immutable rendered export."""
import json
import re

import cv2
import fitz
import numpy as np
from PIL import Image,ImageOps

import cloud
from extraction import local_ocr
from rendering import draw_ops
from storage import DATA,dumps,execute,glyph_record,one,rows,uid


def audit_record(export_id):
    if not re.fullmatch(r"[a-f0-9]{32}",export_id):raise ValueError("生成预览 ID 无效")
    path=DATA/"exports"/export_id/"audit.json"
    if not path.is_file():raise ValueError("请先生成预览")
    return json.loads(path.read_text())


def references(profile_id,export_id):
    audit=audit_record(export_id)
    a=one("SELECT * FROM assignments WHERE id=? AND profile_id=?",(audit["assignment_id"],profile_id))
    labels={op["label"] for r in audit["regions"] for op in r["placements"] if op["kind"]=="glyph"}
    candidates=[glyph_record(g) for g in rows("SELECT * FROM glyphs WHERE profile_id=? AND status IN ('holdout','accepted') AND classification='handwriting' AND origin='extracted' ORDER BY CASE status WHEN 'holdout' THEN 0 ELSE 1 END,page,id",(profile_id,))]
    return [{"id":g["id"],"label":g["label"],"status":g["status"],"page":g["page"],"image_url":g["image_url"]} for g in candidates if g["label"] in labels][:400]


def mask_features(path):
    rgb=np.array(Image.open(path).convert("RGB"));alpha=255-cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    ys,xs=np.where(alpha>60)
    if len(xs)<2:raise ValueError("对比裁剪没有足够笔迹")
    crop=alpha[ys.min():ys.max()+1,xs.min():xs.max()+1]
    h,w=crop.shape;scale=min(60/w,60/h);resized=cv2.resize(crop,(max(1,round(w*scale)),max(1,round(h*scale))))
    normalized=np.zeros((64,64),np.uint8);hh,ww=resized.shape;normalized[(64-hh)//2:(64-hh)//2+hh,(64-ww)//2:(64-ww)//2+ww]=resized
    distance=cv2.distanceTransform((crop>80).astype(np.uint8),cv2.DIST_L2,3)
    xx=xs-xs.mean();yy=ys-ys.mean();slant=float(np.degrees(np.arctan(-np.dot(xx,yy)/(np.dot(yy,yy)+1))))
    return {"aspect_ratio":w/h,"slant_estimate":slant,"relative_stroke":float(np.percentile(distance[distance>0],70)*2/h)},normalized>60


def prepare(export_id,reference_id,region_id=None):
    audit=audit_record(export_id);a=one("SELECT * FROM assignments WHERE id=?",(audit["assignment_id"],))
    reference=glyph_record(one("SELECT * FROM glyphs WHERE id=? AND profile_id=? AND status IN ('accepted','holdout') AND classification='handwriting' AND origin='extracted'",(reference_id,a["profile_id"])))
    matches=[(r,op) for r in audit["regions"] for op in r["placements"] if op["kind"]=="glyph" and op["label"]==reference["label"] and (not region_id or r["id"]==region_id)]
    if not matches:raise ValueError("生成结果中没有相同标签。请选择实际出现的标签，或先生成包含该标签的新内容")
    # Prefer a different real variant; reuse of the reference itself is explicitly disclosed.
    region,op=next(((r,g) for r,g in matches if g["glyph_id"]!=reference_id),matches[0])
    used=audit.get("regions",[])
    source_reused=any(g.get("glyph_id")==reference_id for r in used for g in r["placements"])
    cid=uid();folder=DATA/"exports"/cid;folder.mkdir()
    image=Image.open(DATA/reference["image"]).convert("RGBA")
    white=Image.new("RGB",image.size,"white");white.paste(image,mask=image.getchannel("A"));white=ImageOps.expand(white,border=12,fill="white");white.save(folder/"reference.png")
    pdoc=fitz.open();width=max(600,op["rect"][2]+10);height=max(850,op["rect"][3]+10);p=pdoc.new_page(width=width,height=height)
    settings=audit.get("settings",json.loads(a["settings"]))
    draw_ops(p,[op],settings.get("ink_weight",1))
    rect=fitz.Rect(op["rect"])+(-3,-3,3,3)
    p.get_pixmap(matrix=fitz.Matrix(4,4),clip=rect,alpha=False).save(folder/"generated.png");pdoc.close()
    ref_features,ref_mask=mask_features(folder/"reference.png");gen_features,gen_mask=mask_features(folder/"generated.png")
    dice=float(2*(ref_mask&gen_mask).sum()/max(1,ref_mask.sum()+gen_mask.sum()))
    ref_ocr,ref_error=local_ocr(folder/"reference.png");gen_ocr,gen_error=local_ocr(folder/"generated.png")
    result={"id":cid,"export_id":export_id,"assignment_id":a["id"],"reference_id":reference_id,
            "expected":reference["label"],"reference_status":reference["status"],"source_reused":source_reused,
            "generated_glyph_id":op["glyph_id"],"reference_text":"\n".join(x["text"] for x in ref_ocr),
            "generated_text":"\n".join(x["text"] for x in gen_ocr),"ocr_warnings":[x for x in (ref_error,gen_error) if x],
            "reference_url":f"/exports/{cid}/reference.png","generated_url":f"/exports/{cid}/generated.png",
            "local_metrics":{"normalized_mask_dice":dice,"reference":ref_features,"generated":gen_features},
            "warning":"归一化形状重叠是像素指标，不是本人笔迹相似概率。单字样本不足以评价整词连笔与整页书写习惯。"}
    if source_reused:result["warning"]+=" 对照样本在本次生成中被复用，不属于独立验证；请选择 holdout 样本。"
    (folder/"comparison.json").write_text(dumps(result))
    execute("INSERT INTO comparisons(id,profile_id,assignment_id,export_id,reference_id,payload) VALUES(?,?,?,?,?,?)",(cid,a["profile_id"],a["id"],export_id,reference_id,dumps(result)))
    return result


def run(comparison_id,mode,reference_text,generated_text,consent):
    record=one("SELECT * FROM comparisons WHERE id=?",(comparison_id,));payload=json.loads(record["payload"])
    if max(len(reference_text),len(generated_text))>3000:raise ValueError("对比转录文本过长")
    folder=DATA/"exports"/comparison_id
    result=cloud.compare_images(folder/"reference.png",folder/"generated.png",payload["expected"],reference_text,generated_text,mode,consent)
    result["comparison_id"]=comparison_id
    result["reference_text"]=reference_text;result["generated_text"]=generated_text
    execute("UPDATE comparisons SET result=? WHERE id=?",(dumps(result),comparison_id))
    (folder/"cloud-result.json").write_text(dumps(result))
    return result
