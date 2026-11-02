import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

import cloud
import comparison
import inference
import review_ai
from extraction import (SUPPORTED, VISION, extract_candidates, glyph_signature, prepare_document,
                        profile_coverage, pt_box, save_glyph)
from rendering import parse_document, render_assignment, supplement_template, unit_line_runs, validate_settings
from storage import DATA, db, dumps, execute, glyph_record, init, one, page_record, rows, task_update, uid

init()
app=Flask(__name__)
app.config["MAX_CONTENT_LENGTH"]=40*1024*1024


def local_token():
    """Keep open local pages usable after a normal server restart."""
    path=DATA/".local-session-token"
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd,"w") as f:f.write(secrets.token_urlsafe(32))
    value=path.read_text().strip()
    if len(value)<32:raise RuntimeError("本地会话令牌文件无效，请检查 data/.local-session-token")
    return value


TOKEN=local_token()


def valid_local_token(value):
    if secrets.compare_digest(value,TOKEN):return True
    # One-day bridge for a page that was open before stable session tokens existed.
    # Only the irreversible hash is retained, and a request must still supply its preimage.
    path=DATA/".legacy-session.json"
    try:
        entry=json.loads(path.read_text())
        return entry["expires"]>=time.time() and secrets.compare_digest(hashlib.sha256(value.encode()).hexdigest(),entry["sha256"])
    except (OSError,ValueError,KeyError,TypeError):
        return False


@app.before_request
def local_boundary():
    host=request.host.split(":")[0]
    if host not in ("127.0.0.1","localhost"):
        return jsonify(error="仅允许本机访问"),403
    if request.method in ("POST","PUT","PATCH","DELETE"):
        if not valid_local_token(request.headers.get("X-Local-Token","")):
            return jsonify(error="会话已更新，请刷新页面后重试"),403
        origin=request.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("localhost","127.0.0.1"):
            return jsonify(error="拒绝来自其他网站的请求"),403


@app.after_request
def privacy_headers(response):
    response.headers["Cache-Control"]="no-store"
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["Referrer-Policy"]="no-referrer"
    response.headers["Content-Security-Policy"]="default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"
    return response


@app.errorhandler(Exception)
def error_response(error):
    if isinstance(error,HTTPException):return jsonify(error=error.description),error.code
    if isinstance(error,(ValueError,KeyError,TypeError,IndexError)):
        return jsonify(error=str(error)),400
    app.logger.exception("Local operation failed")
    return jsonify(error="本地操作失败。详情见启动终端，原始文件仍保留。"),500


def body():
    result=request.get_json()
    if not isinstance(result,dict):raise ValueError("需要 JSON 对象")
    return result


def assignment_record(assignment_id):
    a=one("SELECT * FROM assignments WHERE id=?",(assignment_id,))
    a["settings"]=json.loads(a["settings"]);a["answers"]=json.loads(a["answers"])
    if a["document_id"]:a["document"]=document_record(a["document_id"])
    else:a["document"]=None
    return a


def document_record(document_id):
    d=one("SELECT * FROM documents WHERE id=?",(document_id,))
    d.pop("original",None)
    pages=[]
    for p in rows("SELECT * FROM pages WHERE document_id=? ORDER BY number",(document_id,)):
        p["meta"]=json.loads(p["meta"])
        p["image_url"]=f"/files/page/{document_id}/{p['number']}"
        p["ink_url"]=f"/files/ink/{document_id}/{p['number']}"
        pages.append(p)
    d["pages"]=pages
    return d


@app.get("/")
def index():return render_template("index.html",token=TOKEN)


@app.get("/api/state")
def state():
    return jsonify(profiles=rows("SELECT * FROM profiles ORDER BY created"),
                   documents=rows("""SELECT d.id,d.profile_id,d.name,d.kind,d.status,d.error,
                       SUM(CASE WHEN g.origin='extracted' AND g.status='accepted' THEN 1 ELSE 0 END) AS accepted,
                       SUM(CASE WHEN g.origin='extracted' AND g.status='pending' THEN 1 ELSE 0 END) AS pending
                       FROM documents d LEFT JOIN glyphs g ON g.document_id=d.id
                       GROUP BY d.id ORDER BY d.created DESC"""),
                   assignments=rows("SELECT id,profile_id,title,document_id,revision,latest FROM assignments ORDER BY created DESC"),
                   capabilities={"local_ocr":VISION.exists(),"ocr_engine":"Apple Vision（macOS 本地）" if VISION.exists() else "尚未安装；可手动标注",
                                 "cloud":cloud.config(),"storage":str(DATA),"formula":"TeX 子集 → 独立结构化 AST"})


@app.post("/api/profiles")
def create_profile():
    name=str(body().get("name","")).strip()
    if not 1<=len(name)<=80:raise ValueError("档案名称应为 1–80 字符")
    pid=uid();execute("INSERT INTO profiles(id,name) VALUES(?,?)",(pid,name))
    return jsonify(id=pid,name=name)


@app.get("/api/profiles/<pid>/coverage")
def coverage(pid):return jsonify(profile_coverage(pid))


@app.get("/api/profiles/<pid>/inferred")
def inferred_list(pid):
    return jsonify([glyph_record(g) for g in rows("SELECT * FROM glyphs WHERE profile_id=? AND origin='inferred' ORDER BY created DESC",(pid,))])


@app.post("/api/profiles/<pid>/infer")
def infer_glyphs(pid):
    one("SELECT id FROM profiles WHERE id=?",(pid,));b=body()
    return jsonify(inference.complete(pid,str(b.get("characters","")),b.get("consent")))


