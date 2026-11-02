"""PDF text-layer exclusion + local OCR words + explicit, reviewable forced alignment.

No connected component is assigned a character label. OCR words remain reusable
ligatures; proposed character cuts use projection costs and are always pending.
"""
import hashlib
import json
import math
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import cv2
import fitz
import numpy as np
from PIL import Image, ImageOps

from storage import DATA, ROOT, db, dumps, execute, glyph_record, one, page_record, rows, uid

SCALE = 2.0
VISION = ROOT / "tools" / "vision_ocr"
SUPPORTED = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-=.,:;!?()[]{}'<>/%"


def local_ocr(path):
    if not VISION.exists():
        return [], "本地 OCR 未安装。macOS 请运行 ./start.sh 编译 Vision；仍可手动框选和标注。"
    try:
        p = subprocess.run([str(VISION), str(path)], capture_output=True, timeout=90, check=True)
        return json.loads(p.stdout), None
    except (subprocess.SubprocessError, ValueError) as e:
        return [], f"本地 OCR 失败（{type(e).__name__}），可手动框选；没有使用演示识别结果。"


def deskew(rgb):
    """Estimate small page rotation from long text baselines; retain inverse mapping."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 60, 180)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 720, 80,
                            minLineLength=max(50, rgb.shape[1] // 12), maxLineGap=15)
    angles = []
    for x1, y1, x2, y2 in ([] if lines is None else lines[:, 0]):
        a = math.degrees(math.atan2(y2-y1, x2-x1))
        if abs(a) < 8:
            angles.append(a)
    angle = float(np.median(angles)) if len(angles) >= 4 else 0.0
    if abs(angle) < .25:
        angle = 0.0
    h, w = rgb.shape[:2]
    matrix = cv2.getRotationMatrix2D((w/2, h/2), angle, 1)
    # Keep original page dimensions. Use a white border; never rotate the exported PDF.
    corrected = cv2.warpAffine(rgb, matrix, (w, h), borderValue=(255, 255, 255))
    return corrected, angle, matrix.tolist()


def foreground(rgb):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((31, 31), np.uint8))
    flattened = cv2.divide(gray, np.maximum(background, 1), scale=255)
    alpha = 255 - flattened
    alpha[flattened > 215] = 0
    # Red/pink strokes are suggested correction marks. Blue/black ink is retained.
    r, g, b = rgb.transpose(2, 0, 1).astype(float)
    red = (r > g * 1.28 + 18) & (r > b * 1.20 + 12)
    alpha[red] = 0
    return alpha, int(red.sum())


def pt_box(box, width, height):
    if len(box) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box):
        raise ValueError("边界必须包含四个有限数值")
    x0, y0, x1, y1 = map(float, box)
    if x1 <= x0 or y1 <= y0 or min(x0, y0) < 0 or x1 > width+.05 or y1 > height+.05:
        raise ValueError("边界超出页面，或宽高不是正数")
    if x1-x0 < .4 or y1-y0 < .4:
        raise ValueError("裁剪区域太小")
    return [x0, y0, x1, y1]


def suggested_regions(rgb, scale=SCALE):
    """Empty horizontal bands between actual original ink, without deleting questions."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    left, right = int(w*.11), int(w*.89)
    occupied = (gray[:, left:right] < 205).sum(axis=1) > 1
    occupied = cv2.dilate(occupied.astype(np.uint8)[:, None], np.ones((17, 1), np.uint8))[:, 0] > 0
    regions, start = [], None
    for y in range(int(h*.10), int(h*.93)):
        if not occupied[y] and start is None:
            start = y
        if (occupied[y] or y == int(h*.93)-1) and start is not None:
            if y-start >= 42*scale:
                regions.append([left/scale, (start+5)/scale, right/scale, (y-5)/scale])
            start = None
    return regions


def serialize_drawing(d):
    def conv(v):
        if isinstance(v, (fitz.Point, fitz.Rect, fitz.Quad)):
            return list(v) if not isinstance(v, fitz.Quad) else [list(x) for x in v]
        if isinstance(v, (list, tuple)):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        return v
    return conv(d)


