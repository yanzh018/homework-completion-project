"""Exercise automatic layout and review safeguards without using a network."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
temp=tempfile.TemporaryDirectory(prefix="ink-auto-workflow-")
os.environ["HANDWRITING_DATA"]=temp.name
import fitz
from PIL import Image
from app import app,fit_answer_regions,TOKEN
from cloud import plan_question_page,verify_solution
from inference import draw_shape,inferred_recognizable
from rendering import Composer,parse_document,validate_settings
from storage import DATA,dumps,execute

event={"event_id":"mock","usage":{},"service":"local","model":"mock"}
page={"number":2,"width":600,"height":800,"image":"page.png","meta":{"suggested_regions":[[60,105,540,290],[60,350,540,780]],
      "printed":[{"text":"(2)","bbox":[80,80,100,90],"font":"TimesNewRomanPSMT"}]}}
Image.new("RGB",(600,800),"white").save(DATA/"page.png")
items=[{"question_box":[80,70,220,100],"answer_box":[60,120,500,170]},
       {"question_box":[80,310,220,345],"answer_box":[60,360,250,700]},
       {"question_box":[310,310,440,345],"answer_box":[310,360,500,700]}]
fit_answer_regions(page,items)
assert items[0]["answer_box"]==[62,107,538,288]
assert items[1]["answer_box"][2]<items[2]["answer_box"][0]
assert items[1]["answer_box"][1]==items[2]["answer_box"][1]==352

incomplete={"page":2,"blank":False,"items":[{"key":"a","question":"x","question_box":[.1,.4,.4,.45],"answer_box":[.1,.5,.9,.8]}]}
with patch("cloud.chat",return_value=(json.dumps(incomplete),event)):
    try:plan_question_page(page,True,verify=True)
    except ValueError as error:assert "(2)" in str(error)
    else:raise AssertionError("a missed printed subquestion must block completion")

with patch("cloud.chat",return_value=(json.dumps({"ok":True,"answer":"changed","note":""}),event)):
    checked,_=verify_solution("question","original",True)
    assert checked["answer"]=="original", "a supposedly unchanged verification must preserve the answer"

with patch("cloud.chat",side_effect=[("not JSON",event),('{"ok":"yes"}',event),(json.dumps({"ok":True,"answer":"","note":""}),event)]) as request:
    checked,_=verify_solution("question","original",True)
    assert checked["answer"]=="original" and request.call_count==3
with patch("cloud.chat",return_value=("not JSON",event)) as request:
    try:verify_solution("question","original",True)
    except ValueError as error:assert "未将该题标记为完成" in str(error) and request.call_count==3
    else:raise AssertionError("repeated invalid reviews must never be accepted")

complete={"page":2,"blank":False,"items":[
    {"key":"1","question":"Use the theorem for the following functions.","question_box":[.1,.05,.8,.09],"answer_box":[.1,.12,.9,.3]},
    {"key":"1(2)","question":"(2) $f(x)=x$", "question_box":[.12,.095,.5,.13],"answer_box":[.1,.15,.9,.3]}]}
with patch("cloud.chat",return_value=(json.dumps(complete),event)):
    plan,_=plan_question_page(page,True,verify=True)
    assert len(plan["items"])==1 and "following functions" in plan["items"][0]["question"]

settings=validate_settings({})
expr=parse_document(r"$A\cup B\iff x\in A$")["lines"][0]["runs"][0]["body"]
composer=Composer("missing-profile",0,settings);box=composer.math(expr,12)
assert any(op.get("role")=="set_operator" for op in box.ops)
assert any(op.get("role")=="equivalence" for op in box.ops)
assert not ({"∪","⇔"}&composer.missing)

for label,commands,expected in [
    ("z",[["M",25,40],["L",75,40],["C",65,65,50,85,25,104]],False),
    ("z",[["M",30,42],["L",70,42],["L",30,100],["L",75,100]],True),
    ("≤",[["M",78,44],["L",20,74],["L",78,104],["M",20,120],["L",80,120]],True),
    ("≤",[["M",22,40],["L",74,58],["L",22,76],["M",24,102],["L",76,102]],False),
    ("≥",[["M",22,40],["L",80,70],["L",22,100],["M",20,120],["L",80,120]],True),
    ("≥",[["M",76,40],["L",24,58],["L",76,76],["M",22,102],["L",78,102]],False),
    ("ℝ",[["M",30,104],["L",30,30],["L",60,45],["L",42,68],["L",65,104]],False)]:
    path=DATA/("shape-"+str(expected)+label+".png")
    draw_shape({"label":label,"paths":[{"width":3,"commands":commands}]},path)
    assert inferred_recognizable(label,str(path),path.stat().st_mtime_ns) is expected

doc=fitz.open();p=doc.new_page(width=600,height=800)
p.insert_text((70,45),"Name:");p.insert_text((245,45),"Student ID:")
doc.save(DATA/"originals"/"a.pdf");doc.close()
execute("INSERT INTO profiles(id,name) VALUES('p','test')")
execute("INSERT INTO documents(id,profile_id,name,kind,original,sha256,status) VALUES('d','p','a.pdf','assignment','originals/a.pdf','hash','ready')")
execute("INSERT INTO pages(document_id,number,width,height,image,ink,meta) VALUES('d',0,600,800,'page.png','page.png',?)",(dumps({"suggested_regions":[[60,105,540,290]]}),))
old=[{"id":"n","question_key":"name","text":"Example Name","reviewed":True},{"id":"s","question_key":"student_id","text":"123456","reviewed":True}]
execute("INSERT INTO assignments(id,profile_id,document_id,title,settings,answers) VALUES('old','p','d','old',?,?)",(dumps(settings),dumps(old)))
draft=[{"id":"q","page":0,"bbox":[60,100,540,300],"question_bbox":[80,70,200,100],"text":"$x=1$","ast":parse_document("$x=1$"),"seed":0,"reviewed":False,"source":"ai","question_key":"1"}]
execute("INSERT INTO assignments(id,profile_id,document_id,title,settings,answers) VALUES('new','p','d','new',?,?)",(dumps(settings),dumps(draft)))
with app.test_client() as client:
    headers={"X-Local-Token":TOKEN}
    response=client.post("/api/assignments/new/profile-fields",json={"revision":1},headers=headers)
    assert response.status_code==200,response.json
    a=response.json["assignment"];assert response.json["added"]==2
    assert {r["text"] for r in a["answers"] if r["source"]=="profile"}=={"Example Name","123456"}
    stale=client.post("/api/assignments/new/apply-ai-revisions",json={"revision":a["revision"],"changes":[{"id":"q","original":"wrong","text":"$x=2$"}]},headers=headers)
    assert stale.status_code==400
    ok=client.post("/api/assignments/new/apply-ai-revisions",json={"revision":a["revision"],"changes":[{"id":"q","original":"$x=1$","text":"$x=2$"}]},headers=headers)
    assert ok.status_code==200 and ok.json["assignment"]["answers"][0]["ai_previous_answers"]==["$x=1$"]
    a=ok.json["assignment"]
    fitted=client.post("/api/assignments/new/auto-regions",json={"revision":a["revision"],"pages":[0]},headers=headers)
    q=fitted.json["assignment"]["answers"][0]
    assert fitted.json["changed"]==1 and q["bbox"]==[62,107,538,288] and q["previous_ai_positions"]==[[60,100,540,300]]
print("PASS bounded auto layout, missed-question guard, independent verification, profile fields and protected AI revisions")