@app.post("/api/assignments/<aid>/verify-used-glyphs")
def verify_used_glyphs(aid):
    b=body();ids=b.get('ids');a=one("SELECT * FROM assignments WHERE id=?",(aid,))
    if b.get('consent') is not True:raise ValueError("请先确认发送本次字形复核图片")
    if b.get('revision')!=a['revision'] or b.get('export_id')!=a['latest']:raise ValueError("预览已变化，请重新生成后继续复核")
    if not isinstance(ids,list) or not 1<=len(ids)<=12 or len(set(ids))!=len(ids) or not all(isinstance(x,str) for x in ids):raise ValueError("每批复核 1–12 个字形")
    report=json.loads((DATA/'exports'/a['latest']/'audit.json').read_text())
    allowed={x['id'] for x in report.get('glyph_checks',[])}
    if not set(ids)<=allowed:raise ValueError("只能复核当前预览中待检查的字形")
    candidates=[glyph_record(one("SELECT * FROM glyphs WHERE id=? AND profile_id=? AND status='accepted'",(gid,a['profile_id']))) for gid in ids]
    if len({(g['origin'],g['document_id'],g['page']) for g in candidates})!=1:raise ValueError("每批字形须来自同一页或同一 AI 来源")
    signatures={g['id']:glyph_signature(g) for g in candidates}
    if candidates[0]['origin']=='inferred':
        verified,event=inference.verify_proposals(candidates,True)
        verified={g['id'] for g in candidates if g['id'] in verified and inference.inferred_recognizable(g['label'],str(DATA/g['image']),(DATA/g['image']).stat().st_mtime_ns)}
    else:
        p=page_record(candidates[0]['document_id'],candidates[0]['page'])
        proposed={i:{'confidence':1.,'decision':'accept','bbox':g['bbox'],'label':g['label'],'reason':''} for i,g in enumerate(candidates)}
        event=review_ai._verify_candidates(candidates,proposed,p,True,use_saved_crop=True)
        if event and event.get('format_warning'):raise ValueError("AI 字形复核结果不完整，原候选保留并阻止导出，可确认继续重试")
        verified={g['id'] for i,g in enumerate(candidates) if proposed[i]['decision']=='accept' and proposed[i].get('verification')}
    accepted=excluded=0
    for g in candidates:
        raw=one("SELECT * FROM glyphs WHERE id=?",(g['id'],));current=glyph_record(raw)
        if current['status']!='accepted' or glyph_signature(current)!=signatures[g['id']]:continue
        valid=g['id'] in verified;current['metrics']['automatic_verification']={'signature':signatures[g['id']],'accepted':valid,'event':event,'image_checked':True}
        with db() as c:
            changed=c.execute("UPDATE glyphs SET metrics=? WHERE id=? AND status='accepted' AND label=? AND bbox=? AND metrics=?",
                              (dumps(current['metrics']),g['id'],raw['label'],raw['bbox'],raw['metrics'])).rowcount
        if not changed:continue
        if valid:accepted+=1
        else:excluded+=1
    return jsonify(accepted=accepted,excluded=excluded,event=event)


@app.patch("/api/inferred/<gid>")
def review_inferred(gid):
    g=one("SELECT * FROM glyphs WHERE id=? AND origin='inferred'",(gid,));b=body()
    status=b.get("status","pending");label=str(b.get("label",g["label"]))
    if status not in ("accepted","pending","rejected"):raise ValueError("推测字形状态无效")
    if len(label)!=1 or label.isspace():raise ValueError("推测字形标签应为一个字符")
    execute("UPDATE glyphs SET label=?,status=? WHERE id=?",(label,status,gid))
    return jsonify(glyph_record(one("SELECT * FROM glyphs WHERE id=?",(gid,))))


@app.post("/api/profiles/<pid>/template")
def template(pid):
    one("SELECT * FROM profiles WHERE id=?",(pid,))
    chars=body().get("characters")
    if chars is not None:
        if not isinstance(chars,str):raise ValueError("补字列表须为字符串")
        chars=[c for c in chars if not c.isspace()]
    return jsonify(supplement_template(pid,chars))


def import_background(task,document_id):
    try:
        result=prepare_document(document_id,lambda p,m:task_update(task,"running",p,m))
        task_update(task,"complete",1,"本地预处理完成，候选等待后续处理",result)
    except Exception as error:
        if isinstance(error,ValueError):app.logger.warning("Import rejected: %s",error)
        else:app.logger.exception("Import failed")
        execute("UPDATE documents SET status='error',error=? WHERE id=?",(str(error),document_id))
        task_update(task,"error",0,str(error))


@app.post("/api/documents")
def upload():
    profile_id=request.form.get("profile_id");one("SELECT id FROM profiles WHERE id=?",(profile_id,))
    kind=request.form.get("kind")
    if kind not in ("sample","assignment"):raise ValueError("文件类型无效")
    upload=request.files.get("file")
    if not upload or not upload.filename:raise ValueError("请选择文件")
    filename=Path(upload.filename).name
    suffix=Path(filename).suffix.lower()
    if suffix not in (".pdf",".png",".jpg",".jpeg"):raise ValueError("支持 PDF、PNG、JPG")
    document_id=uid();relative=f"originals/{document_id}{suffix}"
    path=DATA/relative;upload.save(path)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    previous=rows("SELECT id,status FROM documents WHERE profile_id=? AND sha256=? AND kind=? AND status!='error'",(profile_id,digest,kind))
    if previous:
        path.unlink();return jsonify(document_id=previous[0]["id"],duplicate=True,status=previous[0]["status"])
    execute("INSERT INTO documents(id,profile_id,name,kind,original,sha256) VALUES(?,?,?,?,?,?)",
            (document_id,profile_id,filename,kind,relative,digest))
    task=uid();execute("INSERT INTO tasks(id,status,message) VALUES(?,'running','正在读取文件')",(task,))
    threading.Thread(target=import_background,args=(task,document_id),daemon=True).start()
    return jsonify(task_id=task,document_id=document_id),202


@app.get("/api/tasks/<task>")
def get_task(task):
    result=one("SELECT * FROM tasks WHERE id=?",(task,))
    if result["result"]:result["result"]=json.loads(result["result"])
    return jsonify(result)


@app.get("/api/documents/<document_id>")
def get_document(document_id):return jsonify(document_record(document_id))


@app.get("/api/glyphs")
def get_glyphs():
    filters=["profile_id=?"];values=[request.args.get("profile_id","")]
    for field in ("status","document_id","label"):
        if field in request.args and request.args[field]!="":filters.append(field+"=?");values.append(request.args[field])
    if request.args.get("page","")!="":filters.append("page=?");values.append(int(request.args["page"]))
    form=request.args.get("form")
    if form=="single":filters.append("length(label)=1")
    if form=="joined":filters.append("length(label)>1")
    if form=="unknown":filters.append("length(label)=0")
    clause=" AND ".join(filters)
    limit=max(1,min(120,int(request.args.get("limit",60))))
    offset=max(0,int(request.args.get("offset",0)))
    glyphs=rows("SELECT * FROM glyphs WHERE "+clause+" ORDER BY CASE WHEN note LIKE 'AI视觉%' THEN 0 ELSE 1 END,page,created,id LIMIT ? OFFSET ?",(*values,limit,offset))
    return jsonify(glyphs=review_ai.get_reviews([glyph_record(x) for x in glyphs]),total=one("SELECT COUNT(*) n FROM glyphs WHERE "+clause,values)["n"])