def vector_ink_page(page, drawings, target, include_simple=False, printed_boxes=()):
    """Replay real drawing operators, excluding print/rules before rasterization.

    Painting over a rule after rasterization erases handwriting crossing that rule.
    Replaying paths avoids that destructive failure and keeps a reusable vector source.
    """
    isolated=fitz.open();out=isolated.new_page(width=page.rect.width,height=page.rect.height)
    for drawing in drawings:
        if len(drawing["items"])<=2:
            if not include_simple:
                continue
            rect=drawing["rect"]
            # Long, straight rules are likely printed tables or separators. Keep
            # short pen segments together as unknown candidates, not characters.
            if rect.width>=20 or rect.height>=20 or any(rect.intersects(fitz.Rect(b)) for b in printed_boxes):
                continue
        color=drawing.get("color");fill=drawing.get("fill")
        visible=fill or color or (0,0,0)
        if len(visible)==3 and visible[0]>visible[1]*1.28+.07 and visible[0]>visible[2]*1.2+.05:
            continue
        shape=out.new_shape()
        for item in drawing["items"]:
            if item[0]=="l":shape.draw_line(item[1],item[2])
            elif item[0]=="c":shape.draw_bezier(item[1],item[2],item[3],item[4])
            elif item[0]=="re":shape.draw_rect(item[1])
            elif item[0]=="qu":shape.draw_quad(item[1])
        shape.finish(color=color,fill=fill,width=drawing.get("width") or 1,
                     even_odd=bool(drawing.get("even_odd")),closePath=bool(drawing.get("closePath")),
                     fill_opacity=drawing.get("fill_opacity") or 1,
                     stroke_opacity=drawing.get("stroke_opacity") or 1)
        shape.commit()
    isolated.save(target,deflate=True)
    pix=out.get_pixmap(matrix=fitz.Matrix(SCALE,SCALE),alpha=False)
    rgb=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3).copy()
    isolated.close()
    return rgb