@app.post("/api/glyphs")
def add_glyph():
    b=body()
    return jsonify(save_glyph(b["document_id"],int(b["page"]),str(b.get("label","")),b["bbox"],
                              classification=b.get("classification","unknown"),note="用户手动框选的真实样本",
                              isolate_vectors=bool(b.get("isolate_vectors",False)),vector_sequences=b.get("vector_sequences")))


@app.patch("/api/glyphs/<gid>")
def update_glyph(gid):
    g=glyph_record(one("SELECT * FROM glyphs WHERE id=?",(gid,)));b=body()
    status=b.get("status",g["status"]);label=str(b.get("label",g["label"]))
    cls=b.get("classification",g["classification"])
    if cls not in ("handwriting","unknown","printed","correction"):raise ValueError("分类无效")
    if status not in ("pending","accepted","rejected","holdout"):raise ValueError("状态无效")
    if status in ("accepted","holdout") and (not label or cls!="handwriting"):
        raise ValueError("请先核对标签，并将来源确认为手写；印刷/批改/未知来源不能进入真实字库")
    baseline=b.get("baseline",g["metrics"]["baseline"])
    if not isinstance(baseline,(int,float)) or not .15<=baseline<=1.25:raise ValueError("基线比例应在 0.15–1.25 之间")
    sequences=b.get("vector_sequences",g["metrics"].get("source_path_sequence_numbers") if g["metrics"].get("vector_explicit_selection") else None)
    if b.get("isolate_vectors") is False:sequences=None
    result=save_glyph(g["document_id"],g["page"],label,b.get("bbox",g["bbox"]),
                      g["confidence"],cls,g["parent"],str(b.get("note",g["note"])),gid,baseline,status,
                      bool(b.get("isolate_vectors",g["metrics"].get("vector_isolated",False))),sequences)
    return jsonify(review_ai.get_reviews([result])[0])


@app.post("/api/glyphs/merge")
def merge_glyphs():
    b=body();ids=list(dict.fromkeys(b["ids"]))
    if not 2<=len(ids)<=20:raise ValueError("请选择 2–20 个相邻候选")
    gs=[glyph_record(one("SELECT * FROM glyphs WHERE id=?",(gid,))) for gid in ids]
    if len({(g["document_id"],g["page"]) for g in gs})!=1:raise ValueError("只能合并同一文件同一页的候选")
    bbox=[min(g["bbox"][0] for g in gs),min(g["bbox"][1] for g in gs),max(g["bbox"][2] for g in gs),max(g["bbox"][3] for g in gs)]
    result=save_glyph(gs[0]["document_id"],gs[0]["page"],str(b.get("label","")),bbox,
                      parent=",".join(ids),note="用户合并候选；待校对")
    with db() as c:c.executemany("UPDATE glyphs SET status='superseded' WHERE id=?",[(gid,) for gid in ids])
    return jsonify(result)


@app.post("/api/glyphs/<gid>/split")
def split_glyph(gid):
    g=glyph_record(one("SELECT * FROM glyphs WHERE id=?",(gid,)));parts=body()["parts"]
    if not isinstance(parts,list) or not 2<=len(parts)<=20:raise ValueError("拆分为 2–20 个可独立调整的区域")
    p=page_record(g["document_id"],g["page"])
    for part in parts:
        pt_box(part["bbox"],p["width"],p["height"])
        if not isinstance(part.get("label"),str):raise ValueError("每个拆分部分需要标签")
    result=[]
    try:
        for part in parts:
            result.append(save_glyph(g["document_id"],g["page"],part["label"],part["bbox"],parent=gid,note="用户拆分候选；待校对"))
    except Exception:
        with db() as c:c.executemany("DELETE FROM glyphs WHERE id=?",[(x["id"],) for x in result])
        raise
    execute("UPDATE glyphs SET status='superseded' WHERE id=?",(gid,))
    return jsonify(glyphs=result)


def default_answer_box(page):
    suggestions=page["meta"].get("suggested_regions") or []
    return max(suggestions,key=lambda r:(r[2]-r[0])*(r[3]-r[1])) if suggestions else [55,150,page["width"]-55,page["height"]-65]


@app.post("/api/assignments")
def create_assignment():
    b=body();pid=b["profile_id"];one("SELECT id FROM profiles WHERE id=?",(pid,))
    did=b.get("document_id");questions="";answers=[]
    if did:
        document=one("SELECT * FROM documents WHERE id=? AND profile_id=? AND kind='assignment' AND status='ready'",(did,pid))
        for p in document_record(did)["pages"]:
            questions+=f"【第 {p['number']+1} 页】\n"+" ".join(x["text"] for x in p["meta"]["printed"])+"\n\n"
            if not p["meta"]["printed"] and b.get("automatic") is not True:
                from extraction import local_ocr
                lines,_=local_ocr(DATA/p["image"])
                questions+="\n".join(x["text"] for x in lines)+"\n"
            bbox=default_answer_box(p)
            answers.append({"id":uid(),"page":p["number"],"bbox":bbox,"text":"","ast":parse_document(""),"seed":0,"reviewed":False,"source":"template"})
        title=document["name"]
    else:
        title="新建个人笔迹稿"
        answers=[{"id":uid(),"page":0,"bbox":[50,65,545,785],"text":"","ast":parse_document(""),"seed":0,"reviewed":False}]
    settings=validate_settings({"font_size":12,"line_spacing":1.4} if did and b.get("automatic") is True else {})
    aid=uid();execute("INSERT INTO assignments(id,profile_id,document_id,title,questions,settings,answers) VALUES(?,?,?,?,?,?,?)",
                     (aid,pid,did,title,questions,dumps(settings),dumps(answers)))
    return jsonify(assignment_record(aid))


@app.get("/api/assignments/<aid>")
def get_assignment(aid):return jsonify(assignment_record(aid))