def prepare_document(document_id, notify=lambda *a: None):
    doc = one("SELECT * FROM documents WHERE id=?", (document_id,))
    source = DATA / doc["original"]
    folder = DATA / "pages" / document_id
    folder.mkdir(parents=True, exist_ok=True)
    pdf = source.suffix.lower() == ".pdf"
    if pdf:
        original = fitz.open(source)
        if original.needs_pass:
            raise ValueError("请先解密 PDF，再上传")
        if original.metadata.get("subject", "").startswith("HandwritingGenerated") and doc["kind"] == "sample":
            raise ValueError("此文件由本系统生成，不能作为真实笔迹来源")
        if not 0 < len(original) <= 40:
            raise ValueError("首版每份文件支持 1–40 页")
        working = fitz.open(source)
    else:
        im = ImageOps.exif_transpose(Image.open(source)).convert("RGB")
        if im.width * im.height > 40_000_000:
            raise ValueError("图片超过 4000 万像素，请缩小后上传")
        im.thumbnail((2400, 3400))
        original = fitz.open()
        p = original.new_page(width=im.width/SCALE, height=im.height/SCALE)
        import io
        stream = io.BytesIO()
        im.save(stream, format="PNG")
        p.insert_image(p.rect, stream=stream.getvalue())
        original.save(folder / "original.pdf")
        working = fitz.open(folder / "original.pdf")
    text_parts, warnings, counts = [], [], []
    for number, page in enumerate(original):
        notify(number/len(original), f"第 {number+1}/{len(original)} 页：分离题干、笔迹与批改颜色")
        if page.rotation:
            raise ValueError(f"第 {number+1} 页设置了 PDF 页面旋转；请先将页面旋转永久应用后再导入，避免坐标错位")
        if page.rect.width > 2000 or page.rect.height > 3000:
            raise ValueError("页面尺寸过大，请先转换为常规作业尺寸")
        pix = page.get_pixmap(matrix=fitz.Matrix(SCALE, SCALE), alpha=False)
        rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3).copy()
        image_path = folder / f"{number}.png"
        Image.fromarray(rgb).save(image_path)
        text_blocks, printed_boxes = [], []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    printed_boxes.append(list(span["bbox"]))
                    text_blocks.append({"text": span["text"], "bbox": list(span["bbox"]), "font": span["font"]})
        drawings = page.get_drawings()
        complex_paths = [d for d in drawings if len(d["items"]) >= 6]
        short_strokes = [d for d in drawings if len(d["items"])<=2 and d["rect"].width<20 and d["rect"].height<20]
        simple_pen = len(short_strokes)>=20
        vector_ink = (len(complex_paths)>=12 or simple_pen) and len(page.get_images())==0
        vector_path = folder / f"{number}-vectors.json"
        vector_path.write_text(dumps([serialize_drawing(d) for d in drawings]))
        if vector_ink:
            ink_rgb=vector_ink_page(page,drawings,folder/f"{number}-ink-vector.pdf",include_simple=simple_pen,printed_boxes=printed_boxes)
            alpha, red_count = foreground(ink_rgb)
            angle, matrix = 0, [[1, 0, 0], [0, 1, 0]]
            classification = "handwriting" if printed_boxes and not simple_pen else "unknown"
        else:
            corrected, angle, matrix = deskew(rgb)
            alpha, red_count = foreground(corrected)
            if printed_boxes:
                # Use ORIGINAL coordinates for precise vector-text masks. Deskew only scans.
                angle, matrix = 0, [[1, 0, 0], [0, 1, 0]]
                alpha, red_count = foreground(rgb)
                for box in printed_boxes:
                    x0, y0, x1, y1 = [int(v*SCALE) for v in box]
                    alpha[max(y0-2,0):y1+2, max(x0-2,0):x1+2] = 0
                for d in drawings:
                    if len(d["items"]) <= 4:
                        x0,y0,x1,y1 = [int(v*SCALE) for v in d["rect"]+(-1,-1,1,1)]
                        alpha[max(0,y0):y1+1,max(0,x0):x1+1] = 0
            classification = "unknown"  # black print and black handwriting cannot be certified from pixels
        # Restore scan ink coordinates to the untouched page, retaining the affine transform.
        if angle:
            alpha = cv2.warpAffine(alpha, cv2.invertAffineTransform(np.array(matrix)),
                                   (rgb.shape[1], rgb.shape[0]), borderValue=0)
        ink_path = folder / f"{number}-ink.png"
        Image.fromarray(alpha).save(ink_path)
        ocr_path = folder / f"{number}-ocr.png"
        Image.fromarray(255-alpha).save(ocr_path)
        lines, ocr_warning = ([], None)
        if doc["kind"] == "sample" and (alpha>30).sum() > 15:
            notify((number+.3)/len(original), f"第 {number+1} 页：本地 OCR 与字词定位（待校对）")
            lines, ocr_warning = local_ocr(ocr_path)
        question_text = page.get_text(sort=True).strip()
        if not question_text and doc["kind"] == "assignment":
            question_lines, ocr_warning = local_ocr(image_path)
            question_text = "\n".join(x["text"] for x in question_lines)
        text_parts.append(f"【第 {number+1} 页】\n{question_text}")
        if ocr_warning:
            warnings.append(ocr_warning)
        meta = {"scale": SCALE, "printed": text_blocks, "ocr": lines, "vector_ink": vector_ink,"simple_pen_paths":simple_pen,
                "vector_path": str(vector_path.relative_to(DATA)), "deskew_degrees": angle,
                "deskew_matrix": matrix, "classification": classification, "excluded_red_pixels": red_count,
                "suggested_regions": suggested_regions(rgb), "warning": ocr_warning,
                "segmentation": "OCR 词框 + 仅沿连续空白竖缝保守切分；没有安全分界时保留整词"}
        execute("INSERT INTO pages VALUES(?,?,?,?,?,?,?)",
                (document_id, number, page.rect.width, page.rect.height,
                 str(image_path.relative_to(DATA)), str(ink_path.relative_to(DATA)), dumps(meta)))
        if doc["kind"] == "sample":
            counts.append(extract_candidates(document_id, number))
    original.close()
    working.close()
    execute("UPDATE documents SET status='ready' WHERE id=?", (document_id,))
    return {"document_id": document_id, "pages": len(text_parts), "candidates": sum(counts),
            "questions": "\n\n".join(text_parts), "warnings": list(set(warnings))}


def measure(alpha, label, bbox, baseline=None):
    ys, xs = np.where(alpha > 40)
    if len(xs) < 2:
        raise ValueError("裁剪区域没有足够笔迹，请调整边界")
    h, w = alpha.shape
    dist = cv2.distanceTransform((alpha>80).astype(np.uint8), cv2.DIST_L2, 3)
    stroke = float(np.percentile(dist[dist>0], 70)*2/SCALE) if dist.max() else 0.5
    yy = ys.astype(float)-ys.mean()
    xx = xs.astype(float)-xs.mean()
    slant = float(np.degrees(np.arctan(-np.dot(xx,yy)/(np.dot(yy,yy)+1))))
    ratio = baseline if baseline is not None else (.76 if any(c in 'gjpqy' for c in label) else .96)
    return {"width": w/SCALE, "height": h/SCALE, "baseline": float(ratio),
            "slant_degrees": max(-40, min(40,slant)), "stroke_width": stroke,
            "ink_pixels": int(len(xs)), "source_line": round(bbox[3]/15)}


def crop_safety(crop):
    """Flag source boxes whose ink runs along a crop edge, where strokes may be cut."""
    mask=crop>40
    ys,xs=np.where(mask)
    if not len(xs):return {"margin_px":2,"edges":{},"clipped":True}
    margin=2;height=max(1,int(ys.max()-ys.min()+1));width=max(1,int(xs.max()-xs.min()+1))
    edges={
        "left":float(len(np.unique(ys[xs<margin]))/height),
        "right":float(len(np.unique(ys[xs>=crop.shape[1]-margin]))/height),
        "top":float(len(np.unique(xs[ys<margin]))/width),
        "bottom":float(len(np.unique(xs[ys>=crop.shape[0]-margin]))/width),
    }
    # A tiny endpoint can be natural; a long edge run usually means a clipped stroke.
    return {"margin_px":margin,"edges":edges,"clipped":any(span>=.28 for span in edges.values())}


@lru_cache(maxsize=16)
def _legacy_page_ink(document_id,page_number):
    p=page_record(document_id,page_number)
    return np.array(Image.open(DATA/p["ink"]).convert("L"))


@lru_cache(maxsize=8192)
def _separate_marks_safe(path,label,modified):
    """Keep ambiguous disconnected strokes out of automatic single-glyph selection."""
    if len(label)!=1 or not label.isascii() or not label.isalnum() or label in 'fijt':return True
    with Image.open(path) as image:alpha=np.asarray(image.convert('RGBA'))[:,:,3]
    _,_,stats,_=cv2.connectedComponentsWithStats((alpha>40).astype(np.uint8),8)
    parts=sorted((tuple(int(v) for v in row) for row in stats[1:]),key=lambda row:row[4],reverse=True)
    if not parts:return False
    x,y,w,h,area=parts[0]
    if label=='E' and len(parts)==3:
        # A handwritten capital E can have a stem/top stroke plus two
        # detached horizontal bars. Require both bars to sit inside the
        # main glyph's footprint rather than admitting arbitrary fragments.
        bars=parts[1:]
        centres=[]
        for xx,yy,ww,hh,other in bars:
            if (ww<2*hh or yy<y+.3*h or yy+hh>y+1.2*h or
                xx<x-.15*w or xx+ww>x+1.2*w):return False
            centres.append((yy+hh*.5-y)/max(1,h))
        return min(centres)<.75 and max(centres)>.75
    for xx,yy,ww,hh,other in parts[1:]:
        dx=max(0,x-(xx+ww),xx-(x+w));dy=max(0,y-(yy+hh),yy-(y+h))
        if other>=max(3,area*.04) or (other>=2 and max(dx,dy)>max(4,.25*max(w,h))):return False
    return True


@lru_cache(maxsize=8192)
def _legacy_crop_safe(document_id,page_number,bbox,vector_pdf):
    x0,y0=[int(math.floor(v*SCALE)) for v in bbox[:2]];x1,y1=[int(math.ceil(v*SCALE)) for v in bbox[2:]]
    margin=5
    if vector_pdf:
        with fitz.open(DATA/vector_pdf) as source:
            rect=fitz.Rect((x0-margin)/SCALE,(y0-margin)/SCALE,(x1+margin)/SCALE,(y1+margin)/SCALE)&source[0].rect
            pix=source[0].get_pixmap(matrix=fitz.Matrix(SCALE,SCALE),clip=rect,alpha=True)
            context=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,pix.n)[:,:,3]
            ox,oy=pix.x,pix.y
    else:
        alpha=_legacy_page_ink(document_id,page_number);ox=max(0,x0-margin);oy=max(0,y0-margin)
        context=alpha[oy:min(alpha.shape[0],y1+margin),ox:min(alpha.shape[1],x1+margin)]
    return _crop_complete_in_context(context,ox,oy,x0,y0,x1,y1)