@app.put("/api/assignments/<aid>")
def save_assignment(aid):
    current=assignment_record(aid);b=body()
    if b.get("revision")!=current["revision"]:return jsonify(error="另一窗口已修改此作业，请重新打开后保存，避免覆盖"),409
    answers=b["answers"]
    if not isinstance(answers,list) or len(answers)>480:raise ValueError("最多 480 个作答区域")
    page_dims=[(p["width"],p["height"]) for p in current["document"]["pages"]] if current["document"] else [(595.28,841.89)]
    seen=set()
    for answer in answers:
        page=int(answer["page"])
        if not 0<=page<len(page_dims):raise ValueError("页码超出原文档；默认保留原页数")
        answer["page"]=page;pt_box(answer["bbox"],*page_dims[page])
        answer["id"]=str(answer.get("id") or uid())
        if answer["id"] in seen:raise ValueError("区域 ID 重复")
        seen.add(answer["id"])
        try:
            answer["ast"]=parse_document(answer["text"]);answer.pop("parse_error",None)
        except ValueError as error:
            # Persist incomplete edits and AI drafts, but rendering will reject them.
            answer["ast"]=None;answer["parse_error"]=str(error);answer["reviewed"]=False
        answer["reviewed"]=bool(answer.get("reviewed",False)) and answer["ast"] is not None;answer["seed"]=int(answer.get("seed",0))
    settings=validate_settings(b.get("settings"));title=str(b.get("title",current["title"]))[:200]
    questions=str(b.get("questions",current["questions"]))
    if len(questions)>40000:raise ValueError("题目文本最多 40000 字符")
    with db() as c:
        updated=c.execute("UPDATE assignments SET title=?,questions=?,settings=?,answers=?,revision=revision+1,latest=NULL WHERE id=? AND revision=?",
                          (title,questions,dumps(settings),dumps(answers),aid,current["revision"]))
        if updated.rowcount!=1:raise ValueError("作业已被另一窗口更新，请重新打开")
    return jsonify(assignment_record(aid))


@app.post("/api/assignments/<aid>/render")
def render(aid):
    current=assignment_record(aid)
    result=render_assignment(aid,expected_revision=current["revision"])
    # A concurrent edit may produce a valid old snapshot; never mark it as current.
    execute("UPDATE assignments SET latest=? WHERE id=? AND revision=?",(result["id"],aid,current["revision"]))
    return jsonify(result)


render_lock=threading.Lock()
# ponytail: the in-process guard assumes one local server; use a DB lock for multiple workers.
active_renders={}


def render_background(task,aid,revision):
    metadata={"assignment_id":aid,"revision":revision}
    last_time=0;last_message="";last_progress=0
    def update(value,message):
        nonlocal last_time,last_message,last_progress
        now=time.monotonic();last_progress=max(last_progress,min(.99,value))
        if message!=last_message or now-last_time>=.35:
            task_update(task,"running",last_progress,message,metadata)
            last_time=now;last_message=message
    try:
        result=render_assignment(aid,progress=update,expected_revision=revision)
        with db() as c:
            changed=c.execute("UPDATE assignments SET latest=? WHERE id=? AND revision=?",(result["id"],aid,revision)).rowcount
        if not changed:raise ValueError("作业在生成期间发生变化，请重新生成预览")
        task_update(task,"complete",1,"预览已生成",{**metadata,"export_id":result["id"]})
    except Exception as error:
        if isinstance(error,(ValueError,KeyError,TypeError,IndexError)):message=str(error)
        else:
            app.logger.exception("Preview generation failed")
            message="本地预览生成失败，请查看启动终端；答案仍保留"
        task_update(task,"error",last_progress,message,metadata)
    finally:
        with render_lock:
            if active_renders.get(aid,{}).get("id")==task:active_renders.pop(aid,None)


@app.post("/api/assignments/<aid>/render/start")
def start_render(aid):
    current=assignment_record(aid);revision=body().get("revision")
    if type(revision) is not int or revision!=current["revision"]:
        return jsonify(error="作业已修改，请重新打开后生成预览"),409
    with render_lock:
        active=active_renders.get(aid)
        if active and one("SELECT status FROM tasks WHERE id=?",(active["id"],))["status"]!="running":
            active_renders.pop(aid,None);active=None
        if active:
            if active["revision"]!=revision:return jsonify(error="上一版预览仍在生成，请完成后再试"),409
            return jsonify(task_id=active["id"]),202
        task=uid();metadata={"assignment_id":aid,"revision":revision}
        execute("INSERT INTO tasks(id,status,progress,message,result) VALUES(?,'running',0,?,?)",
                (task,"正在准备预览",dumps(metadata)))
        active_renders[aid]={"id":task,"revision":revision}
        try:threading.Thread(target=render_background,args=(task,aid,revision),daemon=True).start()
        except Exception:
            active_renders.pop(aid,None);task_update(task,"error",0,"预览任务未能启动",metadata);raise
    return jsonify(task_id=task),202


@app.post("/api/format-preview")
def format_preview():
    texts=body().get("texts")
    if not isinstance(texts,list) or len(texts)>100 or not all(isinstance(x,str) for x in texts):
        raise ValueError("预览内容须为最多 100 项文字")
    result=[]
    for text in texts:
        try:
            ast=parse_document(text);ast["lines"]=unit_line_runs(ast["lines"]);result.append({"ast":ast})
        except ValueError as error:result.append({"error":str(error)})
    return jsonify(result)


@app.get("/api/config")
def get_config():return jsonify(cloud.config())


@app.put("/api/config")
def set_config():return jsonify(cloud.save_config(body()))


@app.get("/api/ai/glyph-review/plan")
def ai_review_plan():
    document_id=request.args.get("document_id","")
    page=request.args.get("page")
    if page is not None:page_record(document_id,int(page))
    return jsonify(review_ai.plan(document_id,int(page) if page is not None else None,request.args.get("retry_uncertain")=="1"))


@app.post("/api/ai/glyph-review")
def ai_review_glyphs():
    b=body()
    if b.get("verification_consent") is not True:
        raise ValueError("AI 校对流程已更新，包含独立复核；请先保存正在编辑的内容并刷新页面，再重新处理")
    return jsonify(review_ai.review_batch(b.get("document_id"),b.get("ids"),b.get("consent"),b.get("format_retry_consent") is True))


@app.post("/api/ai/solve")
def ai_solve():
    b=body();space=b.get("space")
    if space is not None:
        import math
        if not isinstance(space,list) or len(space)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or not 1<=v<=2000 for v in space):raise ValueError("作答空间无效")
    previous=b.get("previous_answer")
    if previous is not None and (not isinstance(previous,str) or len(previous)>30000):raise ValueError("旧答案无效")
    text,event=cloud.solve(str(b.get("questions","")),b.get("consent"),space,previous)
    return jsonify(text=text,event=event,reviewed=False)