def _crop_complete_in_context(context,ox,oy,x0,y0,x1,y1):
    inside=np.zeros(context.shape,dtype=bool)
    inside[max(0,y0-oy):min(context.shape[0],y1-oy),max(0,x0-ox):min(context.shape[1],x1-ox)]=True
    crop=context[max(0,y0-oy):min(context.shape[0],y1-oy),max(0,x0-ox):min(context.shape[1],x1-ox)]
    if not crop_safety(crop)['clipped']:return True
    # Touching the crop boundary is safe only when the same ink component
    # does not continue outside it. Inspect the original page, not the crop.
    mask=context>40;_,labels=cv2.connectedComponents(mask.astype(np.uint8),8)
    interior=set(np.unique(labels[inside&mask]))-{0}
    exterior=set(np.unique(labels[(~inside)&mask]))-{0}
    return bool(interior) and not bool(interior&exterior)


@lru_cache(maxsize=8192)
def _image_digest(path,modified,size):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def glyph_signature(g):
    path=DATA/g['image'];stat=path.stat()
    return hashlib.sha256(dumps([g['label'],g['bbox'],_image_digest(str(path),stat.st_mtime_ns,stat.st_size)]).encode()).hexdigest()


def glyph_verified(g):
    check=g['metrics'].get('automatic_verification',{})
    try:return check.get('accepted') is True and (g['origin']!='extracted' or check.get('image_checked') is True) and check.get('signature')==glyph_signature(g)
    except (OSError,ValueError,KeyError,TypeError):return False


def glyph_usable(g):
    """Apply the same non-destructive source and fragment checks to old and new samples."""
    metrics=g['metrics'];safety=metrics.get('crop_safety')
    try:
        checked=metrics.get('automatic_verification')
        if (checked and checked.get('signature')==glyph_signature(g) and checked.get('accepted') is not True
            and (g.get('origin')!='extracted' or checked.get('image_checked') is True)):return False
        if safety is None and metrics.get('source_type') in ('raster','vector'):
            vector_pdf=metrics['selected_vector_pdf'] if metrics.get('vector_isolated') else None
            if not _legacy_crop_safe(g['document_id'],g['page'],tuple(g['bbox']),vector_pdf):return False
        if safety and safety.get('clipped'):return False
        path=DATA/g['image']
        return path.is_file() and _separate_marks_safe(str(path),g['label'],path.stat().st_mtime_ns)
    except (OSError,ValueError,KeyError,IndexError,TypeError):
        return False