@app.post("/api/ai/verify-solution")
def ai_verify_solution():
    b=body();question=b.get("question");answer=b.get("answer")
    if not isinstance(question,str) or not question.strip() or len(question)>15000 or not isinstance(answer,str) or not answer.strip() or len(answer)>30000:raise ValueError("题目或答案无效")
    result,event=cloud.verify_solution(question,answer,b.get("consent"))
    return jsonify(result=result,event=event)


@app.post("/api/ai/read-questions")
def ai_read_questions():
    b=body();d=one("SELECT * FROM documents WHERE id=? AND kind='assignment' AND status='ready'",(b["document_id"],))
    numbers=list(dict.fromkeys(int(n) for n in b["pages"]))
    pages=[page_record(d["id"],n) for n in numbers]
    result,event=cloud.recognize_questions(pages,b.get("consent"))
    return jsonify(pages=result["pages"],event=event)


@app.post("/api/ai/plan-question-page")
def ai_plan_question_page():
    b=body();d=one("SELECT id FROM documents WHERE id=? AND kind='assignment' AND status='ready'",(b["document_id"],))
    page=page_record(d["id"],int(b["page"]))
    previous=page_record(d["id"],page["number"]-1) if page["number"]>0 and b.get("verify") is True else None
    existing=b.get("existing",[])
    if not isinstance(existing,list) or len(existing)>12 or any(not isinstance(x,dict) or not isinstance(x.get("key"),str) or not isinstance(x.get("question"),str) or len(x["question"])>15000 for x in existing):raise ValueError("已保存题目无效")
    result,event=cloud.plan_question_page(page,b.get("consent"),previous,b.get("verify") is True,existing)
    if b.get("verify") is True:fit_answer_regions(page,result["items"])
    return jsonify(result=result,event=event)


def fit_answer_regions(page,items):
    """Use detected empty bands to bound AI placements; split a band for adjacent questions."""
    bands=page["meta"].get("suggested_regions") or []
    groups={}
    for item in items:
        q=item["question_box"]
        possible=[(i,b) for i,b in enumerate(bands) if b[1]>=q[1]-5 and b[3]>q[3]]
        if not possible:continue
        index,band=min(possible,key=lambda pair:abs(pair[1][1]-q[3]))
        groups.setdefault(index,[]).append(item)
    for index,group in groups.items():
        band=pt_box(bands[index],page["width"],page["height"])
        if len(group)==1:
            group[0]["answer_box"]=[band[0]+2,band[1]+2,band[2]-2,band[3]-2]
            continue
        xs=[(r["question_box"][0]+r["question_box"][2])/2 for r in group]
        ys=[(r["question_box"][1]+r["question_box"][3])/2 for r in group]
        axis=0 if max(xs)-min(xs)>max(40,max(ys)-min(ys)) else 1
        group.sort(key=lambda r:(r["question_box"][axis]+r["question_box"][axis+2])/2)
        start,end=band[axis],band[axis+2];step=(end-start)/len(group)
        for i,item in enumerate(group):
            rect=band.copy();rect[axis]=start+i*step+2;rect[axis+2]=start+(i+1)*step-2
            rect[1-axis]+=2;rect[3-axis]-=2
            item["answer_box"]=rect


@app.post("/api/assignments/<aid>/auto-regions")
def auto_regions(aid):
    b=body();current=assignment_record(aid);page_numbers=b.get("pages")
    if b.get("revision")!=current["revision"]:return jsonify(error="作业已修改，未覆盖当前区域"),409
    if not current["document"] or not isinstance(page_numbers,list) or len(page_numbers)>len(current["document"]["pages"]) or any(type(n) is not int or n<0 or n>=len(current["document"]["pages"]) for n in page_numbers):raise ValueError("自动检查的页码无效")
    changed=0
    for number in set(page_numbers):
        page=page_record(current["document_id"],number)
        regions=[r for r in current["answers"] if r["page"]==number and r.get("source")=="ai" and not r.get("user_edited") and r.get("question_bbox")]
        items=[{"id":r["id"],"question_box":r["question_bbox"],"answer_box":r["bbox"]} for r in regions]
        fit_answer_regions(page,items);by_id={item["id"]:item for item in items}
        for r in regions:
            box=by_id[r["id"]]["answer_box"]
            if box!=r["bbox"]:
                r.setdefault("previous_ai_positions",[]).append(r["bbox"])
                r["bbox"]=box;r["placement_pending"]=False;r["reviewed"]=False;changed+=1
    if changed:
        with db() as c:
            updated=c.execute("UPDATE assignments SET answers=?,revision=revision+1,latest=NULL WHERE id=? AND revision=?",(dumps(current["answers"]),aid,current["revision"]))
            if updated.rowcount!=1:return jsonify(error="作业刚被修改，未覆盖当前区域"),409
    return jsonify(assignment=assignment_record(aid),changed=changed)


@app.post("/api/assignments/<aid>/profile-fields")
def apply_profile_fields(aid):
    """Reuse only mutually consistent values from previously reviewed work in this profile."""
    import fitz
    b=body();current=assignment_record(aid)
    if b.get("revision")!=current["revision"]:return jsonify(error="作业已修改，未填入档案信息"),409
    if not current["document_id"]:raise ValueError("作业没有原题文件")
    values={"name":set(),"student_id":set()}
    aliases={"name":"name","姓名":"name","student_id":"student_id","学号":"student_id"}
    for old in rows("SELECT answers FROM assignments WHERE profile_id=? AND id<>?",(current["profile_id"],aid)):
        for r in json.loads(old["answers"]):
            key=aliases.get(str(r.get("question_key","")).lower())
            if key and r.get("reviewed") and r.get("text","").strip():values[key].add(r["text"].strip())
    source=one("SELECT original FROM documents WHERE id=?",(current["document_id"],))
    path=DATA/source["original"]
    if path.suffix.lower()!=".pdf":path=DATA/"pages"/current["document_id"]/"original.pdf"
    doc=fitz.open(path);added=[]
    for n,page in enumerate(doc):
        words=page.get_text("words")
        name=next((w for w in words if w[4].lower()=="name:"),None)
        student=next((w for w in words if w[4].lower()=="student"),None)
        sid=next((w for w in words if w[4].lower()=="id:" and student and abs(w[1]-student[1])<5),None)
        if not (name and student and sid):continue
        if any(r.get("source")=="profile" and r.get("page")==n for r in current["answers"]):continue
        for key,label,box in (("name","姓名",[name[2]+4,name[1]-12,student[0]-5,name[1]+8]),
                              ("student_id","学号",[sid[2]+4,sid[1]-12,min(page.rect.width-50,sid[2]+135),sid[1]+8])):
            text=next(iter(values[key])) if len(values[key])==1 else ""
            region={"id":uid(),"page":n,"bbox":pt_box(box,page.rect.width,page.rect.height),"text":text,
                    "ast":parse_document(text),"seed":0,"reviewed":False,"source":"profile","question_key":key,
                    "question":label,"ai_error":"档案中没有一致且已审核的"+label if not text else ""}
            added.append(region)
    doc.close()
    if added:
        current["answers"].extend(added);current["answers"].sort(key=lambda r:r["page"])
        with db() as c:
            updated=c.execute("UPDATE assignments SET answers=?,revision=revision+1,latest=NULL WHERE id=? AND revision=?",(dumps(current["answers"]),aid,current["revision"]))
            if updated.rowcount!=1:return jsonify(error="作业刚被修改，未填入档案信息"),409
    return jsonify(assignment=assignment_record(aid),added=len(added))


@app.post("/api/assignments/<aid>/apply-ai-revisions")
def apply_ai_revisions(aid):
    b=body();current=assignment_record(aid);changes=b.get("changes")
    if b.get("revision")!=current["revision"]:return jsonify(error="作业已修改，AI 修订未覆盖原内容"),409
    if not isinstance(changes,list) or not 1<=len(changes)<=12:raise ValueError("AI 修订数量无效")
    regions={r["id"]:r for r in current["answers"]};seen=set()
    for change in changes:
        if not isinstance(change,dict) or change.get("id") in seen:raise ValueError("AI 修订区域无效")
        r=regions.get(change.get("id"));seen.add(change.get("id"))
        if not r or r.get("source")!="ai" or r.get("user_edited") or r["text"]!=change.get("original"):
            raise ValueError("答案已被修改，AI 修订未覆盖原内容")
        text=change.get("text")
        if not isinstance(text,str) or not text.strip() or len(text)>30000:raise ValueError("AI 修订答案无效")
        r.setdefault("ai_previous_answers",[]).append(r["text"])
        r["text"]=text;r["reviewed"]=False;r["ai_event"]=change.get("event");r["verification_event"]=change.get("verification_event")
        try:r["ast"]=parse_document(text);r.pop("parse_error",None)
        except ValueError as error:r["ast"]=None;r["parse_error"]=str(error)
    with db() as c:
        updated=c.execute("UPDATE assignments SET answers=?,revision=revision+1,latest=NULL WHERE id=? AND revision=?",(dumps(current["answers"]),aid,current["revision"]))
        if updated.rowcount!=1:return jsonify(error="作业刚被修改，AI 修订未覆盖原内容"),409
    return jsonify(assignment=assignment_record(aid))


@app.post("/api/ai/refine-positions")
def ai_refine_positions():
    import re
    b=body();a=assignment_record(b["assignment_id"]);page=int(b.get("page",-1));export_id=b.get("export_id","")
    if b.get("revision")!=a["revision"]:return jsonify(error="作业已修改，未发送旧预览进行位置调整"),409
    if not a["document"] or not 0<=page<len(a["document"]["pages"]) or not re.fullmatch(r"[a-f0-9]{32}",str(export_id)):raise ValueError("预览或页码无效")
    folder=DATA/"exports"/export_id;record=folder/"audit.json";preview=folder/f"{page}.png"
    if not record.is_file() or not preview.is_file():raise ValueError("找不到当前页面预览")
    audit=json.loads(record.read_text())
    if audit.get("assignment_id")!=a["id"] or audit.get("revision")!=a["revision"]:raise ValueError("预览与当前作业版本不一致")
    issues=[x for x in audit.get("errors",[]) if x.startswith(f"第 {page+1} 页") and any(k in x for k in ("超出","压到原题","区域重叠","书写位置","碰撞"))]
    if not issues:raise ValueError("本页没有需要自动调整的作答位置")
    regions=[r for r in a["answers"] if r["page"]==page and r.get("source")=="ai" and r.get("question_key") and not r.get("user_edited")]
    result,event=cloud.refine_answer_positions(page_record(a["document_id"],page),preview,regions,issues,b.get("consent"))
    return jsonify(result=result,event=event)


@app.post("/api/assignments/<aid>/apply-ai-positions")
def apply_ai_positions(aid):
    b=body();current=assignment_record(aid);page=int(b.get("page",-1));items=b.get("positions")
    if b.get("revision")!=current["revision"]:return jsonify(error="作业已修改，AI 新位置未覆盖原内容"),409
    if not current["document"] or not 0<=page<len(current["document"]["pages"]) or not isinstance(items,list) or not 1<=len(items)<=12:raise ValueError("AI 位置结果无效")
    regions=[r for r in current["answers"] if r["page"]==page and r.get("source")=="ai" and r.get("question_key") and not r.get("user_edited")]
    ids=[i.get("id") for i in items if isinstance(i,dict)]
    if len(ids)!=len(items) or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids) or set(ids)!={r["id"] for r in regions}:raise ValueError("AI 位置结果遗漏或改变了题目，未修改作业")
    p=current["document"]["pages"][page];by_id={r["id"]:r for r in regions}
    for item in items:
        r=by_id[item["id"]];box=item.get("answer_box");note=item.get("note","")
        if not isinstance(note,str) or len(note)>500:raise ValueError("AI 位置说明无效")
        if box is not None:
            if not isinstance(box,list):raise ValueError("AI 位置坐标无效")
            r.setdefault("previous_ai_positions",[]).append(r["bbox"])
            r["bbox"]=pt_box(box,p["width"],p["height"]);r["placement_pending"]=False
        else:r["placement_pending"]=True
        r["placement_note"]=note;r["position_event"]=b.get("event");r["reviewed"]=False
    with db() as c:
        updated=c.execute("UPDATE assignments SET answers=?,revision=revision+1,latest=NULL WHERE id=? AND revision=?",(dumps(current["answers"]),aid,current["revision"]))
        if updated.rowcount!=1:return jsonify(error="作业刚被修改，AI 新位置未覆盖原内容"),409
    return jsonify(assignment=assignment_record(aid))