def save_glyph(document_id, page_number, label, bbox, confidence=0, classification=None,
               parent=None, note="", glyph_id=None, baseline=None, status="pending", isolate_vectors=False,
               vector_sequences=None):
    if len(label) > 80 or any(c in label for c in '\r\n\t'):
        raise ValueError("标签最长 80 字符，不能包含换行或制表符")
    p = page_record(document_id, page_number)
    doc = one("SELECT * FROM documents WHERE id=? AND kind='sample'", (document_id,))
    bbox = pt_box(bbox, p["width"], p["height"])
    glyph_id = glyph_id or uid()
    selected_paths=[]
    if isolate_vectors:
        if not p["meta"]["vector_ink"]:raise ValueError("该页没有可分离的矢量手写笔画，请使用普通边界裁剪")
        source=fitz.open(DATA/doc["original"]);source_page=source[page_number]
        if vector_sequences is not None:
            if not isinstance(vector_sequences,list) or not vector_sequences or any(type(n) is not int or n<0 for n in vector_sequences):
                source.close();raise ValueError("矢量笔画编号无效")
            wanted=set(vector_sequences)
            drawings=[d for d in source_page.get_drawings() if d["seqno"] in wanted and d["rect"].intersects(fitz.Rect(bbox))]
            if {d["seqno"] for d in drawings}!=wanted:
                source.close();raise ValueError("所选矢量笔画不存在或不在字形区域内")
        else:
            drawings=[d for d in source_page.get_drawings() if (len(d["items"])>2 or p["meta"].get("simple_pen_paths")) and
                      bbox[0] <= (d["rect"].x0+d["rect"].x1)/2 <= bbox[2] and
                      bbox[1] <= (d["rect"].y0+d["rect"].y1)/2 <= bbox[3]]
        if not drawings:
            source.close();raise ValueError("框内没有笔画中心，请放宽边界或关闭矢量笔画分离")
        selected_paths=[d["seqno"] for d in drawings]
        rgb=vector_ink_page(source_page,drawings,DATA/f"glyphs/{glyph_id}-vector.pdf",
                            include_simple=p["meta"].get("simple_pen_paths",False),
                            printed_boxes=[x["bbox"] for x in p["meta"]["printed"]])
        alpha,_=foreground(rgb);source.close()
    else:
        if vector_sequences is not None:raise ValueError("选择矢量笔画时需要启用矢量分离")
        alpha = np.array(Image.open(DATA/p["ink"]).convert("L"))
    x0, y0 = [int(math.floor(v*SCALE)) for v in bbox[:2]]
    x1, y1 = [int(math.ceil(v*SCALE)) for v in bbox[2:]]
    crop = alpha[y0:y1, x0:x1]
    ys, xs = np.where(crop>40)
    if not len(xs):
        raise ValueError("此区域未检测到笔迹。印刷文字和红色批改可能已被屏蔽。")
    safety=crop_safety(crop)
    if safety['clipped']:
        margin=5;ox=max(0,x0-margin);oy=max(0,y0-margin)
        context=alpha[oy:min(alpha.shape[0],y1+margin),ox:min(alpha.shape[1],x1+margin)]
        if _crop_complete_in_context(context,ox,oy,x0,y0,x1,y1):
            safety['edge_touch_only']=True;safety['clipped']=False
    if status in ("accepted","holdout") and safety["clipped"]:
        raise ValueError("候选边界可能切断笔画，已保留待校对")
    # Tight crop + one pixel padding; source bbox remains an exact, editable page region.
    ink_bbox = [x0+int(xs.min()), y0+int(ys.min()), x0+int(xs.max())+1, y0+int(ys.max())+1]
    ink_crop = crop[ys.min():ys.max()+1,xs.min():xs.max()+1]
    ink_crop = np.pad(ink_crop,1)
    metrics = measure(ink_crop, label, bbox, baseline)
    metrics["ink_bbox"] = [v/SCALE for v in ink_bbox]
    metrics["crop_safety"] = safety
    metrics["source_type"] = "vector" if p["meta"]["vector_ink"] else "raster"
    metrics["vector_source"] = p["meta"]["vector_path"] if p["meta"]["vector_ink"] else None
    metrics["vector_isolated"] = bool(isolate_vectors)
    if isolate_vectors:
        metrics["selected_vector_pdf"]=f"glyphs/{glyph_id}-vector.pdf"
        metrics["source_path_sequence_numbers"]=selected_paths
        metrics["vector_explicit_selection"]=vector_sequences is not None
    if vector_sequences is not None:
        # Preserve the original vector detail when a small symbol is enlarged later.
        scale=min(8,math.sqrt(4_000_000/((bbox[2]-bbox[0])*(bbox[3]-bbox[1]))))
        with fitz.open(DATA/metrics["selected_vector_pdf"]) as isolated:
            pix=isolated[0].get_pixmap(matrix=fitz.Matrix(scale,scale),clip=fitz.Rect(bbox),alpha=True)
            high=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,pix.n)[:,:,3]
            hy,hx=np.where(high>40)
            if not len(hx):raise ValueError("所选矢量笔画没有可用墨迹")
            ink_crop=np.pad(high[hy.min():hy.max()+1,hx.min():hx.max()+1],1)
            metrics["image_scale"]=scale
            metrics["image_ink_bbox"]=[(pix.x+int(hx.min()))/scale,(pix.y+int(hy.min()))/scale,
                                       (pix.x+int(hx.max())+1)/scale,(pix.y+int(hy.max())+1)/scale]
    metrics["image_size"]=[int(ink_crop.shape[1]),int(ink_crop.shape[0])]
    image_path = f"glyphs/{glyph_id}.png"
    rgba = np.zeros((*ink_crop.shape,4),dtype=np.uint8)
    rgba[:,:,3] = ink_crop
    Image.fromarray(rgba).save(DATA/image_path)
    classification = classification or p["meta"]["classification"]
    with db() as c:
        previous = c.execute("SELECT original_label,label,bbox,metrics FROM glyphs WHERE id=?", (glyph_id,)).fetchone()
        if previous and previous["label"]==label and json.loads(previous["bbox"])==bbox:
            old_metrics=json.loads(previous["metrics"])
            if old_metrics.get("radical_slices") and old_metrics.get("source_path_sequence_numbers")==selected_paths and old_metrics.get("image_size")==metrics["image_size"]:
                metrics["radical_slices"]=old_metrics["radical_slices"]
        c.execute("""INSERT INTO glyphs(id,profile_id,document_id,page,label,original_label,bbox,image,metrics,
                  confidence,classification,status,origin,parent,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(id) DO UPDATE SET label=excluded.label,bbox=excluded.bbox,
                  metrics=excluded.metrics,classification=excluded.classification,status=excluded.status,note=excluded.note""",
                  (glyph_id,doc["profile_id"],document_id,page_number,label,
                   previous[0] if previous else label,dumps(bbox),image_path,dumps(metrics),confidence,
                   classification,status,"extracted",parent,note))
    return glyph_record(one("SELECT * FROM glyphs WHERE id=?",(glyph_id,)))


def split_word(alpha, bbox_pixels, text):
    """Split only where a multi-column blank gutter separates adjacent glyphs.

    ponytail: uncertain connected words stay as ligatures; expand only with a
    validated character aligner trained on enough reviewed local samples.
    """
    x0,y0,x1,y1 = map(int,bbox_pixels)
    crop = alpha[max(y0,0):y1,max(x0,0):x1]
    if crop.size == 0 or not text:
        return []
    projection = (crop>40).sum(axis=0).astype(float)
    width = crop.shape[1]
    weights = np.array([.48 if c in "il1'.,:;!|" else 1.35 if c in 'mwMW' else 1 for c in text])
    expected = np.cumsum(weights)/weights.sum()*width
    boundaries=[0]
    for k,target in enumerate(expected[:-1]):
        radius=max(2,int(width/len(text)*.32))
        lo=max(boundaries[-1]+1,int(target-radius))
        hi=min(width-(len(text)-k-1),int(target+radius)+1)
        if lo>=hi:
            return []
        # Split only at a visibly empty gutter. A minimum through ink turns a
        # connected stroke from 1/3/x into part of its neighbour.
        choices=np.arange(max(2,lo),min(width-2,hi))
        valleys=[int(x) for x in choices if projection[x-2:x+2].sum()==0]
        if not valleys:return []
        boundaries.append(min(valleys,key=lambda x:abs(x-target)))
    boundaries.append(width)
    return [[x0+boundaries[i], y0, x0+boundaries[i+1], y1] for i in range(len(text))]