@app.post("/api/assignments/<aid>/apply-ai-page")
def apply_ai_page(aid):
    """Keep completed or manually edited regions; import AI results one page at a time."""
    b=body();current=assignment_record(aid)
    if b.get("revision")!=current["revision"]:return jsonify(error="作业已被其他窗口修改，AI 本页结果未覆盖原内容"),409
    if not current["document"]:raise ValueError("空白稿没有可自动定位的原题")
    page=int(b.get("page",-1));pages=current["document"]["pages"]
    if not 0<=page<len(pages):raise ValueError("AI 结果页码无效")
    items=b.get("items");blank=b.get("blank");page_error=b.get("page_error","")
    if not isinstance(page_error,str) or len(page_error)>1000 or not isinstance(items,list) or len(items)>12 or type(blank) is not bool or (blank and (items or page_error)) or (not blank and not items and not page_error):raise ValueError("AI 逐题结果不完整")
    p=pages[page];default_box=default_answer_box(p)
    def untouched_legacy(r):
        return r.get("source") is None and not r["text"].strip() and len(r["bbox"])==4 and all(abs(float(x)-float(y))<.1 for x,y in zip(r["bbox"],default_box))
    previous=[r for r in current["answers"] if r["page"]==page]
    if any(r.get("user_edited") or (r.get("source") not in ("template","ai","profile") and not untouched_legacy(r)) or (r["text"].strip() and (r.get("source")=="template" or not r.get("question_key"))) for r in previous):
        return jsonify(assignment=current,applied=0,skipped=True,reason="本页有人工编辑，已保留原内容")
    prior_ai={r.get("question_key"):r for r in previous if r.get("source")=="ai" and r.get("question_key")}
    keys=[i.get("key") for i in items if isinstance(i,dict)]
    if len(keys)!=len(items) or any(not isinstance(k,str) or not k or len(k)>100 for k in keys) or len(set(keys))!=len(keys):raise ValueError("AI 题号无效或重复")
    omitted=set(prior_ai)-set(keys)
    heading_keys={key for key in omitted if key and any(k.startswith(key+"(") for k in keys)
                  and any(prior_ai[key].get("question","") in item.get("question","") for item in items if item["key"].startswith(key+"("))}
    if not page_error and omitted-heading_keys:return jsonify(assignment=current,applied=0,skipped=True,reason="本页题目与已保存结果不一致，未覆盖")
    suggestions=p["meta"].get("suggested_regions") or []
    fallback=max(suggestions,key=lambda r:(r[2]-r[0])*(r[3]-r[1])) if suggestions else [0,0,p["width"],p["height"]]
    fallback=pt_box(fallback,p["width"],p["height"]);fresh=[];applied=0
    for item in items:
        key=item["key"];old=prior_ai.get(key)
        if old and old["text"].strip():
            headings=[prior_ai[k] for k in heading_keys if key.startswith(k+"(")]
            if headings:
                old["superseded_ai_headings"]=headings;old["question"]=item["question"];old["reviewed"]=False
            fresh.append(old);continue
        question=item.get("question");answer=item.get("answer","");note=item.get("note","");failure=item.get("solve_error","")
        if not isinstance(question,str) or not question.strip() or len(question)>15000 or not isinstance(answer,str) or len(answer)>30000 or not isinstance(note,str) or len(note)>500 or not isinstance(failure,str) or len(failure)>1000:
            raise ValueError("AI 题目、答案或说明无效")
        if not isinstance(item.get("question_box"),list):raise ValueError("AI 题目位置无效")
        qbox=pt_box(item["question_box"],p["width"],p["height"])
        proposed=item.get("answer_box");bbox=pt_box(proposed,p["width"],p["height"]) if proposed is not None else fallback
        region={"id":old["id"] if old else uid(),"page":page,"bbox":bbox,"text":answer,"seed":0,"reviewed":False,
                "source":"ai","question_key":key,"question":question,"question_bbox":qbox,
                "placement_pending":proposed is None,"placement_note":note,"ai_error":failure or ("AI 尚未生成本题答案" if not answer.strip() else ""),
                "ai_event":item.get("event"),"verification_event":item.get("verification_event"),"recognition_event":b.get("event")}
        headings=[prior_ai[k] for k in heading_keys if key.startswith(k+"(")]
        if headings:region["superseded_ai_headings"]=headings
        try:region["ast"]=parse_document(answer)
        except ValueError as error:region["ast"]=None;region["parse_error"]=str(error)
        fresh.append(region);applied+=1
    if blank:
        fresh=[{"id":uid(),"page":page,"bbox":fallback,"text":"","ast":parse_document(""),"seed":0,"reviewed":False,"source":"ai","ai_page_blank":True}]
    elif page_error:
        fresh=list(prior_ai.values())+[{"id":uid(),"page":page,"bbox":fallback,"text":"","ast":parse_document(""),"seed":0,"reviewed":False,"source":"ai","ai_page_error":True,"ai_error":page_error}]
    answers=[r for r in current["answers"] if r["page"]!=page or r.get("source")=="profile"]+fresh
    answers.sort(key=lambda r:r["page"])
    if len(answers)>480:raise ValueError("AI 作答区域超出上限；原内容未更改")
    with db() as c:
        updated=c.execute("UPDATE assignments SET answers=?,revision=revision+1,latest=NULL WHERE id=? AND revision=?",(dumps(answers),aid,current["revision"]))
        if updated.rowcount!=1:return jsonify(error="作业刚被修改，AI 本页结果未覆盖原内容"),409
    return jsonify(assignment=assignment_record(aid),applied=applied,skipped=False)


@app.post("/api/assignments/<aid>/apply-ai")
def apply_ai_drafts(aid):
    """Save AI drafts without replacing answers or question edits made in the meantime."""
    b=body();current=assignment_record(aid)
    recognized=b.get("questions","");initial=b.get("initial_questions","")
    if not isinstance(recognized,str) or len(recognized)>40000 or not isinstance(initial,str):raise ValueError("题目文本无效")
    suggestions=b.get("answers",[])
    if not isinstance(suggestions,list) or len(suggestions)>40:raise ValueError("AI 答案数量无效")
    applied=0
    for suggestion in suggestions:
        if not isinstance(suggestion,dict) or not isinstance(suggestion.get("page"),int) or not isinstance(suggestion.get("text"),str):
            raise ValueError("AI 答案格式无效")
        target=next((r for r in current["answers"] if r["page"]==suggestion["page"] and not r["text"].strip()),None)
        if not target or not suggestion["text"].strip():continue
        target["text"]=suggestion["text"]
        try:target["ast"]=parse_document(target["text"]);target.pop("parse_error",None)
        except ValueError as error:target["ast"]=None;target["parse_error"]=str(error)
        target["reviewed"]=False;target["source"]="ai";target["ai_event"]=suggestion.get("event")
        applied+=1
    question_replaced=bool(recognized) and current["questions"]==initial
    if question_replaced:current["questions"]=recognized
    with db() as c:
        updated=c.execute("UPDATE assignments SET questions=?,answers=?,revision=revision+1,latest=NULL WHERE id=? AND revision=?",
                          (current["questions"],dumps(current["answers"]),aid,current["revision"]))
        if updated.rowcount!=1:raise ValueError("作业刚被修改，请重试 AI 草稿导入")
    return jsonify(assignment=assignment_record(aid),applied=applied,question_replaced=question_replaced)