def extract_candidates(document_id, page_number, engine="本地 OCR", include_unrecognized=True):
    p=page_record(document_id,page_number)
    alpha=np.array(Image.open(DATA/p["ink"]).convert("L"))
    h,w=alpha.shape
    count=0
    covered=np.zeros_like(alpha)
    for line in p["meta"]["ocr"]:
        for word in line.get("words",[]):
            text=word["text"].strip()
            if not text or len(text)>65:
                continue
            norm=word["bbox"]
            pixels=[max(0,int(norm[0]*w)-2),max(0,int(norm[1]*h)-3),
                    min(w,int(norm[2]*w)+3),min(h,int(norm[3]*h)+3)]
            box=[v/SCALE for v in pixels]
            try:
                whole=save_glyph(document_id,page_number,text,box,word["confidence"],note=f"{engine} 整词/自然连笔；请核对标签和边界")
                count+=1
            except ValueError:
                continue
            x0,y0,x1,y1=pixels
            covered[y0:y1,x0:x1]=255
            if 1<len(text)<=24:
                for char,part in zip(text,split_word(alpha,pixels,text)):
                    try:
                        save_glyph(document_id,page_number,char,[v/SCALE for v in part],
                                   min(word["confidence"],.55),parent=whole["id"],
                                   note=f"{engine} 标签 + 空白竖缝切分；没有安全分界时保留整词")
                        count+=1
                    except ValueError:
                        pass
    if not include_unrecognized:
        return count
    # Unrecognized strokes are grouped as UNLABELLED regions, never as characters.
    remaining=((alpha>60)&(covered==0)).astype(np.uint8)*255
    grouped=cv2.dilate(remaining,np.ones((9,13),np.uint8))
    contours,_=cv2.findContours(grouped,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x,y,bw,bh=cv2.boundingRect(contour)
        if bw<6 or bh<7 or bw*bh>120000 or (alpha[y:y+bh,x:x+bw]>40).sum()<12:
            continue
        try:
            save_glyph(document_id,page_number,"",[x/SCALE,y/SCALE,(x+bw)/SCALE,(y+bh)/SCALE],
                       note="OCR 未识别区域；此框可能含多个字、笔画或非文字，请手工标注/合并/拆分")
            count+=1
        except ValueError:
            pass
    return count


def profile_coverage(profile_id):
    glyphs=[glyph_record(x) for x in rows("SELECT * FROM glyphs WHERE profile_id=? AND status='accepted' AND origin='extracted'",(profile_id,))]
    glyphs=[g for g in glyphs if glyph_usable(g)]
    labels={}
    for g in glyphs:
        labels.setdefault(g["label"],[]).append(g)
    chars={c:len(labels.get(c,[])) for c in SUPPORTED}
    style={}
    singles=[g for g in glyphs if len(g["label"])==1 and g["label"].isalnum()]
    if singles:
        for name,key in [("height","height"),("stroke_width","stroke_width"),("slant_degrees","slant_degrees")]:
            values=[g["metrics"][key] for g in singles]
            style[name]={"median":float(np.median(values)),"p10":float(np.percentile(values,10)),"p90":float(np.percentile(values,90))}
    # Only compare neighbours on the same source line and of the same candidate granularity.
    gaps,wordgaps,linegaps=[],[],[]
    documents={g["document_id"] for g in glyphs}
    for doc_id in documents:
        gs=[g for g in glyphs if g["document_id"]==doc_id]
        for page in {g["page"] for g in gs}:
            pg=[g for g in gs if g["page"]==page]
            words=sorted([g for g in pg if len(g["label"])>1 and not g["parent"]],key=lambda g:(g["bbox"][3],g["bbox"][0]))
            for a,b in zip(words,words[1:]):
                if abs(a["bbox"][3]-b["bbox"][3])<5:
                    gap=b["bbox"][0]-a["bbox"][2]
                    if 0<gap<40:wordgaps.append(gap)
            ys=sorted({round(g["bbox"][3]/8)*8 for g in words})
            linegaps.extend(b-a for a,b in zip(ys,ys[1:]) if 14<b-a<65)
            for parent in {g["parent"] for g in pg if g["parent"]}:
                children=sorted([g for g in pg if g["parent"]==parent],key=lambda g:g["bbox"][0])
                for a,b in zip(children,children[1:]):
                    gap=b["metrics"]["ink_bbox"][0]-a["metrics"]["ink_bbox"][2]
                    if 0<=gap<20:gaps.append(gap)
    for key,values,default in [("letter_gap",gaps,.8),("word_gap",wordgaps,4),("line_gap",linegaps,25)]:
        style[key]={"median":float(np.median(values)) if values else default,"samples":len(values),"estimated":not bool(values)}
    return {"characters":chars,"missing":[c for c,n in chars.items() if not n],
            "covered":sum(n>0 for n in chars.values()),"total":len(chars),"accepted":len(glyphs),
            "ligatures":{c:len(v) for c,v in labels.items() if len(c)>1},"style":style,
            "pending":one("SELECT COUNT(*) n FROM glyphs WHERE profile_id=? AND status='pending'",(profile_id,))["n"],
            "holdout":one("SELECT COUNT(*) n FROM glyphs WHERE profile_id=? AND status='holdout'",(profile_id,))["n"]}