@app.post("/api/ai/recognize")
def ai_recognize():
    b=body();d=one("SELECT * FROM documents WHERE id=? AND kind='sample' AND status='ready'",(b["document_id"],))
    page_numbers=list(dict.fromkeys(int(n) for n in b["pages"]))
    pages=[page_record(d["id"],n) for n in page_numbers]
    result,event=cloud.recognize_pages(pages,b.get("consent"))
    count=0
    # Validate all normalized boxes and page references before mutating the library.
    import math
    if not isinstance(result,dict) or not isinstance(result.get("pages"),list):raise ValueError("AI 识别结果格式无效，保留本地候选")
    returned=[p.get("page") for p in result["pages"] if isinstance(p,dict)]
    if len(returned)!=len(result["pages"]) or len(returned)!=len(set(returned)) or set(returned)!=set(page_numbers):
        raise ValueError("AI 识别缺页或返回其他页码，保留本地候选")
    for page in result["pages"]:
        if not isinstance(page.get("lines"),list) or len(page["lines"])>500:raise ValueError("AI 识别行数据无效")
        for line in page["lines"]:
            if not isinstance(line,dict) or not isinstance(line.get("words"),list) or len(line["words"])>200:
                raise ValueError("AI 识别词框数据无效")
            for word in line["words"]:
                if not isinstance(word,dict) or not isinstance(word.get("text"),str):raise ValueError("AI 标签无效")
                box=word.get("bbox")
                if not isinstance(box,list) or len(box)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in box):
                    raise ValueError("AI 词框不是有效坐标")
                if not (0<=box[0]<box[2]<=1 and 0<=box[1]<box[3]<=1):raise ValueError("AI 词框必须为 0–1 的左上角坐标")
                confidence=float(word.get("confidence",.3))
                word["confidence"]=max(0,min(.7,confidence)) if math.isfinite(confidence) else 0
    for result_page in result.get("pages",[]):
        p=page_record(d["id"],result_page["page"])
        p["meta"].setdefault("local_ocr",p["meta"]["ocr"])
        p["meta"]["ocr"]=result_page["lines"]
        p["meta"]["cloud_event"]=event
        execute("UPDATE pages SET meta=? WHERE document_id=? AND number=?",(dumps(p["meta"]),d["id"],p["number"]))
        count+=extract_candidates(d["id"],p["number"],engine="AI视觉",include_unrecognized=False)
    return jsonify(candidates=count,event=event,message="云端结果作为新增待校对候选，未覆盖正式字库")


@app.get("/api/cloud-events")
def events():
    result=rows("SELECT * FROM cloud_events ORDER BY created DESC LIMIT 30")
    for event in result:event["usage"]=dumps(cloud.token_usage(json.loads(event["usage"] or "{}")))
    return jsonify(result)


@app.get("/api/compare/references")
def compare_references():return jsonify(comparison.references(request.args["profile_id"],request.args["export_id"]))


@app.post("/api/compare/prepare")
def prepare_compare():
    b=body();return jsonify(comparison.prepare(b["export_id"],b["reference_id"],b.get("region_id")))


@app.post("/api/compare/<cid>/run")
def run_compare(cid):
    b=body();return jsonify(comparison.run(cid,b.get("mode","vision"),str(b.get("reference_text","")),str(b.get("generated_text","")),b.get("consent")))


@app.get("/api/compare/history/<aid>")
def compare_history(aid):
    records=rows("SELECT * FROM comparisons WHERE assignment_id=? ORDER BY created DESC LIMIT 15",(aid,))
    for r in records:
        r["payload"]=json.loads(r["payload"]);r["result"]=json.loads(r["result"]) if r["result"] else None
    return jsonify(records)


@app.get("/files/glyph/<gid>")
def glyph_file(gid):return send_file(DATA/one("SELECT image FROM glyphs WHERE id=?",(gid,))["image"])


@app.get("/files/ai-proposal/<gid>")
def glyph_ai_proposal(gid):return send_file(review_ai.proposal_image(gid),mimetype="image/png")


@app.get("/files/page/<did>/<int:number>")
def page_file(did,number):return send_file(DATA/page_record(did,number)["image"])


@app.get("/files/ink/<did>/<int:number>")
def ink_file(did,number):return send_file(DATA/"pages"/did/f"{number}-ocr.png")


@app.get("/files/original/<did>")
def original_file(did):
    d=one("SELECT * FROM documents WHERE id=?",(did,))
    return send_file(DATA/d["original"],as_attachment=True,download_name=d["name"])


@app.get("/exports/<eid>/<filename>")
def export_file(eid,filename):
    import re
    if not re.fullmatch(r"[a-f0-9]{32}",eid):raise ValueError("导出 ID 无效")
    if filename not in ("result.pdf","template.pdf","audit.json","cells.json","reference.png","generated.png","comparison.json","cloud-result.json") and not re.fullmatch(r"\d+\.png",filename):
        raise ValueError("文件名无效")
    folder=DATA/"exports"/eid
    if filename=="result.pdf":
        report=json.loads((folder/"audit.json").read_text())
        if report["errors"]:raise ValueError("此预览未通过检查，禁止 PDF 导出")
    path=folder/filename
    if not path.is_file():return jsonify(error="文件尚未生成"),404
    return send_file(path,as_attachment=filename.endswith((".pdf",".json")),download_name="个人笔迹作业.pdf" if filename=="result.pdf" else filename)


if __name__=="__main__":
    # A killed import is reported explicitly, rather than looking active forever.
    execute("UPDATE tasks SET status='error',message='上次任务被中断，请重新开始' WHERE status='running'")
    execute("UPDATE documents SET status='error',error='上次处理被中断，可重新上传原文件' WHERE status='processing'")
    port=int(os.environ.get("PORT","8765"))
    print(f"个人笔迹工作台：http://127.0.0.1:{port}",flush=True)
    app.run(host="127.0.0.1",port=port,debug=False,threaded=True)
