"use strict";
const $=id=>document.getElementById(id);
const token=document.querySelector('meta[name="local-token"]').content;
let state={},profile="",view="library",coverage={},reviewDoc=null,reviewPage=0;
let glyphs=[],glyphTotal=0,glyphOffset=0,activeGlyph=null,editBox=null,selected=new Set(),inkOnly=false;
let assignment=null,regionId="",report=null,showResult=false,dirty=false,editVersion=0,saveTimer=null,savePromise=null;
let comparisonState=null;
let processingAI=false,stopAiReview=false;
let readableTimer,readableVersion=0,editorRegionId="",renderWatch=null,renderStarting=false;
const declinedAiSuggestions=new Set();
const documentCache=new Map(),imageCache=new Map();
const statusNames={pending:"待校对",accepted:"真实已入库",holdout:"独立对照",rejected:"已排除",superseded:"已合并/拆分"};
let toastTimer;
function toast(message,error=false){$("toast").textContent=message;$("toast").className="show"+(error?" error":"");clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("toast").className="",error?8000:4200)}
async function api(path,method="GET",value){
  const options={method,headers:{"X-Local-Token":token}};
  if(value!==undefined){if(value instanceof FormData)options.body=value;else{options.headers["Content-Type"]="application/json";options.body=JSON.stringify(value)}}
  const response=await fetch(path,options);let result;
  try{result=await response.json()}catch{throw Error("服务器未返回有效数据，请检查启动终端")}
  if(!response.ok)throw Error(result.error||`请求失败 ${response.status}`);return result;
}
function on(id,event,fn){$(id).addEventListener(event,async e=>{try{await fn(e)}catch(error){toast(error.message,true)}})}
function node(tag,text,className){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e}
const mathNamespace="http://www.w3.org/1998/Math/MathML";
const mathFunctions=new Set(["sin","cos","tan","arcsin","arccos","arctan","sec","csc","cot","sinh","cosh","tanh","ln","log","exp","lim","det","max","min"]);
function mathElement(tag,...children){const e=document.createElementNS(mathNamespace,tag);for(const c of children)e.append(c);return e}
function mathNode(n){
  if(n.type==="text"){const e=mathElement("mtext");e.textContent=n.value;return e}
  if(n.type==="char"){
    const tag=/^[0-9]$/.test(n.value)?"mn":/^[a-zA-Zα-ωΑ-Ωℂℍℕℙℚℝℤ]$/.test(n.value)?"mi":"mo";
    const e=mathElement(tag);e.textContent=n.value;return e;
  }
  if(n.type==="seq"){
    const name=n.children.every(c=>c.type==="char")?n.children.map(c=>c.value).join(""):"";
    if(mathFunctions.has(name)){const e=mathElement("mi");e.textContent=name;e.setAttribute("mathvariant","normal");return e}
    return mathElement("mrow",...n.children.map(mathNode));
  }
  if(n.type==="frac")return mathElement("mfrac",mathNode(n.numerator),mathNode(n.denominator));
  if(n.type==="sqrt")return n.index?mathElement("mroot",mathNode(n.body),mathNode(n.index)):mathElement("msqrt",mathNode(n.body));
  if(n.type==="primes"){const e=mathElement("mo");e.textContent="′".repeat(n.count);return e}
  if(n.type==="space"){const e=mathElement("mspace");e.setAttribute("width",`${n.size}em`);return e}
  if(n.type==="scripts"){
    if(n.sub&&n.sup)return mathElement("msubsup",mathNode(n.base),mathNode(n.sub),mathNode(n.sup));
    return mathElement(n.sub?"msub":"msup",mathNode(n.base),mathNode(n.sub||n.sup));
  }
  if(n.type==="delimited"){
    const left=mathElement("mo"),right=mathElement("mo");left.textContent=n.left;right.textContent=n.right;
    return mathElement("mrow",left,mathNode(n.body),right);
  }
  if(n.type==="vector"){
    const arrow=mathElement("mo");arrow.textContent="→";arrow.setAttribute("stretchy","true");
    const result=mathElement("mover",mathNode(n.body),arrow);result.setAttribute("accent","true");return result;
  }
  if(n.type==="matrix"){
    const table=mathElement("mtable",...n.rows.map(row=>mathElement("mtr",...row.map(cell=>mathElement("mtd",mathNode(cell))))));
    if(n.bracket==="matrix")return table;
    const left=mathElement("mo"),right=mathElement("mo");left.textContent=n.bracket==="bmatrix"?"[":"(";right.textContent=n.bracket==="bmatrix"?"]":")";
    return mathElement("mrow",left,table,right);
  }
  throw Error("无法显示此公式结构");
}
function appendReadableText(element,text){
  const re=/\*\*([^*]+)\*\*|__([^_]+)__|`([^`]+)`/g;let from=0,match;
  while((match=re.exec(text))){element.append(document.createTextNode(text.slice(from,match.index)));element.append(node(match[3]!==undefined?"code":"strong",match[1]||match[2]||match[3]));from=re.lastIndex}
  element.append(document.createTextNode(text.slice(from)));
}
function hasFormattedText(text){return /(^|\n)(#{1,6}|[-*])\s+|\*\*[^*]+\*\*|__[^_]+__|`[^`]+`/.test(text)}
function showReadable(target,text,parsed){
  target.replaceChildren();
  if(!text.trim())return;
  if(parsed.error){target.append(node("span","部分内容无法排版，请展开原文检查："+parsed.error,"readable-warning"));target.append(document.createTextNode(text));return}
  try{
    for(const line of parsed.ast.lines){
      const row=node("div",undefined,"readable-line");let first=true;
      for(const run of line.runs){
        if(run.type==="math"){const m=mathElement("math",mathNode(run.body));m.setAttribute("displaystyle","true");row.append(m)}
        else{let value=run.value;if(first)value=value.replace(/^#{1,6}\s+/,"").replace(/^[-*]\s+/,"• ");appendReadableText(row,value)}
        first=false;
      }
      target.append(row);
    }
  }catch(error){target.replaceChildren(node("span","公式暂时无法显示，请展开原文检查。","readable-warning"),document.createTextNode(text))}
}
async function formatReadable(entries){
  for(let start=0;start<entries.length;start+=100){
    const batch=entries.slice(start,start+100),results=await api("/api/format-preview","POST",{texts:batch.map(x=>x.text)});
    batch.forEach((entry,i)=>{if(entry.target.isConnected&&(!entry.valid||entry.valid()))showReadable(entry.target,entry.text,results[i])});
  }
}
function scheduleReadable(){
  clearTimeout(readableTimer);const version=++readableVersion;
  const valid=()=>version===readableVersion;
  const entries=[{target:$("questionReadable"),text:$("regionQuestion").value,valid},{target:$("answerReadable"),text:$("answerText").value,valid}];
  readableTimer=setTimeout(()=>formatReadable(entries).catch(()=>{if(valid())for(const e of entries)showReadable(e.target,e.text,{error:"本地预览暂不可用"})}),150);
}
function button(text,fn,cls="secondary"){const b=node("button",text,cls);b.addEventListener("click",async()=>{try{b.disabled=true;await fn()}catch(e){toast(e.message,true)}finally{b.disabled=false}});return b}
function options(element,items,value){element.replaceChildren();for(const item of items){const op=node("option",item.label);op.value=item.value;element.append(op)}if(items.some(x=>String(x.value)===String(value)))element.value=value}
function requireProfile(){if(!profile)throw Error("请先在左侧新建使用者档案")}
async function ensureProfile(){
  if(profile)return;
  const created=await api("/api/profiles","POST",{name:"我的笔迹"});
  await refreshState();await selectProfile(created.id);
}
function currentRegion(){return assignment?.answers.find(r=>r.id===regionId)}
function profileDocuments(kind){return(state.documents||[]).filter(d=>d.profile_id===profile&&d.kind===kind)}
function profileAssignments(){return(state.assignments||[]).filter(a=>a.profile_id===profile)}
function newId(){return crypto.randomUUID().replaceAll("-","")}

async function textDialog(title,help,value=""){
  $("textDialogTitle").textContent=title;$("textDialogHelp").textContent=help;$("textDialogInput").value=value;
  const d=$("textDialog");d.returnValue="cancel";d.showModal();$("textDialogInput").focus();
  return new Promise(resolve=>d.addEventListener("close",()=>resolve(d.returnValue==="ok"?$("textDialogInput").value:null),{once:true}));
}
async function switchView(next){
  view=next;localStorage.setItem("ink-view",next);
  if(!processingAI&&next!=="library")$("taskPanel").classList.add("hidden");
  for(const e of document.querySelectorAll(".view"))e.classList.toggle("active",e.id===`view-${next}`);
  for(const e of document.querySelectorAll(".nav-item"))e.classList.toggle("active",e.dataset.view===next);
  $("moreTools").open=["review","settings"].includes(next);
  $("breadcrumb").textContent="工作台 / "+({library:"准备笔迹",review:"检查字形",compose:"AI 处理作业",preview:"预览与导出",settings:"AI 服务设置"}[next]);
  window.scrollTo(0,0);
  if(next==="review"){if(!reviewDoc)await loadReviewDocument();else drawSource()}
  if(next==="preview"){paintReviewOverview();drawAssignment()}
  if(next==="settings")await loadEvents();
}
async function refreshState(){state=await api("/api/state");documentCache.clear()}
async function selectProfile(pid){
  if(dirty)await saveAssignment();profile=pid;localStorage.setItem("ink-profile",pid);reviewDoc=null;activeGlyph=null;editBox=null;selected.clear();assignment=null;report=null;dirty=false;
  options($("profileSelect"),state.profiles.map(p=>({value:p.id,label:p.name})),pid);
  if(!state.profiles.length)options($("profileSelect"),[{value:"",label:"开始时自动创建档案"}],"");
  paintDocumentLists();await loadCoverage();$("sampleDetails").open=!coverage.accepted&&profileDocuments("sample").length>0;await loadReviewDocument();
  const saved=localStorage.getItem("ink-assignment-"+profile);const first=profileAssignments().find(a=>a.id===saved)||profileAssignments()[0];
  if(first)await loadAssignment(first.id);else paintAssignmentEmpty();
}
async function getDocument(id){if(!id)return null;if(!documentCache.has(id))documentCache.set(id,await api("/api/documents/"+id));return documentCache.get(id)}
function paintDocumentLists(){
  const samples=profileDocuments("sample");const list=$("sampleList");list.replaceChildren();
  if(!samples.length)list.append(node("div","还没有样本。上传亲笔完成的旧作业，建立第一份字库。","empty"));
  for(const d of samples){
    const card=node("div",undefined,"document");card.append(node("div",d.name.toLowerCase().endsWith(".pdf")?"PDF":"IMAGE","file-symbol"));
    const content=node("div",undefined,"document-body");content.append(node("h3",d.name),node("p",d.status==="ready"?`${d.accepted} 个真实字形已入库 · ${d.pending} 个候选待处理或未确定`:d.status==="error"?"处理失败："+d.error:"正在处理…"));card.append(content);
    if(d.status==="ready"){
      card.append(button("确认继续 AI 处理",()=>resumeSample(d.id)));
      card.append(button("查看字形",async()=>{$("labelFilter").value="";$("formFilter").value="";$("statusFilter").value="pending";$("reviewFilters").open=false;$("reviewDocument").value=d.id;await loadReviewDocument(d.id);await switchView("review")},"quiet"));
    }
    const link=node("a","原文件","quiet");link.href="/files/original/"+d.id;card.append(link);list.append(card);
  }
  const old=$("reviewDocument").value;
  options($("reviewDocument"),samples.filter(d=>d.status==="ready").map(d=>({value:d.id,label:d.name})),old);
  if(!$("reviewDocument").options.length)options($("reviewDocument"),[{value:"",label:"尚无可校对样本"}],"");
  const assignments=profileAssignments();options($("assignmentSelect"),assignments.map(a=>({value:a.id,label:a.title})),assignment?.id);
  if(!assignments.length)options($("assignmentSelect"),[{value:"",label:"尚无作业，上传或新建"}],"");
  $("assignmentPicker").classList.toggle("hidden",!assignments.length);
}
async function loadCoverage(){
  coverage=profile?await api(`/api/profiles/${profile}/coverage`):{characters:{},accepted:0,covered:0,total:0,pending:0,holdout:0,style:{},ligatures:{}};
  $("acceptedCount").textContent=coverage.accepted;$("coveredCount").replaceChildren(document.createTextNode(coverage.covered+" "),node("em","/ "+(profile?coverage.total:"—")));
  $("pendingCount").textContent=coverage.pending;$("holdoutCount").textContent=coverage.holdout;
  $("railCoverage").textContent=profile?`已覆盖 ${coverage.covered} / ${coverage.total} 个字符`:"上传样本开始建立笔迹";
  $("coverageBar").style.width=(coverage.total?coverage.covered/coverage.total*100:0)+"%";
  const samples=profileDocuments("sample"),cloud=state.capabilities?.cloud,cloudReady=cloud?.enabled&&cloud?.has_key&&cloud?.model&&cloud?.supports_images;
  $("libraryNextTitle").textContent=coverage.accepted?`已有 ${coverage.accepted} 个真实字形，可以开始处理作业`:samples.length?"样本已保存，尚无可用字形":"先上传一份旧作业";
  $("libraryNextHint").textContent=coverage.accepted?(cloudReady?"接下来上传新作业；AI 会识题、解答并安排位置。":"请先配置 AI，再继续处理作业。"):samples.length?(cloudReady?"展开已保存的笔迹，选择“继续 AI 处理”；也可先处理作业。":"先配置 AI 服务，再继续处理样本。"):cloudReady?"AI 会整理手写样本，可靠的结果自动入库。":"先配置 AI，以便自动整理上传的笔迹。";
  const grid=$("coverageGrid");grid.replaceChildren();
  for(const[c,n]of Object.entries(coverage.characters)){const e=node("button",undefined,"coverage-cell"+(n?" covered":""));e.append(document.createTextNode(c),node("small",n?`${n} 版`:"缺失"));e.title=c+"："+(n?`${n} 个真实样本`:"缺字");e.addEventListener("click",async()=>{$("labelFilter").value=c;$("reviewFilters").open=true;$("statusFilter").value=n?"accepted":"pending";glyphOffset=0;await switchView("review");await loadGlyphs()});grid.append(e)}
  $("ligatureInfo").textContent="已确认连笔 / 整词："+(Object.entries(coverage.ligatures).map(([x,n])=>`${x} ×${n}`).join(" · ")||"尚无");
  const stats=$("styleStats");stats.replaceChildren();
  for(const[key,title,unit]of [["height","样本字高","pt"],["stroke_width","笔画粗细","pt"],["slant_degrees","倾斜估计","°"],["letter_gap","字距估计","pt"],["word_gap","词距估计","pt"],["line_gap","行距估计","pt"]]){
    const m=coverage.style[key],e=node("div",title);e.append(node("strong",m?`${m.median.toFixed(1)} ${unit}`:"待补样本"));
    if(m?.estimated)e.append(node("small","样本不足，使用可调默认值"));stats.append(e);
  }
  await loadInferred();paintMissingAction();
}

async function resumeSample(documentId){
  const d=await getDocument(documentId);
  if(d.pages.some(p=>!p.meta.cloud_event)){
    const result=await runAiSample(documentId,true);
    if(!result||result.failed)return;
  }
  await runAiGlyphReview(documentId,null,false);
}
async function uploadFile(file,kind){
  if(!file)return;await ensureProfile();const form=new FormData();form.append("file",file);form.append("kind",kind);form.append("profile_id",profile);
  $("taskPanel").classList.remove("hidden");$("taskMessage").textContent="上传并读取文件…";$("taskProgress").value=0;
  const started=await api("/api/documents","POST",form);let result;
  if(started.task_id){
    while(true){const task=await api("/api/tasks/"+started.task_id);$("taskMessage").textContent=task.message;$("taskProgress").value=task.progress;$("taskPercent").textContent=Math.round(task.progress*100)+"%";
      if(task.status==="complete"){result=task.result;break}if(task.status==="error")throw Error(task.message);await new Promise(r=>setTimeout(r,1300));}
  }else if(started.status!=="ready")throw Error("该文件正在处理，请稍后刷新");
  await refreshState();paintDocumentLists();await loadCoverage();
  if(kind==="sample"){await loadReviewDocument(started.document_id);await switchView("library");toast(started.duplicate?"样本已存在，可继续 AI 处理":`样本已导入：${result?.candidates||0} 个候选等待处理`)}
  else{
    const a=await api("/api/assignments","POST",{profile_id:profile,document_id:started.document_id,automatic:true});await refreshState();paintDocumentLists();await loadAssignment(a.id);
    await switchView("compose");toast("题目已导入，AI 答案和题目转录均需核对");
  }
  if(!started.duplicate||kind==="assignment"){
    const recognized=kind==="assignment"?await runAiAssignment(started.document_id,false):await runAiSample(started.document_id,false);
    const cfg=await api("/api/config");
    if(kind==="sample"&&recognized?.completed&&!recognized.failed&&cfg.auto_review)await runAiGlyphReview(started.document_id,null,false);
  }
}

function pendingAiPages(d){
  if(!d||!assignment)return [];
  return d.pages.filter(p=>{
    const regions=assignment.answers.filter(r=>r.page===p.number);
    const candidates=p.meta.suggested_regions||[],area=r=>(r[2]-r[0])*(r[3]-r[1]);
    const originalBox=candidates.length?candidates.reduce((a,b)=>area(a)>=area(b)?a:b):[55,150,p.width-55,p.height-65];
    const legacy=r=>r.source==null&&!r.text.trim()&&r.bbox.length===4&&r.bbox.every((v,i)=>Math.abs(v-originalBox[i])<.1);
    if(regions.some(r=>r.user_edited||(r.source!=="ai"&&r.source!=="template"&&r.source!=="profile"&&!legacy(r))||(r.text.trim()&&(r.source==="template"||!r.question_key))))return false;
    return !regions.length||regions.some(r=>legacy(r)||r.source==="template"||r.ai_error||r.placement_pending)||!!report?.errors.some(e=>e.startsWith(`第 ${p.number+1} 页`)&&isLayoutIssue(e));
  });
}
function paintAssignmentAction(){
  if(!assignment)return;
  const pending=assignment.document_id&&pendingAiPages(assignment.document).length;
  const c=state.capabilities?.cloud,ready=c?.enabled&&c?.has_key&&c?.model&&c?.supports_images;
  const hasAnswer=assignment.answers.some(r=>r.text.trim());
  const problems=report?.errors.filter(e=>!e.includes("此区域答案尚未勾选审阅确认"))||[];
  const needsPosition=pending&&problems.some(isLayoutIssue)&&!assignment.answers.some(r=>r.ai_error||r.placement_pending||r.source==="template");
  $("assignmentName").textContent=assignment.title;
  $("assignmentNextHint").textContent=needsPosition?"预览发现位置或空间问题，可确认继续自动处理。":pending?(hasAnswer?"部分内容已保存，可继续处理或查看已有结果。":"作业已保存，等待 AI 处理。"):problems.length?`预览还有 ${problems.length} 项需处理，查看结果了解详情。`:(hasAnswer?"作业已保存，可以查看结果并审核。":"作业已保存，等待 AI 生成答案。");
  $("goPreview").hidden=!hasAnswer;$("assignmentAiCta").hidden=!pending;
  $("aiStartAssignment").hidden=!ready;$("aiSetupAssignment").hidden=!!ready;
  $("assignmentAiTitle").textContent=needsPosition?"作答位置需要调整":"本作业还有页面未处理";
  $("aiStartAssignment").textContent=needsPosition?"确认继续自动处理":"确认继续 AI 处理";
  $("assignmentAiHint").textContent=ready?(needsPosition?"AI 会重新检查页面并安排位置，已有答案会保留。":"继续自动识题、解答和安排位置，已完成的内容会保留。 "):"AI 服务尚未接通，请先配置服务再继续。";
}
async function verifiedAiAnswer(question,box,previous=""){
  const space=box?[Math.max(1,box[2]-box[0]),Math.max(1,box[3]-box[1])]:null;
  const solved=await api("/api/ai/solve","POST",{questions:question,space,previous_answer:previous||null,consent:true});
  let answer=solved.text,verification;
  for(let i=0;i<2;i++){
    verification=await api("/api/ai/verify-solution","POST",{question,answer,consent:true});
    if(verification.result.ok)return {text:answer,event:solved.event,verification_event:verification.event};
    answer=verification.result.answer;
  }
  throw Error("AI 两次复核仍未确认答案，请继续处理并检查题意");
}
async function runAiAssignment(documentId,force,restart=false){
  if(processingAI)throw Error("另一项 AI 任务正在处理，请先等待完成");
  if(!assignment||assignment.document_id!==documentId)throw Error("请先打开对应作业");
  if(dirty)await saveAssignment();
  const cfg=await api("/api/config"),d=await getDocument(documentId);let targetId=assignment.id;
  if(!force&&!cfg.prefer_ai){$("taskMessage").textContent="已选择本地备选，可手动输入答案。";return}
  if(!cfg.enabled||!cfg.has_key||!cfg.model||!cfg.supports_images){
    if(force)throw Error("请先在 AI 服务设置中启用支持图像的模型并填写密钥。仍可手动输入答案。");
    $("taskMessage").textContent="AI 视觉服务尚未接通；可以配置服务后继续，或手动输入答案。";toast($("taskMessage").textContent,true);return;
  }
  const pages=restart?d.pages:pendingAiPages(d);
  if(!pages.length){toast("没有需要继续处理的页面；已有答案和人工修改会保留。",true);return}
  const canInfer=cfg.auto_infer&&assignment.settings.allow_inferred&&coverage.accepted>=4;
  const summary=`上传 ${pages.length} 页作业原题图片和所需的上一页图片（可能含姓名、学号和题目），由视觉模型逐页识别并复查漏题和跨页题意；各题转录、答案文字再发送给同一服务解答、复核和必要时精炼。需要发送笔迹字形或生成预览时会另行确认。每页最多识别 12 题，含断线重试最多 ${pages.length*220} 次请求；旧稿和人工编辑会保留。`;
  if(!await cloudConsent("AI 识题、解答并安排每题位置",summary)){toast("本次未调用云端，可手动输入或稍后继续处理");return}
  if(restart){
    const fresh=await api("/api/assignments","POST",{profile_id:profile,document_id:documentId,automatic:true});
    await refreshState();paintDocumentLists();await loadAssignment(fresh.id);targetId=fresh.id;
  }
  processingAI=true;stopAiReview=false;$("stopAiReview").classList.remove("hidden");$("taskPanel").classList.remove("hidden");
  for(const id of ["composeWorkspace","previewWorkspace","assignmentPicker","startChoice"])$(id).inert=true;
  $("profileSelect").disabled=true;$("newProfile").disabled=true;
  let completed=0,questions=0,failed="",skipped=d.pages.length-pages.length;
  const savePage=async(page,items,blank,event,pageError="")=>{
    if(assignment?.id!==targetId)throw Error("当前作业已切换，AI 结果未覆盖其他作业");
    const result=await api(`/api/assignments/${targetId}/apply-ai-page`,"POST",{revision:assignment.revision,page,items,blank,event,page_error:pageError});
    if(result.skipped){skipped++;return}assignment=result.assignment;questions+=result.applied;
  };
  try{
    for(let index=0;index<pages.length;index++){
      if(stopAiReview){failed="已按要求停止，后续页面尚未处理";break}
      const page=pages[index];$("taskMessage").textContent=`AI 正在识别第 ${page.number+1} 页的题目和书写位置（${index+1} / ${pages.length} 页）`;
      $("taskProgress").value=.75*index/pages.length;$("taskPercent").textContent=Math.round(.75*index/pages.length*100)+"%";
      let planned;
      try{
        const existing=assignment.answers.filter(r=>r.page===page.number&&r.source==="ai"&&r.question_key&&r.question).map(r=>({key:r.question_key,question:r.question}));
        planned=await api("/api/ai/plan-question-page","POST",{document_id:documentId,page:page.number,existing,verify:true,consent:true});
      }
      catch(error){failed=`第 ${page.number+1} 页识题或定位失败：${error.message}`;await savePage(page.number,[],false,null,failed);break}
      const items=planned.result.items;
      for(let i=0;i<items.length;i++){
        if(stopAiReview&&!failed)failed="已按要求停止，后续题目尚未解答";
        const item=items[i],old=assignment.answers.find(r=>r.page===page.number&&r.source==="ai"&&r.question_key===item.key&&r.text.trim());
        if(old){item.answer=old.text;item.event=old.ai_event;continue}
        if(failed){item.solve_error=stopAiReview?"AI 处理已停止，本题尚未解答":"前一题处理失败，本题尚未解答";continue}
        if(item.question.toLowerCase().includes("[unclear]")){item.solve_error="题目存在无法辨认的内容，请核对原题";continue}
        $("taskMessage").textContent=`AI 正在解答第 ${page.number+1} 页第 ${i+1} / ${items.length} 题并安排位置`;
        try{const solved=await verifiedAiAnswer(item.question,item.answer_box);item.answer=solved.text;item.event=solved.event;item.verification_event=solved.verification_event}
        catch(error){item.solve_error=error.message;failed=`第 ${page.number+1} 页题目 ${item.key} 解答失败：${error.message}`}
      }
      await savePage(page.number,items,planned.result.blank,planned.event);completed++;
      $("taskProgress").value=.75*completed/pages.length;$("taskPercent").textContent=Math.round(.75*completed/pages.length*100)+"%";
      if(failed)break;
    }
    if(failed){
      for(const page of pages.slice(completed+(failed.includes("识题或定位失败")?1:0)))await savePage(page.number,[],false,null,"AI 处理已中断，本页尚未识题；请继续 AI 处理或手动输入。");
    }
    let positionFailures=[],inferFailure="",inferredCount=0,inferredPending=0;
    if(assignment?.id===targetId){
      if(!failed){const fields=await api(`/api/assignments/${targetId}/profile-fields`,"POST",{revision:assignment.revision});assignment=fields.assignment}
      await loadAssignment(targetId);await switchView("preview");await renderCurrent(false);
      for(let attempt=0;attempt<2&&!failed;attempt++){
        const targets=report.regions.filter(r=>r.errors.some(e=>e.includes("超出")||e.includes("公式需要修正"))).map(r=>assignment.answers.find(a=>a.id===r.id)).filter(r=>r?.source==="ai"&&!r.user_edited&&r.question&&r.text.trim());
        if(!targets.length)break;
        const changes=[];
        for(const r of targets){
          if(stopAiReview){failed="已按要求停止，部分答案尚未自动修订";break}
          $("taskMessage").textContent=`AI 正在复核并精炼第 ${r.page+1} 页题目 ${r.question_key} 的答案`;
          try{const fixed=await verifiedAiAnswer(r.question,r.bbox,r.text);changes.push({id:r.id,original:r.text,text:fixed.text,event:fixed.event,verification_event:fixed.verification_event})}
          catch(error){failed=`题目 ${r.question_key} 的自动修订失败：${error.message}`;break}
        }
        for(let i=0;i<changes.length;i+=12){const saved=await api(`/api/assignments/${targetId}/apply-ai-revisions`,"POST",{revision:assignment.revision,changes:changes.slice(i,i+12)});assignment=saved.assignment}
        if(changes.length){await loadAssignment(targetId);await renderCurrent(false)}
      }
      if(!failed&&canInfer&&report?.missing?.length){
        const missing=report.missing.filter(ch=>[...ch].length===1).slice(0,90);
        const approved=!missing.length||await cloudConsent("自动推测缺失字形",`缺字：${missing.join(" ")}。每批上传最多 16 个已入库个人真实字形的局部对照板和缺字，并上传生成字形的无标签图片进行独立复核；最多 ${Math.ceil(missing.length/3)} 批，含断线重试最多 ${Math.ceil(missing.length/3)*4} 次请求。结果标记为 AI 推测。`);
        if(!approved)inferFailure="未确认发送个人字形";
        for(let i=0;approved&&i<missing.length;i+=3){
          if(stopAiReview){inferFailure="已按要求停止";break}
          const chars=missing.slice(i,i+3).join("");$("taskMessage").textContent=`AI 正在推测缺字 ${chars}（${Math.floor(i/3)+1} / ${Math.ceil(missing.length/3)} 批）`;
          $("taskProgress").value=.75+.1*i/Math.max(1,missing.length);$("taskPercent").textContent=Math.round($("taskProgress").value*100)+"%";
          try{const result=await api(`/api/profiles/${profile}/infer`,"POST",{characters:chars,consent:true});inferredCount+=result.glyphs.filter(g=>g.status==="accepted").length;inferredPending+=result.glyphs.filter(g=>g.status!=="accepted").length}
          catch(error){inferFailure=error.message;break}
        }
        if(inferredPending)inferFailure=[inferFailure,`${inferredPending} 个推测字形未通过可读性检查，已留待处理`].filter(Boolean).join("；");
        if(inferredCount||inferredPending){await loadInferred();await renderCurrent(false)}
      }
      const snapshot=report,revision=assignment.revision,adjustments=[];
      const needsPositions=pages.some(p=>snapshot.errors.some(e=>e.startsWith(`第 ${p.number+1} 页`)&&isLayoutIssue(e))&&assignment.answers.some(r=>r.page===p.number&&r.source==="ai"&&r.question_key&&!r.user_edited));
      const positionApproved=!needsPositions||await cloudConsent("自动修正作答位置",`将需要调整的原题页、包含个人笔迹及答案的生成预览图和排版问题发送给服务，最多 ${pages.length} 页。`);
      if(!positionApproved)positionFailures.push("未确认发送包含笔迹的预览");
      for(const p of pages){
        if(!positionApproved)break;
        if(stopAiReview){positionFailures.push("已按要求停止");break}
        if(!snapshot.errors.some(e=>e.startsWith(`第 ${p.number+1} 页`)&&isLayoutIssue(e))||!assignment.answers.some(r=>r.page===p.number&&r.source==="ai"&&r.question_key&&!r.user_edited))continue;
        $("taskMessage").textContent=`AI 正在根据排版检查调整第 ${p.number+1} 页的作答位置`;
        $("taskProgress").value=.9;$("taskPercent").textContent="90%";
        try{adjustments.push({page:p.number,...await api("/api/ai/refine-positions","POST",{assignment_id:targetId,revision,export_id:snapshot.id,page:p.number,consent:true})})}
        catch(error){positionFailures.push(`第 ${p.number+1} 页：${error.message}`)}
      }
      for(const value of adjustments){
        try{const applied=await api(`/api/assignments/${targetId}/apply-ai-positions`,"POST",{revision:assignment.revision,page:value.page,positions:value.result.positions,event:value.event});assignment=applied.assignment}
        catch(error){positionFailures.push(`第 ${value.page+1} 页：${error.message}`);break}
      }
      if(adjustments.length){await loadAssignment(targetId);await renderCurrent(false)}
    }
    const layoutRemaining=report?.errors.some(isLayoutIssue);
    const remaining=report?.errors.filter(e=>!e.includes("此区域答案尚未勾选审阅确认"))||[];
    const solvedCount=assignment?.answers.filter(r=>r.source==="ai"&&r.question_key&&r.text.trim()).length||0;
    const message=failed?`AI 处理未完成：${failed}。已保存已完成的题目，未完成页面已标记。`:`AI 已处理 ${completed} 页，当前共有 ${solvedCount} 个题目答案；${inferredCount?`自动补充 ${inferredCount} 个 AI 推测字形。`:""}${inferFailure?`自动补字未完成：${inferFailure}。`:""}${skipped?`保留 ${skipped} 页已有结果。`:""}${positionFailures.length?`自动位置调整失败：${positionFailures.join("；")}。`:layoutRemaining?"部分位置仍需调整。":""}${remaining.length?`预览还有 ${remaining.length} 项需处理，请查看下方提示。`:"请最后核对题目、答案和位置。"}`;
    $("taskPanel").classList.remove("hidden");$("taskMessage").textContent=message;if(!failed){$("taskProgress").value=1;$("taskPercent").textContent="100%"}toast(message,!!failed||!!remaining.length);
    return {completed,questions,failed};
  }finally{
    processingAI=false;$("stopAiReview").classList.add("hidden");for(const id of ["composeWorkspace","previewWorkspace","assignmentPicker","startChoice"])$(id).inert=false;
    $("profileSelect").disabled=false;$("newProfile").disabled=false;
  }
}

async function runAiSample(documentId,force){
  if(processingAI)throw Error("另一项 AI 识别正在处理，请先等待完成");
  const cfg=await api("/api/config");
  if(!force&&!cfg.prefer_ai){$("taskMessage").textContent="本地预处理完成；可继续使用本地识别和手动输入。";return}
  if(!cfg.enabled||!cfg.has_key||!cfg.model||!cfg.supports_images){
    if(force)throw Error("AI 视觉服务尚未接通，请在“AI 服务设置”填写密钥并启用图像模型。");
    $("taskMessage").textContent="本地预处理完成；AI 尚未接通，配置服务后可继续处理。";toast("AI 视觉服务尚未接通，已使用本地备选。",true);return;
  }
  const d=await getDocument(documentId),pages=d.pages.filter(p=>!p.meta.cloud_event),total=pages.length;
  if(!total)return {completed:0,created:0,failed:null};
  if(!await cloudConsent("AI 优先识别笔迹",`尚未由 AI 识别的 ${total} 页去印刷层笔迹图片，可能含姓名、学号及手写内容；含断线重试最多 ${total*2} 次视觉识别请求。`)){toast("本次未调用云端，继续使用本地备选");return}
  processingAI=true;$("taskPanel").classList.remove("hidden");let completed=0,created=0,failed=null;
  try{
    for(const p of pages){
      $("taskMessage").textContent=`AI 正在识别笔迹：第 ${completed+1} / ${total} 页（原稿第 ${p.number+1} 页）`;
      $("taskProgress").value=completed/total;$("taskPercent").textContent=Math.round(completed/total*100)+"%";
      try{const result=await api("/api/ai/recognize","POST",{document_id:documentId,pages:[p.number],consent:true});created+=result.candidates;completed++}
      catch(error){failed=error.message;break}
    }
    documentCache.delete(documentId);if(reviewDoc?.id===documentId){reviewDoc=await getDocument(documentId);await loadGlyphs()}
    await refreshState();paintDocumentLists();await loadCoverage();
    const message=failed?`AI 处理未全部完成：${failed}。已保留本地备选和已完成的识别。`:`AI 已定位 ${created} 个候选，可继续进行 AI 自动校对。`;
    $("taskMessage").textContent=message;$("taskProgress").value=completed/total;$("taskPercent").textContent=Math.round(completed/total*100)+"%";toast(message,!!failed);
    return {completed,created,failed};
  }finally{processingAI=false}
}

async function runAiGlyphReview(documentId,page,retry){
  if(processingAI)throw Error("另一项 AI 任务正在处理，请先等待完成");
  if(!documentId)throw Error("请先选择样本文件");
  const params=new URLSearchParams({document_id:documentId});
  if(page!==null)params.set("page",page);
  if(retry)params.set("retry_uncertain","1");
  const plan=await api("/api/ai/glyph-review/plan?"+params);
  if(!plan.total){toast("没有尚未处理的候选；不确定项可在“查看字形”中检查或重新处理。");return}
  const scope=page===null?"整份样本":`第 ${page+1} 页`;
  const summary=`${scope}的 ${plan.total} 个候选及对应原文局部和识别标签；每批最多 12 个，可能再上传候选裁剪进行独立复核。首次返回格式无效时，会将同一批候选重试一次；含复核及断线重试最多 ${plan.requests*6} 次请求。原文局部可能包含姓名、学号或答案。两次识别一致且可靠的手写样本会自动入库，不确定项保留。`;
  if(!await cloudConsent("AI 自动校对并入库",summary)){toast("本次未调用云端，候选保持原状");return}
  processingAI=true;stopAiReview=false;$("stopAiReview").classList.remove("hidden");$("taskPanel").classList.remove("hidden");
  const counts={accepted:0,rejected:0,uncertain:0,superseded:0,skipped:0};let done=0,failed="",formatWarnings=0,reviewWarnings=0,formatRetries=0,consecutiveFailures=0;
  try{
    for(const ids of plan.batches){
      if(stopAiReview)break;
      $("taskMessage").textContent=`AI 正在校对 ${scope}：第 ${done+1} / ${plan.requests} 批；已自动入库 ${counts.accepted} 个`;
      $("taskProgress").value=done/plan.requests;$("taskPercent").textContent=Math.round(done/plan.requests*100)+"%";
      try{
        const result=await api("/api/ai/glyph-review","POST",{document_id:documentId,ids,consent:true,verification_consent:true,format_retry_consent:true});
        for(const key of Object.keys(counts))counts[key]+=result.counts[key]||0;
        if(result.event?.verification?.format_warning)formatWarnings++;
        if(result.event?.format_retry_succeeded)formatRetries++;
        if(result.event?.review_format_warning){reviewWarnings++;consecutiveFailures++}else consecutiveFailures=0;
        done++;
        if(consecutiveFailures>=3){failed="连续 3 批首次校对格式无效，候选保持待处理，请检查模型或服务配置";break}
      }catch(error){failed=error.message;break}
    }
    // Refresh the list without replacing an editor that the user may be changing.
    if(reviewDoc?.id===documentId)await loadGlyphs();
    await refreshState();paintDocumentLists();await loadCoverage();
    const message=`AI 已完成 ${done} / ${plan.requests} 批请求：自动入库 ${counts.accepted}，排除 ${counts.rejected}，不确定 ${counts.uncertain}，跳过 ${counts.skipped}。`+(formatRetries?` ${formatRetries} 批格式重试成功。`:"")+(reviewWarnings?` ${reviewWarnings} 批校对格式仍无效，已保留待处理，可稍后继续。`:"")+(formatWarnings?` ${formatWarnings} 批独立复核格式异常，未通过复核的候选已保留待处理。`:"")+
      (failed?` 处理暂停：${failed}`:stopAiReview?" 已停止；已完成的结果保存在本地。":"");
    $("taskMessage").textContent=message;$("taskProgress").value=done/plan.requests;$("taskPercent").textContent=Math.round(done/plan.requests*100)+"%";
    toast(message,!!failed);
  }finally{
    processingAI=false;$("stopAiReview").classList.add("hidden");
  }
}

async function loadReviewDocument(id){
  id=id||$("reviewDocument").value;reviewDoc=await getDocument(id);activeGlyph=null;editBox=null;selected.clear();glyphOffset=0;
  if(reviewDoc){$("reviewDocument").value=id;options($("reviewPage"),reviewDoc.pages.map(p=>({value:p.number,label:`第 ${p.number+1} 页`})),0);reviewPage=0}
  else options($("reviewPage"),[{value:0,label:"无页面"}],0);
  resetGlyphEditor();await loadGlyphs();drawSource();
}
async function loadGlyphs(){
  if(!reviewDoc){glyphs=[];glyphTotal=0;paintGlyphGrid();return}
  const params=new URLSearchParams({profile_id:profile,document_id:reviewDoc.id,page:reviewPage,status:$("statusFilter").value,form:$("formFilter").value,label:$("labelFilter").value,limit:60,offset:glyphOffset});
  const result=await api("/api/glyphs?"+params);glyphs=result.glyphs;glyphTotal=result.total;paintGlyphGrid();drawSource();
}
function paintGlyphGrid(){
  const grid=$("glyphGrid");grid.replaceChildren();
  if(!glyphs.length)grid.append(node("div","此筛选条件下没有候选。可更改筛选，或在原页框选新候选。","empty"));
  for(const g of glyphs){const card=node("div",undefined,"glyph-card"+(g.id===activeGlyph?.id?" active":""));card.tabIndex=0;
    const check=node("input");check.type="checkbox";check.checked=selected.has(g.id);check.setAttribute("aria-label","勾选候选 "+(g.label||"未识别"));check.addEventListener("click",e=>e.stopPropagation());check.addEventListener("change",()=>{check.checked?selected.add(g.id):selected.delete(g.id);$("selectionCount").textContent=selected.size});card.append(check);
    const crop=node("div",undefined,"crop"),im=node("img");im.src=g.image_url;im.alt="候选 "+(g.label||"未识别");crop.append(im);
    const ai=g.ai_review&&!g.ai_review.stale?(g.ai_review.outcome==="accepted"?" · AI 自动入库":g.ai_review.outcome==="uncertain"?" · AI 不确定":" · AI 已处理"):"";
    card.append(crop,node("div",g.label||"待标注","label"),node("small",(statusNames[g.status]||g.status)+ai));
    card.addEventListener("click",()=>selectGlyph(g));card.addEventListener("keydown",e=>{if(e.key==="Enter")selectGlyph(g)});grid.append(card);
  }
  $("glyphCount").textContent=`${glyphTotal} 个`;
  $("glyphRange").textContent=glyphTotal?`${glyphOffset+1}–${Math.min(glyphOffset+60,glyphTotal)} / ${glyphTotal}`:"0 / 0";
  $("previousGlyphs").disabled=glyphOffset===0;$("nextGlyphs").disabled=glyphOffset+60>=glyphTotal;$("selectionCount").textContent=selected.size;
}
function resetGlyphEditor(){
  $("glyphLabel").value="";$("originalLabel").textContent="";$("glyphClass").value="unknown";$("glyphBaseline").value=.96;$("glyphIsolate").checked=false;
  $("glyphCrop").hidden=true;$("cropEmpty").hidden=false;$("candidateConfidence").textContent="未选择";$("candidateNote").textContent="";$("candidateSource").textContent="在原页拖拽框选，或选择候选。";writeCoords("glyphCoords",null);
  $("aiReviewInfo").hidden=true;$("aiReviewInfo").replaceChildren();$("applyAiSuggestion").classList.add("hidden");
}
function selectGlyph(g){
  activeGlyph=g;editBox=[...g.bbox];$("editorTitle").textContent="校对候选";$("glyphLabel").value=g.label;$("glyphClass").value=g.classification;$("glyphBaseline").value=g.metrics.baseline;$("glyphIsolate").checked=!!g.metrics.vector_isolated;
  $("originalLabel").textContent="初次识别："+(g.original_label||"未识别");$("glyphCrop").src=g.image_url+"?v="+Date.now();$("glyphCrop").hidden=false;$("cropEmpty").hidden=true;
  $("candidateConfidence").textContent=`${statusNames[g.status]||g.status} · 引擎分数 ${g.confidence.toFixed(2)}`;
  $("candidateSource").textContent=`${reviewDoc.name} · 第 ${g.page+1} 页 · ${g.metrics.source_type==="vector"?"矢量来源":"扫描来源"} · ${g.id.slice(0,8)}`;
  $("candidateNote").textContent=g.note;
  const info=$("aiReviewInfo");info.replaceChildren();info.hidden=!g.ai_review;$("applyAiSuggestion").classList.add("hidden");
  if(g.ai_review){
    const r=g.ai_review,s=r.suggestion;
    info.append(node("strong",r.stale?"AI 结果（样本已修改）":({accepted:"AI 自动入库",rejected:"AI 自动排除",superseded:"AI 自动合并 / 拆分",uncertain:"AI 未确定"}[r.outcome]||"AI 判断")));
    info.append(node("p",`建议标签：${s.label||"未确定"} · 分类：${s.classification} · 模型自评分：${s.confidence.toFixed(2)}`));
    info.append(node("p",s.reason||"模型未提供说明。"));
    if(s.verification?.length)info.append(node("p",`独立复核：${s.verification.map(x=>`${x.label||"无法辨认"}（${x.complete?"裁剪完整":"裁剪不完整"}）`).join("；")}`));
    info.append(node("small",`服务：${r.event.service} · 模型：${r.event.model}；自评分不保证正确。`));
    if(g.status==="pending"&&s.bbox)$("applyAiSuggestion").classList.remove("hidden");
  }
  writeCoords("glyphCoords",editBox);paintGlyphGrid();drawSource();
}
function writeCoords(id,bbox){for(const e of $(id).querySelectorAll("input[data-coord]"))e.value=bbox?Number(bbox[Number(e.dataset.coord)].toFixed(2)):""}
function readCoords(id){return[...$(id).querySelectorAll("input[data-coord]")].map(e=>Number(e.value))}
async function saveGlyph(status){
  if(!reviewDoc||!editBox)throw Error("请先选择候选或在原页框选");
  const value={label:$("glyphLabel").value,bbox:readCoords("glyphCoords"),classification:$("glyphClass").value,baseline:Number($("glyphBaseline").value),isolate_vectors:$("glyphIsolate").checked,status};
  let g;
  if(activeGlyph)g=await api(`/api/glyphs/${activeGlyph.id}`,"PATCH",value);
  else{
    if(["accepted","holdout"].includes(status)&&(!value.label||value.classification!=="handwriting"))throw Error("请填写实际标签，并将来源分类确认为本人真实手写");
    g=await api("/api/glyphs","POST",{...value,document_id:reviewDoc.id,page:reviewPage});
    if(status!=="pending")g=await api(`/api/glyphs/${g.id}`,"PATCH",value);
  }
  activeGlyph=g;selected.delete(g.id);await loadCoverage();await loadGlyphs();selectGlyph(g);toast(status==="accepted"?"已确认真实并入库":status==="holdout"?"已保留为独立对照，不参与生成":"校对结果已保存");
}

async function imageFor(url){if(!imageCache.has(url)){imageCache.set(url,new Promise((resolve,reject)=>{const im=new Image();im.onload=()=>resolve(im);im.onerror=()=>reject(Error("页面图片加载失败"));im.src=url}))}return imageCache.get(url)}
let sourceDrawId=0,assignmentDrawId=0;
async function drawSource(){
  const generation=++sourceDrawId;const canvas=$("sourceCanvas");
  if(!reviewDoc){canvas.hidden=true;$("sourceEmpty").hidden=false;return}
  canvas.hidden=false;$("sourceEmpty").hidden=true;const p=reviewDoc.pages[reviewPage];if(!p)return;
  const image=await imageFor(inkOnly?p.ink_url:p.image_url);if(generation!==sourceDrawId)return;
  const scale=Math.max(250,$("sourceScroll").clientWidth-28)/p.width*Number($("sourceZoom").value);
  canvas.width=Math.round(p.width*scale);canvas.height=Math.round(p.height*scale);canvas.style.width=canvas.width+"px";canvas.style.height=canvas.height+"px";
  const ctx=canvas.getContext("2d");ctx.setTransform(scale,0,0,scale,0,0);ctx.drawImage(image,0,0,p.width,p.height);
  for(const g of glyphs){if(g.id===activeGlyph?.id)continue;outline(ctx,g.bbox,"#627d8b55",null,1/scale)}
  if(editBox)outline(ctx,editBox,"#2c7958",$("glyphLabel").value||"框选",1.8/scale,true);
  $("sourceInfo").textContent=`${p.width.toFixed(0)} × ${p.height.toFixed(0)} pt · ${p.meta.vector_ink?"矢量分离":"扫描/混合，需确认来源"}`;
  $("toggleInk").textContent=inkOnly?"显示原页":"只看笔迹";
}
function outline(ctx,r,color,label,width=1,fill=false){
  ctx.strokeStyle=color;ctx.lineWidth=width;ctx.strokeRect(r[0],r[1],r[2]-r[0],r[3]-r[1]);
  if(fill){ctx.fillStyle="#549b6c13";ctx.fillRect(r[0],r[1],r[2]-r[0],r[3]-r[1]);for(const[x,y]of[[r[0],r[1]],[r[2],r[1]],[r[0],r[3]],[r[2],r[3]]]){ctx.fillStyle=color;ctx.fillRect(x-2,y-2,4,4)}}
  if(label){ctx.font="10px sans-serif";ctx.fillStyle=color;ctx.fillText(label.slice(0,35),r[0],Math.max(10,r[1]-5))}
}
function draggableCanvas(id,getPage,getBox,setBox,redraw){
  const canvas=$(id);let drag=null;
  function point(e){const p=getPage(),r=canvas.getBoundingClientRect();return[Math.max(0,Math.min(p.width,(e.clientX-r.left)/r.width*p.width)),Math.max(0,Math.min(p.height,(e.clientY-r.top)/r.height*p.height))]}
  canvas.addEventListener("pointerdown",e=>{
    if(!getPage())return;const start=point(e),r=getBox(),p=getPage();let type="new",edges=null;
    if(r){const tol=p.width/canvas.clientWidth*8;edges={l:Math.abs(start[0]-r[0])<tol,r:Math.abs(start[0]-r[2])<tol,t:Math.abs(start[1]-r[1])<tol,b:Math.abs(start[1]-r[3])<tol};
      if(start[0]>=r[0]-tol&&start[0]<=r[2]+tol&&start[1]>=r[1]-tol&&start[1]<=r[3]+tol){type=Object.values(edges).some(Boolean)?"resize":"move"}}
    drag={start,box:r?[...r]:null,type,edges,moved:false};canvas.setPointerCapture(e.pointerId);e.preventDefault();
  });
  canvas.addEventListener("pointermove",e=>{
    if(!drag)return;const end=point(e),p=getPage(),dx=end[0]-drag.start[0],dy=end[1]-drag.start[1];if(Math.abs(dx)+Math.abs(dy)<1)return;drag.moved=true;let r;
    if(drag.type==="move"){const b=drag.box,w=b[2]-b[0],h=b[3]-b[1],x=Math.max(0,Math.min(p.width-w,b[0]+dx)),y=Math.max(0,Math.min(p.height-h,b[1]+dy));r=[x,y,x+w,y+h]}
    else if(drag.type==="resize"){r=[...drag.box];if(drag.edges.l)r[0]=end[0];if(drag.edges.r)r[2]=end[0];if(drag.edges.t)r[1]=end[1];if(drag.edges.b)r[3]=end[1]}
    else r=[drag.start[0],drag.start[1],end[0],end[1]];
    r=[Math.min(r[0],r[2]),Math.min(r[1],r[3]),Math.max(r[0],r[2]),Math.max(r[1],r[3])];if(r[2]-r[0]>.5&&r[3]-r[1]>.5){setBox(r,false);redraw()}
  });
  canvas.addEventListener("pointerup",()=>{if(drag?.moved)setBox(getBox(),true);drag=null;redraw()});canvas.addEventListener("pointercancel",()=>drag=null);
}

function paintAssignmentEmpty(){
  $("composeWorkspace").classList.add("hidden");$("composeEmpty").classList.remove("hidden");
  $("previewWorkspace").classList.add("hidden");$("previewEmpty").classList.remove("hidden");
  $("saveState").textContent="选择或新建一份作业";
}
async function loadAssignment(id){
  if(dirty)await saveAssignment();assignment=await api("/api/assignments/"+id);dirty=false;report=null;showResult=false;regionId=assignment.answers[0]?.id||"";
  localStorage.setItem("ink-assignment-"+profile,id);$("assignmentSelect").value=id;$("composeWorkspace").classList.remove("hidden");$("composeEmpty").classList.add("hidden");
  $("previewWorkspace").classList.remove("hidden");$("previewEmpty").classList.add("hidden");$("assignmentPicker").open=false;$("answerDetails").open=!assignment.document_id&&!assignment.answers.some(r=>r.text.trim());
  $("assignmentTitle").value=assignment.title;$("questions").value=assignment.questions;
  $("aiWorkflow").hidden=!assignment.document_id;
  const s=assignment.settings;$("fontSize").value=s.font_size;$("lineSpacing").value=s.line_spacing;$("letterSpacing").value=s.letter_spacing??.7;$("wordSpacing").value=s.word_spacing;$("inkWeight").value=s.ink_weight;$("missingPolicy").value=s.missing_policy;
  $("allowInferred").checked=s.allow_inferred!==false;
  $("uniformStroke").checked=s.uniform_stroke!==false;
  $("autoFitSize").checked=s.auto_fit!==false;
  paintStyleOptions();
  const pages=assignmentPages();options($("previewPage"),pages.map((p,i)=>({value:i,label:`第 ${i+1} 页`})),0);options($("regionPage"),pages.map((p,i)=>({value:i,label:String(i+1)})),0);
  paintRegions();$("saveState").textContent="已保存 · 版本 "+assignment.revision;clearReport();
  if(assignment.latest){try{const old=await api(`/exports/${assignment.latest}/audit.json`);if(old.revision===assignment.revision){if(old.parser_version===5&&old.size_version===31&&(old.format_version===1||!assignment.answers.some(r=>hasFormattedText(r.text)))){report=old;showResult=true;paintReport()}else{$("renderSummary").textContent="排版和字形检查已更新，请确认自动重新检查；原答案未修改。";$("resumeAssignment").hidden=!assignment.document_id||!assignment.answers.some(r=>r.source==="ai");$("resumeAssignment").textContent="确认继续自动处理";$("renderAssignment").textContent="确认重新检查并生成预览"}}}catch{}}
  paintAssignmentAction();drawAssignment();
  $("renderProgressWrap").classList.add("hidden");$("renderAssignment").disabled=false;
  const pending=localStorage.getItem("ink-render-"+id);
  if(pending&&(!renderWatch||renderWatch.taskId===pending))followRender(pending,id,assignment.revision,editVersion).catch(error=>toast(error.message,true));
}
function assignmentPages(){return assignment?.document?.pages||[{width:595.28,height:841.89,meta:{suggested_regions:[[50,65,545,785]]}}]}
function paintRegionChoices(){
  const regions=assignment.answers.map((r,i)=>({value:r.id,label:`第 ${r.page+1} 页 · ${r.question_key?"题目 "+r.question_key:"区域 "+(i+1)} · ${r.text.trim()?(r.reviewed?"已审核":"待审核"):r.ai_error?"未完成":"空白"}`}));
  options($("regionSelect"),regions,regionId);options($("previewRegion"),regions,regionId);
  $("regionPicker").hidden=regions.length<=1;
}
function paintRegions(){
  paintRegionChoices();
  const r=currentRegion();if(!r)return;
  $("regionPage").value=r.page;$("previewPage").value=r.page;$("answerText").value=r.text;$("answerReviewed").checked=!!r.reviewed;writeCoords("regionCoords",r.bbox);
  $("regionQuestionWrap").hidden=!(r.question!==undefined||(r.source==="ai"&&!r.ai_page_blank));$("regionQuestion").value=r.question||"";
  if(editorRegionId!==regionId){editorRegionId=regionId;$("questionSourceEditor").open=!(r.question||"").trim();$("answerSourceEditor").open=!r.text.trim()}
  scheduleReadable();$("answerParseError").classList.toggle("hidden",!r.parse_error);$("answerParseError").textContent=r.parse_error?"公式待修正（草稿已保存）："+r.parse_error:"";drawAssignment();
}
function collectEditors(){
  if(!assignment)return;const r=currentRegion();
  if(r){r.text=$("answerText").value;r.reviewed=$("answerReviewed").checked;r.bbox=readCoords("regionCoords");r.page=Number($("regionPage").value);if(!$("regionQuestionWrap").hidden)r.question=$("regionQuestion").value}
  assignment.title=$("assignmentTitle").value;assignment.questions=$("questions").value;
  assignment.settings={font_size:Number($("fontSize").value),line_spacing:Number($("lineSpacing").value),letter_spacing:Number($("letterSpacing").value),word_spacing:Number($("wordSpacing").value),ink_weight:Number($("inkWeight").value),missing_policy:$("missingPolicy").value,allow_inferred:$("allowInferred").checked,uniform_stroke:$("uniformStroke").checked,auto_fit:$("autoFitSize").checked};
}
function paintStyleOptions(){
  const fitHelp=$("autoFitHelp"),missingHelp=$("missingPolicyHelp");
  if(!fitHelp||!missingHelp)return;
  const field=$("fontSize"),size=Number(field.value),valid=field.value!==""&&Number.isFinite(size)&&size>=8&&size<=40;
  const floor=Math.ceil(Math.max(12,size*.8)*2)/2,steps=Math.floor((size-floor)*2);
  $("autoFitSize").disabled=!valid||steps<1;
  if(!valid)fitHelp.textContent="请先输入 8–40 pt 的字高。";
  else if(steps<1)fitHelp.textContent="当前字高没有可用的缩小空间：每次缩小 0.5 pt，且不低于原设的 80% 和 12 pt。";
  else{
    const active=assignment?.answers.filter(r=>r.text.trim())||[];
    const ineligible=active.some(r=>r.source!=="ai"||r.user_edited);
    fitHelp.textContent=`仅当所有非空答案均为未修改的 AI 草稿时生效；当前最低可缩至 ${Number((size-steps*.5).toFixed(2))} pt（每次 0.5 pt）。`+(ineligible?"本稿包含手动或已修改答案，不会自动缩小。":"")+"手动修改字高会关闭此项。";
  }
  const inferred=$("allowInferred").checked;
  const reference=$("missingPolicy").value==="reference";
  const last=reference?"仍缺字时使用灰色印刷参考字":"仍缺字时阻止导出";
  missingHelp.textContent="当前顺序：真实字形 → "+(inferred?"已允许的推测字形 → ":"")+last+"。"+(inferred?"推测字形会在预览和审计中标明；自动补字由 AI 服务设置控制。":"推测字形已关闭。")+(reference?"中文、∈、≠ 等缺字会显示为问号。":"");
}
function invalidateReview(){if(!assignment)return;for(const r of assignment.answers)r.reviewed=false;$("answerReviewed").checked=false}
function exportFilename(job){
  const clean=value=>String(value||"").replace(/[\x00-\x1f<>:"/\\|?*]+/g,"").replace(/\s+/g," ").trim();
  const title=clean((job.document?.name||job.title||"作业").replace(/\.pdf$/i,""))||"作业";
  const field=key=>{const values=new Set(job.answers.filter(r=>r.source==="profile"&&r.reviewed&&r.question_key===key).map(r=>clean(r.text)).filter(Boolean));return values.size===1?[...values][0]:""};
  const number=title.match(/^assignment\s*0*(\d+)$/i),name=field("name"),student=field("student_id");
  return number&&name&&student?`${name} ${student} A${number[1]}.pdf`:`${title}_检查稿.pdf`;
}
function clearReport(){
  comparisonState=null;$("verifyUsed").hidden=true;$("comparisonWorkspace").classList.add("hidden");options($("comparisonReference"),[{value:"",label:"先生成预览"}],"");
  $("exportPdf").removeAttribute("href");$("exportPdf").classList.add("disabled");$("exportPdf").setAttribute("aria-disabled","true");$("exportPdf").hidden=true;$("renderAssignment").hidden=false;$("renderAgain").hidden=true;$("exportAudit").classList.add("hidden");$("renderIssues").replaceChildren();$("missingAction").classList.add("hidden");$("resumeAssignment").hidden=true;$("restartAssignment").hidden=true;$("finalReviewLabel").hidden=true;$("confirmAll").hidden=true;$("renderSummary").className="render-summary";$("renderSummary").textContent="生成预览后，检查答案、书写位置和笔迹。";
}
function markDirty(){
  if(!assignment)return;collectEditors();paintStyleOptions();scheduleReadable();paintRegionChoices();dirty=true;editVersion++;report=null;showResult=false;clearReport();$("saveState").textContent="有改动，正在自动保存…";
  clearTimeout(saveTimer);saveTimer=setTimeout(()=>saveAssignment(false).catch(error=>{$("saveState").textContent="尚未保存："+error.message}),1000);paintAssignmentAction();drawAssignment();
}
async function saveAssignment(notify=false){
  clearTimeout(saveTimer);if(!assignment)throw Error("请先选择或新建作业");
  if(savePromise){await savePromise;if(!dirty)return assignment}
  if(!dirty){if(notify)toast("答案已保存");return assignment}
  collectEditors();const version=editVersion,id=assignment.id,payload=JSON.parse(JSON.stringify(assignment));
  savePromise=api("/api/assignments/"+id,"PUT",payload);
  try{
    const saved=await savePromise;
    if(assignment.id===id){assignment.revision=saved.revision;assignment.latest=null;if(editVersion===version){assignment=saved;dirty=false;const error=currentRegion()?.parse_error;paintRegionChoices();$("saveState").textContent=error?"草稿已保存 · 公式待修正":"已保存 · 版本 "+saved.revision;$("answerParseError").classList.toggle("hidden",!error);$("answerParseError").textContent=error?"公式待修正（草稿已保存）："+error:"";if(error)$("answerReviewed").checked=false}else{$("saveState").textContent="正在保存新改动…";clearTimeout(saveTimer);saveTimer=setTimeout(()=>saveAssignment(false).catch(e=>toast(e.message,true)),700)}}
    if(notify)toast("答案与结构已保存");return assignment;
  }finally{savePromise=null}
}
async function drawAssignment(){
  const generation=++assignmentDrawId;if(!assignment)return;
  const page=Number($("previewPage").value)||0,p=assignmentPages()[page];if(!p)return;
  const useResult=showResult&&report?.preview_urls?.[page];$("resultPreview").hidden=!useResult;$("assignmentCanvas").hidden=!!useResult;
  if(useResult){$("resultPreview").src=report.preview_urls[page];$("togglePreview").textContent="显示原题 / 区域";return}
  $("togglePreview").textContent=report?"显示生成结果":"原题 / 区域";
  const image=p.image_url?await imageFor(p.image_url):null;if(generation!==assignmentDrawId)return;
  const canvas=$("assignmentCanvas");const scale=1.5;canvas.width=Math.round(p.width*scale);canvas.height=Math.round(p.height*scale);const ctx=canvas.getContext("2d");ctx.setTransform(scale,0,0,scale,0,0);ctx.fillStyle="white";ctx.fillRect(0,0,p.width,p.height);if(image)ctx.drawImage(image,0,0,p.width,p.height);
  assignment.answers.filter(r=>r.page===page).forEach((r,i)=>outline(ctx,r.bbox,r.id===regionId?"#2b7956":"#8a9b6e88",`区域 ${assignment.answers.indexOf(r)+1}`,r.id===regionId?1.6:1,r.id===regionId));
}
function paintReviewOverview(){
  const box=$("reviewCards"),entries=[];box.replaceChildren();if(!assignment)return;
  for(const [i,r] of assignment.answers.entries()){
    const card=node("div",undefined,"review-card");const title=`第 ${r.page+1} 页 · ${r.question_key?"题目 "+r.question_key:"区域 "+(i+1)}`;
    card.append(node("h3",title));
    if(r.ai_page_blank)card.append(node("p","AI 判断本页没有题目，请对照原页确认。","muted"));
    else{
      if(r.question){const q=node("div",undefined,"readable-content review-question");card.append(node("p","题目"),q);entries.push({target:q,text:r.question})}
      const answer=node("div",undefined,"readable-content review-answer");card.append(answer);entries.push({target:answer,text:r.text||r.ai_error||"尚无答案"});
      if(r.placement_pending)card.append(node("p","AI 尚未确定书写位置，等待继续自动处理。","issue"));
      card.append(button("查看 / 修改此题",async()=>{collectEditors();regionId=r.id;paintRegions();$("answerDetails").open=true;await switchView("compose");$("answerDetails").scrollIntoView({behavior:"smooth",block:"start"})} ,"quiet"));
    }
    box.append(card);
  }
  formatReadable(entries).catch(()=>{for(const e of entries)if(e.target.isConnected)showReadable(e.target,e.text,{error:"本地预览暂不可用"})});
}
async function followRender(taskId,id,revision,version){
  if(renderWatch){if(renderWatch.taskId===taskId)return renderWatch.promise;throw Error("另一份预览正在生成，请稍后再试")}
  const key="ink-render-"+id;
  const promise=(async()=>{
    let terminal=false;
    if(assignment?.id===id){report=null;showResult=false;clearReport();$("renderProgressWrap").classList.remove("hidden");$("renderAssignment").disabled=true;$("renderSummary").textContent="正在排版并检查所有页面…";drawAssignment()}
    try{
      while(true){
        const task=await api("/api/tasks/"+taskId),meta=task.result||{};
        if(meta.assignment_id!==id||meta.revision!==revision){terminal=true;throw Error("作业已修改，请重新生成预览")}
        if(assignment?.id===id){
          const changed=assignment.revision!==revision||editVersion!==version||dirty;
          $("renderProgressWrap").classList.remove("hidden");$("renderAssignment").disabled=true;
          $("renderProgressMessage").textContent=changed?"答案已修改，正在结束旧版预览":task.message;
          $("renderProgress").value=Math.max(0,Math.min(100,task.progress*100));$("renderProgressPercent").textContent=Math.round(task.progress*100)+"%";
        }
        if(task.status==="error"){terminal=true;throw Error(task.message)}
        if(task.status==="complete"){
          terminal=true;const result=await api(`/exports/${meta.export_id}/audit.json`);
          if(assignment?.id!==id)return;
          if(assignment.revision!==revision||result.revision!==revision||editVersion!==version||dirty){report=null;clearReport();throw Error("作业在生成期间发生变化，请重新打开后再预览")}
          report=result;showResult=true;assignment.latest=report.id;paintReport();await drawAssignment();
          const problems=report.errors.filter(e=>!e.includes("此区域答案尚未勾选审阅确认"));
          toast(problems.length?"预览已生成，请先处理提示的问题":report.pdf_url?"预览通过检查，可以导出完整 PDF":"预览已生成，请统一核对后确认",!!problems.length);
          return true;
        }
        await new Promise(resolve=>setTimeout(resolve,700));
      }
    }catch(error){
      if(error.message==="找不到记录")terminal=true;
      if(assignment?.id===id)$("renderSummary").textContent="预览未完成："+error.message;
      throw error;
    }finally{
      if(terminal&&localStorage.getItem(key)===taskId)localStorage.removeItem(key);
      if(assignment?.id===id){$("renderProgressWrap").classList.add("hidden");$("renderAssignment").disabled=false}
    }
  })();
  renderWatch={taskId,promise};
  try{return await promise}finally{if(renderWatch?.promise===promise)renderWatch=null}
}
async function renderCurrent(assist=true){
  if(!assignment)throw Error("请先选择或新建作业");
  if(renderStarting||renderWatch)throw Error("预览正在生成，请稍候");
  renderStarting=true;$("renderAssignment").disabled=true;let complete=false;
  try{
    await saveAssignment();if(dirty)await saveAssignment();
    const id=assignment.id,revision=assignment.revision,version=editVersion;
    $("renderSummary").textContent="正在准备预览…";
    const started=await api(`/api/assignments/${id}/render/start`,"POST",{revision});
    localStorage.setItem("ink-render-"+id,started.task_id);
    complete=await followRender(started.task_id,id,revision,version);
  }finally{renderStarting=false;if(!renderWatch)$("renderAssignment").disabled=false}
  if(complete&&assist)await maybeAiAssist();
}
function isLayoutIssue(text){return ["超出","压到原题","区域重叠","书写位置","碰撞"].some(x=>text.includes(x))}
function displayWarnings(warnings){
  const prefixes=["使用 AI 推测补字，非真实提取，请检查字形：","使用真实笔迹衍生的推测字形，非对应字符的真实样本：","灰色印刷参考字（不属于个人笔迹）：","仅有一个字形版本，无法避免重复："];
  const groups=prefixes.map(()=>new Set()),other=[];
  for(const warning of warnings){
    const index=prefixes.findIndex(prefix=>warning.startsWith(prefix));
    if(index<0){other.push(warning);continue}
    for(const label of warning.slice(prefixes[index].length).trim().split(/\s+/))if(label)groups[index].add(label);
  }
  return [...groups.flatMap((labels,index)=>labels.size?[prefixes[index]+[...labels].join(" ")]:[]),...other];
}
function paintReport(){
  if(!report)return;const issues=$("renderIssues");issues.replaceChildren();
  const otherErrors=report.errors.filter(text=>!text.includes("此区域答案尚未勾选审阅确认"));
  for(const text of otherErrors)issues.append(node("div",text.replace(/[；，]?请扩展区域或移动部分步骤到另一明确区域|[；，]?请增大区域、手动换行或修改字号|，?请检查公式或行距|，?请移动作答区域|，?请调整此题作答区域|，?请调整/g,""),"issue"));for(const text of displayWarnings(report.warnings))issues.append(node("div",text.replace("已排除易混淆的 AI 推测字形：","已排除易混淆的 AI 推测版本（同字符有真实样本时仍使用真实样本）："),"issue warning"));
  paintMissingAction();
  const checks=report.glyph_checks||[];$("verifyUsed").hidden=!checks.length;
  if(checks.length){$("inferFromMissing").hidden=true;$("missingHelp").textContent="先自动复核本次使用的字形，再继续补字和排版。"}
  const needsReview=assignment.answers.some(r=>r.text.trim()&&!r.reviewed);
  const hasLayoutProblem=otherErrors.some(isLayoutIssue);
  $("restartAssignment").hidden=!assignment.document_id||!assignment.answers.some(r=>r.source==="ai");
  $("resumeAssignment").hidden=!assignment.document_id||!(assignment.answers.some(r=>r.ai_error||r.placement_pending)||(hasLayoutProblem&&assignment.answers.some(r=>r.source==="ai"&&!r.user_edited)));
  if(checks.length)$("resumeAssignment").hidden=true;
  $("resumeAssignment").textContent="确认继续自动处理";
  $("finalReviewLabel").hidden=!needsReview||otherErrors.length>0;$("confirmAll").hidden=!needsReview||otherErrors.length>0;
  $("renderSummary").textContent=otherErrors.length?`${report.pages} 页预览 · ${otherErrors.length} 项需继续自动处理`:needsReview?`${report.pages} 页预览已生成，请最后审核题目、答案和位置`:`自动检查通过 · ${report.pages} 页 · 请核对每页内容后导出`;
  $("renderSummary").className="render-summary"+(report.errors.length?"":" good");paintReviewOverview();
  $("renderAssignment").hidden=true;$("renderAgain").hidden=false;$("exportPdf").hidden=!report.pdf_url;
  if(report.pdf_url){$("exportPdf").href=report.pdf_url;$("exportPdf").setAttribute("download",exportFilename(assignment));$("exportPdf").classList.remove("disabled");$("exportPdf").setAttribute("aria-disabled","false")}
  else{$("exportPdf").removeAttribute("href");$("exportPdf").removeAttribute("download");$("exportPdf").classList.add("disabled");$("exportPdf").setAttribute("aria-disabled","true")}
  $("exportAudit").href=report.audit_url||`/exports/${report.id}/audit.json`;$("exportAudit").classList.remove("hidden");
  paintAssignmentAction();loadComparisonReferences().catch(error=>toast(error.message,true));
}
function paintMissingAction(){
  if(!report)return;
  $("missingAction").classList.toggle("hidden",!report.missing.length);$("missingMessage").textContent="本次缺字："+report.missing.join(" ");
  const enough=coverage.accepted>=4,c=state.capabilities?.cloud,ready=c?.enabled&&c?.has_key&&c?.model&&c?.supports_images;
  $("missingHelp").textContent=!enough?"可用真实字形不足，需继续处理已上传的笔迹样本。":!ready?"AI 视觉服务尚未配置，连接后可继续自动处理。":!$("allowInferred").checked?"确认后启用 AI 推测字形，再自动补字和复核。":"确认后由 AI 参考真实笔迹自动补字并复核。";
  $("inferFromMissing").hidden=!enough||!ready;$("missingToLibrary").hidden=enough;$("missingToSettings").hidden=!!ready;
}

async function loadConfig(){
  const c=await api("/api/config");state.capabilities.cloud=c;$("cloudEnabled").checked=c.enabled;$("cloudBase").value=c.base_url;$("cloudModel").value=c.model;$("cloudKey").value="";
  $("maxTokens").value=c.max_tokens;
  $("cloudVision").checked=c.supports_images;$("cloudFirst").checked=c.prefer_ai;$("autoReview").checked=c.auto_review;$("autoInfer").checked=c.auto_infer;$("autoCompare").checked=c.auto_compare;$("cloudPreset").value=c.base_url==="https://api.deepseek.com"&&c.model==="deepseek-flash"?"deepseek":"custom";
  $("cloudConfigStatus").textContent=c.has_key?"本机已保存密钥（不会回显）；连接有效性需要实际调用验证":"AI 优先预设已就绪，尚未保存 API 密钥；当前使用本地备选";
  $("homeAiStatus").textContent=c.enabled&&c.has_key&&c.model&&c.supports_images?`AI 已配置：${c.model} · 上传前会说明内容`:"AI 尚未接通，可配置服务或使用本地备选";
  $("ocrCapability").textContent=`${c.prefer_ai?"AI 优先":"本地优先"}；本地备选：${state.capabilities.ocr_engine}`;$("dataLocation").textContent=state.capabilities.storage;
  $("aiStatus").textContent=c.enabled&&c.has_key&&c.model?`服务：${c.base_url} · 模型：${c.model} · 调用前显示外发内容` : "AI 尚未接通，可在“AI 服务设置”配置，或直接输入答案。";
  const ready=c.enabled&&c.has_key&&c.model&&c.supports_images;$("libraryNextHint").textContent=coverage.accepted?(ready?"接下来上传新作业；AI 会识题、解答并安排位置。":"请先配置 AI，再继续处理作业。"):profileDocuments("sample").length?(ready?"展开已保存的笔迹，选择“继续 AI 处理”；也可先处理作业。":"先配置 AI 服务，再继续处理样本。"):ready?"AI 会整理手写样本，可靠的结果自动入库。":"先配置 AI，以便自动整理上传的笔迹。";
  paintAssignmentAction();paintMissingAction();
}
async function cloudConsent(action,summary){
  const c=await api("/api/config");if(!c.enabled||!c.has_key||!c.model)throw Error("云端能力尚未接通。请先在“AI 服务设置”配置服务、模型和密钥；本地功能仍可继续。");
  const d=$("cloudDialog"),box=$("cloudDisclosure");box.replaceChildren();
  for(const text of ["用途："+action,"服务："+c.base_url,"模型："+c.model,"本次上传："+summary,
    `每次请求输出上限 ${c.max_tokens} token；连接被中断时最多自动重试一次。密钥只用于服务认证，不写入题目文本或调用记录。`])box.append(node("p",text));
  d.returnValue="cancel";d.showModal();return new Promise(resolve=>d.addEventListener("close",()=>resolve(d.returnValue==="ok"),{once:true}));
}
async function loadEvents(){const events=await api("/api/cloud-events");const box=$("cloudEvents");box.replaceChildren();if(!events.length){box.textContent="尚未调用云端服务。";return}for(const e of events){const usage=JSON.parse(e.usage);const item=node("div",undefined,"cloud-event");item.append(node("strong",`${e.action} · ${e.model}`),node("p",`${e.service} · ${e.created} UTC`),node("p",e.payload_summary),node("p",`用量：${JSON.stringify(usage)} `));box.append(item)}}

async function loadComparisonReferences(){
  if(!report||!assignment)return;const id=report.id;
  const refs=await api(`/api/compare/references?profile_id=${profile}&export_id=${id}`);if(report?.id!==id)return;
  options($("comparisonReference"),refs.map(g=>({value:g.id,label:`${g.label} · ${g.status==="holdout"?"独立对照":"已入库（可能同源）"} · 原页 ${g.page+1} · ${g.id.slice(0,6)}`})),null);
  if(!refs.length)options($("comparisonReference"),[{value:"",label:"暂无相同标签的真实对照，请先校对 / 保留样本"}],"");
  const history=await api("/api/compare/history/"+assignment.id);const box=$("comparisonHistory");box.replaceChildren();
  if(!history.length)box.textContent="尚无对照。";
  for(const h of history){const e=node("div",undefined,"cloud-event");e.append(node("p",`${h.payload.expected} · ${h.payload.reference_status==="holdout"?"独立对照":"入库样本"} · ${h.created} UTC`));const link=node("a","查看本地对照数据","quiet");link.href=`/exports/${h.id}/comparison.json`;e.append(link);if(h.result){const r=node("a","AI 审核记录","quiet");r.href=`/exports/${h.id}/cloud-result.json`;e.append(r)}box.append(e)}
}

async function loadInferred(){
  const grid=$("inferredGrid");grid.replaceChildren();if(!profile)return;
  const list=await api(`/api/profiles/${profile}/inferred`);
  if(!list.length){grid.append(node("p","尚无推测字形。真实样本不足时，仍可先使用定向补写模板。","muted"));return}
  for(const g of list){const derived=g.metrics.source_type==="derived_from_real",kind=derived?"本地衍生":"AI 推测";const card=node("div",undefined,"inferred-card");card.append(node("span",kind+" · "+(g.status==="accepted"?"可用于生成":g.status==="rejected"?"已排除":"待处理"),"tag"));const image=node("img");image.src=g.image_url;image.alt=kind+"字形 "+g.label;card.append(image);const label=node("input");label.value=g.label;label.setAttribute("aria-label",kind+"标签 "+g.id.slice(0,6));card.append(label,node("p",derived?(g.note||"由真实笔迹衍生；并非对应字符的真实提取"):`参考 ${g.metrics.reference_ids?.length||0} 个真实样本；非真实提取`));const buttons=node("div",undefined,"button-row");
    const change=async status=>{await api("/api/inferred/"+g.id,"PATCH",{label:label.value,status});await loadInferred();if(assignment){invalidateReview();markDirty()}toast(status==="accepted"?"已允许使用此推测字形，请重新生成预览":"已排除该字形，请重新生成预览")};
    buttons.append(button("使用 / 保存标签",()=>change("accepted")),button("排除",()=>change("rejected"),"danger-quiet"));card.append(buttons);grid.append(card)
  }
}
async function maybeAiAssist(force=false){
  if(!report||!assignment||processingAI)return;const snapshot=report.id,c=await api("/api/config");
  if(report.glyph_checks?.length)return;
  if((!force&&!c.prefer_ai)||!c.enabled||!c.has_key||!c.supports_images)return;
  if(report.missing.length&&(force||c.auto_infer)&&coverage.accepted>=4&&$("allowInferred").checked){
    const targetId=assignment.id,targetProfile=profile;
    const missing=report.missing.filter(ch=>[...ch].length===1).slice(0,90);
    const declinedKey=targetId+":"+missing.join("");
    if(missing.length&&!declinedAiSuggestions.has(declinedKey)&&report?.id===snapshot){
      const batches=Math.ceil(missing.length/3),limit=batches+missing.length;
      const consent=await cloudConsent("为本次缺字生成 AI 推测字形",`本次缺字：${missing.join(" ")}；每批最多 3 个字符，失败的字符自动单独重试一次。上传最多 16 个真实字形的局部对照板及生成字形的无标签图片进行复核。最多 ${limit} 批，含断线重试最多 ${limit*4} 次请求。结果标记为 AI 推测，不计入真实字库。`);
      if(!consent){declinedAiSuggestions.add(declinedKey);return}
      if(assignment?.id!==targetId||profile!==targetProfile||report?.id!==snapshot)return;
      processingAI=true;stopAiReview=false;$("taskPanel").classList.remove("hidden");$("stopAiReview").classList.remove("hidden");
      $("inferGlyphs").disabled=true;$("inferFromMissing").disabled=true;
      let generated=0,pending=0,attempted=0,failed="";const remaining=new Set(missing),retry=[];
      try{
        const attempt=async chars=>{
          if(stopAiReview)throw Error("已停止，剩余缺字保留待处理");
          if(assignment?.id!==targetId||profile!==targetProfile||report?.id!==snapshot)throw Error("作业已变化，已停止后续补字");
          attempted++;$("taskMessage").textContent=`AI 正在自动补字并复核：${chars}（第 ${attempted} 批）`;
          $("taskProgress").value=attempted/limit;$("taskPercent").textContent=Math.round(attempted/limit*100)+"%";
          try{const result=await api(`/api/profiles/${targetProfile}/infer`,"POST",{characters:chars,consent:true});const usable=result.glyphs.filter(g=>g.status==="accepted");generated+=usable.length;pending+=result.glyphs.length-usable.length;for(const g of usable)remaining.delete(g.label)}
          catch(error){if(!error.message.startsWith("AI 补字结构校验失败"))throw error}
        };
        for(let i=0;i<missing.length;i+=3){
          const chars=missing.slice(i,i+3).join("");
          try{await attempt(chars);retry.push(...[...chars].filter(ch=>remaining.has(ch)))}catch(error){failed=error.message;break}
        }
        for(const ch of retry){if(failed)break;try{await attempt(ch)}catch(error){failed=error.message;break}}
        if(profile===targetProfile)await loadInferred();
        if(generated&&assignment?.id===targetId&&profile===targetProfile&&report?.id===snapshot&&$("allowInferred").checked){invalidateReview();markDirty();await renderCurrent(false)}
        const message=`AI 补字处理 ${attempted} 批，可用 ${generated} 个推测字形。`+(pending?` ${pending} 个未通过检查，已排除。`:"")+(remaining.size?` 未完成：${[...remaining].join("、")}。`:"")+(failed?` 已暂停：${failed}。`:"")+"推测字形已单独标记，真实字库未改动。";
        $("taskPanel").classList.remove("hidden");$("taskMessage").textContent=message;$("taskProgress").value=failed?attempted/limit:1;$("taskPercent").textContent=Math.round($("taskProgress").value*100)+"%";
        toast(message,!!failed||!!remaining.size);
      }catch(error){$("taskMessage").textContent="AI 补字后预览未完成："+error.message;toast($("taskMessage").textContent,true)}
      finally{processingAI=false;$("stopAiReview").classList.add("hidden");$("inferGlyphs").disabled=false;$("inferFromMissing").disabled=false}
      return;
    }
  }
  if(!report.errors.length&&c.auto_compare&&report?.id===snapshot){
    const refs=await api(`/api/compare/references?profile_id=${profile}&export_id=${snapshot}`);
    const ref=refs.find(x=>x.status==="holdout");if(!ref)return;
    try{
      await prepareComparison(ref.id);
      await executeComparison("vision");
    }catch(error){toast("AI 笔迹对照未完成："+error.message+"。本地对照仍可查看。",true)}
  }
}
on("inferGlyphs","click",async()=>{
  requireProfile();const chars=[...new Set([...$("inferCharacters").value].filter(c=>!c.trim()?false:true))].join("");if(!chars||[...chars].length>3)throw Error("每批输入 1–3 个确实缺失的字符");
  if(coverage.accepted<4)throw Error("至少需要 4 个已入库真实字形；请继续处理样本，或下载手写补字模板。");
  if(!await cloudConsent("根据个人真实字形推测缺字",`至多 16 个已入库真实局部字形组成 1 张对照板，以及待补字符“${chars}”；生成结果的无标签图片会再次发送以独立复核，含断线重试最多 4 次请求。通过复核的字形用于预览并标记为 AI 推测，不属于真实样本。`))return;
  const snapshot=report?.id;$("inferGlyphs").disabled=true;toast("正在根据真实笔迹推测补字…");
  try{const result=await api(`/api/profiles/${profile}/infer`,"POST",{characters:chars,consent:true});await loadInferred();if(snapshot&&report?.id===snapshot&&$("allowInferred").checked){invalidateReview();markDirty();await renderCurrent(false)}if(assignment&&!$("allowInferred").checked){$("styleDetails").open=true;toast("已保存 AI 推测字形；本稿已关闭推测字形，可在“调整书写样式”中启用。",true)}else toast(`已保存 ${result.glyphs.length} 个 AI 推测字形，请检查预览；未混入真实字库`)}finally{$("inferGlyphs").disabled=false}
});
on("verifyUsed","click",async()=>{
  if(processingAI||!assignment||!report?.glyph_checks?.length)throw Error("当前没有可复核的字形");
  const target=assignment.id,revision=assignment.revision,exportId=report.id,groups=new Map();
  for(const g of report.glyph_checks){const key=JSON.stringify([g.origin,g.document_id,g.page]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(g.id)}
  const batches=[];for(const ids of groups.values())for(let i=0;i<ids.length;i+=12)batches.push(ids.slice(i,i+12));
  if(!await cloudConsent("自动复核本次使用的字形",`发送本次预览使用的 ${report.glyph_checks.length} 个字形：真实字形附原稿局部上下文，推测字形（含本地衍生）发送无标签图片；同一服务独立辨认标签与完整性。共 ${batches.length} 批，含断线重试最多 ${batches.length*2} 次请求。异常样本自动排除；原稿和原字库不删除。`))return;
  if(assignment?.id!==target||assignment.revision!==revision||report?.id!==exportId)throw Error("作业已变化，请重新确认复核");
  processingAI=true;stopAiReview=false;$("verifyUsed").disabled=true;$("taskPanel").classList.remove("hidden");$("stopAiReview").classList.remove("hidden");
  let done=0,accepted=0,excluded=0,failure="";
  try{
    for(const ids of batches){
      if(stopAiReview){failure="已停止";break}
      $("taskMessage").textContent=`AI 正在独立复核字形：${done+1} / ${batches.length} 批`;
      try{const result=await api(`/api/assignments/${target}/verify-used-glyphs`,"POST",{revision,export_id:exportId,ids,consent:true});accepted+=result.accepted;excluded+=result.excluded;done++;$("taskProgress").value=done/batches.length;$("taskPercent").textContent=Math.round(done/batches.length*100)+"%"}
      catch(error){failure=error.message;break}
    }
    if(done&&assignment?.id===target&&assignment.revision===revision)await renderCurrent(false);
    const message=`字形复核 ${done} / ${batches.length} 批，通过 ${accepted} 个，自动排除 ${excluded} 个。`+(failure?` 未完成：${failure}。`:report?.glyph_checks?.length?` 替换后的 ${report.glyph_checks.length} 个字形还需确认继续复核。`:" 可继续自动补字和排版。");
    $("taskMessage").textContent=message;toast(message,!!failure);
  }finally{processingAI=false;$("verifyUsed").disabled=false;$("stopAiReview").classList.add("hidden")}
});
on("inferFromMissing","click",async()=>{
  if(!report?.missing?.length)throw Error("当前预览没有缺字，或尚未生成预览");
  if(!$("allowInferred").checked){$("allowInferred").checked=true;invalidateReview();markDirty();await renderCurrent(false);if(report?.glyph_checks?.length||!report?.missing?.length)return}
  for(const key of declinedAiSuggestions)if(key.startsWith(assignment.id+":"))declinedAiSuggestions.delete(key);await maybeAiAssist(true)
});
on("missingToLibrary","click",()=>switchView("library"));on("missingToSettings","click",()=>switchView("settings"));
on("missingTemplate","click",async()=>{if(!report?.missing?.length)throw Error("当前预览没有缺字");requireProfile();const result=await api(`/api/profiles/${profile}/template`,"POST",{characters:report.missing.join("")});const a=node("a");a.href=result.pdf_url;a.download="缺字补写模板.pdf";a.click();toast("已生成本次缺字补写模板")});

on("cloudPreset","change",()=>{if($("cloudPreset").value==="deepseek"){$("cloudBase").value="https://api.deepseek.com";$("cloudModel").value="deepseek-flash";$("cloudVision").checked=true;toast("已填写 DeepSeek 视觉接口预设；请输入自己的密钥并保存")}});
on("aiWorkflow","click",async()=>{if(!assignment?.document_id)throw Error("空白稿没有题目图片；可手动输入题目后调用 AI 解答");await runAiAssignment(assignment.document_id,true)});
on("aiStartAssignment","click",async()=>{if(!assignment?.document_id)throw Error("请先上传作业");await runAiAssignment(assignment.document_id,true)});
on("resumeAssignment","click",async()=>{
  if(!assignment?.document_id)throw Error("请先上传作业");
  if(!assignment.answers.some(r=>r.ai_error||r.placement_pending||r.source==="template")){
    const pages=[...new Set((report?.errors||[]).filter(isLayoutIssue).map(e=>Number(e.match(/^第 (\d+) 页/)?.[1])-1).filter(n=>Number.isInteger(n)&&n>=0))];
    if(pages.length){
      const result=await api(`/api/assignments/${assignment.id}/auto-regions`,"POST",{revision:assignment.revision,pages});
      assignment=result.assignment;if(result.changed)await loadAssignment(assignment.id);
    }
    await renderCurrent(false);
    if(report?.glyph_checks?.length||report?.missing?.length)return;
    const pendingPages=[...new Set((report?.errors||[]).filter(isLayoutIssue).map(e=>Number(e.match(/^第 (\d+) 页/)?.[1])-1).filter(n=>Number.isInteger(n)&&n>=0))];
    if(!pendingPages.length)return;
    const snapshot=report.id,revision=assignment.revision,target=assignment.id;
    if(!await cloudConsent("自动修正作答位置",`发送 ${pendingPages.length} 页原题、包含生成笔迹和答案的预览及排版问题；含断线重试最多 ${pendingPages.length*2} 次请求。已有答案保持不变。`))return;
    if(assignment?.id!==target||assignment.revision!==revision||report?.id!==snapshot)throw Error("预览已变化，请重新确认继续处理");
    const positions=[];
    for(const page of pendingPages)positions.push({page,...await api("/api/ai/refine-positions","POST",{assignment_id:target,revision,export_id:snapshot,page,consent:true})});
    for(const value of positions){const applied=await api(`/api/assignments/${target}/apply-ai-positions`,"POST",{revision:assignment.revision,page:value.page,positions:value.result.positions,event:value.event});assignment=applied.assignment}
    await loadAssignment(target);await renderCurrent(false);return;
  }
  await runAiAssignment(assignment.document_id,true);
});
on("restartAssignment","click",async()=>{if(!assignment?.document_id)throw Error("请先上传作业");await runAiAssignment(assignment.document_id,true,true)});
async function prepareComparison(gid){
  if(!report)throw Error("请先生成预览");gid=gid||$("comparisonReference").value;if(!gid)throw Error("请先建立相同标签的真实对照样本");
  $("prepareComparison").disabled=true;
  try{comparisonState=await api("/api/compare/prepare","POST",{export_id:report.id,reference_id:gid});const c=comparisonState;$("comparisonPanel").open=true;
    $("comparisonWorkspace").classList.remove("hidden");$("comparisonReal").src=c.reference_url;$("comparisonGenerated").src=c.generated_url;$("comparisonRealText").value=c.reference_text;$("comparisonGeneratedText").value=c.generated_text;
    $("comparisonMetrics").textContent=`同标签：${c.expected} · 归一化轮廓重叠 ${(c.local_metrics.normalized_mask_dice*100).toFixed(1)}%（仅像素指标）`;
    $("comparisonWarning").textContent=c.warning;$("comparisonResult").textContent="尚未调用云端。可先修正上面的本地识别转录。";$("comparisonDownload").classList.add("hidden");toast("本地对照已保存，尚未上传云端");
  }finally{$("prepareComparison").disabled=false}
}
async function executeComparison(mode){
  if(!comparisonState)throw Error("请先建立本地对照");mode=mode||$("comparisonMode").value;$("comparisonMode").value=mode;
  if(!await cloudConsent("判断书写内容与差异",mode==="vision"?`仅两张局部裁剪（真实样本、生成结果），标签“${comparisonState.expected}”和两份可编辑转录。`:`仅标签“${comparisonState.expected}”及两份转录文字，不上传图片，无法评价笔迹外形。`))return;
  $("runComparison").disabled=true;$("comparisonResult").textContent="正在等待 AI 审核…";
  try{const result=await api(`/api/compare/${comparisonState.id}/run`,"POST",{mode,reference_text:$("comparisonRealText").value,generated_text:$("comparisonGeneratedText").value,consent:true});$("comparisonResult").textContent=result.structured?JSON.stringify(result.structured,null,2):result.text;$("comparisonDownload").href=`/exports/${comparisonState.id}/cloud-result.json`;$("comparisonDownload").classList.remove("hidden");await loadComparisonReferences();toast("AI 审核已保存，请人工核实意见")}
  finally{$("runComparison").disabled=false}
}
on("prepareComparison","click",()=>prepareComparison());
on("runComparison","click",()=>executeComparison());

for(const e of document.querySelectorAll(".nav-item"))e.addEventListener("click",()=>switchView(e.dataset.view).catch(error=>toast(error.message,true)));
on("goCompose","click",()=>switchView("compose"));
on("openSettings","click",()=>switchView("settings"));
on("goInference","click",async()=>{await switchView("preview");$("inferredDetails").open=true;$("inferCharacters").focus()});
on("goPreview","click",async()=>{collectEditors();if(!assignment?.answers.some(r=>r.text.trim()))throw Error("请先输入或导入答案");const reopening=!!assignment.latest&&!dirty;await switchView("preview");if(!report||dirty)await renderCurrent(!reopening)});
on("previewStart","click",()=>switchView("compose"));on("backToAnswer","click",async()=>{$("answerDetails").open=true;await switchView("compose");$("answerDetails").scrollIntoView({behavior:"smooth",block:"start"})});
on("aiSetupAssignment","click",()=>switchView("settings"));
on("dismissTask","click",()=>{if(processingAI)throw Error("AI 正在处理，可先点击停止");$("taskPanel").classList.add("hidden")});
on("profileSelect","change",async()=>selectProfile($("profileSelect").value));
on("newProfile","click",async()=>{const name=await textDialog("新建使用者档案","每个档案有独立的样本、字形与作业。","我的笔迹");if(!name?.trim())return;const p=await api("/api/profiles","POST",{name});await refreshState();await selectProfile(p.id);toast("档案已建立，上传真实手写样本开始")});
on("sampleUpload","click",()=>$("sampleFile").click());
on("sampleFile","change",async()=>{try{await uploadFile($("sampleFile").files[0],"sample")}finally{$("sampleFile").value=""}});
on("assignmentUpload","click",()=>$("assignmentFile").click());
on("assignmentFile","change",async()=>{try{await uploadFile($("assignmentFile").files[0],"assignment")}finally{$("assignmentFile").value=""}});
on("sampleDrop","dragover",e=>{e.preventDefault();$("sampleDrop").classList.add("drag")});on("sampleDrop","dragleave",()=>$("sampleDrop").classList.remove("drag"));
on("sampleDrop","drop",async e=>{e.preventDefault();$("sampleDrop").classList.remove("drag");await uploadFile(e.dataTransfer.files[0],"sample")});
on("refreshLibrary","click",async()=>{await refreshState();paintDocumentLists();await loadCoverage()});
on("templateButton","click",async()=>{requireProfile();const result=await api(`/api/profiles/${profile}/template`,"POST",{});const a=node("a");a.href=result.pdf_url;a.download="补字模板.pdf";a.click();toast(`已生成 ${result.characters.length} 个缺字的定向补写模板`)});
on("reviewDocument","change",()=>loadReviewDocument());
on("aiReviewPage","click",()=>runAiGlyphReview(reviewDoc?.id,reviewPage,$("retryUncertain").checked));
on("aiReviewAll","click",()=>runAiGlyphReview(reviewDoc?.id,null,$("retryUncertain").checked));
on("stopAiReview","click",()=>{stopAiReview=true;$("taskMessage").textContent="等待当前请求完成后停止；已处理结果会保留。"});
on("applyAiSuggestion","click",()=>{
  const s=activeGlyph?.ai_review?.suggestion;if(!s)return;
  $("glyphLabel").value=s.label||"";$("glyphClass").value=s.classification;$("glyphBaseline").value=s.baseline;editBox=[...s.bbox];writeCoords("glyphCoords",editBox);drawSource();
  toast("已填入 AI 建议，可继续修改并保存");
});
on("reviewPage","change",async()=>{reviewPage=Number($("reviewPage").value);glyphOffset=0;activeGlyph=null;editBox=null;selected.clear();resetGlyphEditor();await loadGlyphs()});
on("filterGlyphs","click",async()=>{glyphOffset=0;selected.clear();await loadGlyphs()});
for(const id of["statusFilter","formFilter"])on(id,"change",async()=>{glyphOffset=0;selected.clear();await loadGlyphs()});
on("labelFilter","keydown",async e=>{if(e.key==="Enter"){glyphOffset=0;await loadGlyphs()}});
on("previousGlyphs","click",async()=>{glyphOffset=Math.max(0,glyphOffset-60);await loadGlyphs()});on("nextGlyphs","click",async()=>{glyphOffset+=60;await loadGlyphs()});
on("clearSelection","click",()=>{selected.clear();paintGlyphGrid()});
on("manualGlyph","click",()=>{if(!reviewDoc)throw Error("请先选择样本文件");activeGlyph=null;editBox=null;resetGlyphEditor();$("editorTitle").textContent="框选新候选";drawSource();toast("在左侧原页拖拽框选真实笔迹，再输入实际内容")});
on("toggleInk","click",()=>{inkOnly=!inkOnly;drawSource()});on("sourceZoom","input",()=>drawSource());
for(const e of $("glyphCoords").querySelectorAll("input"))e.addEventListener("input",()=>{editBox=readCoords("glyphCoords");drawSource()});
on("glyphLabel","input",()=>drawSource());
on("acceptGlyph","click",()=>saveGlyph("accepted"));on("saveGlyph","click",()=>saveGlyph("pending"));on("holdoutGlyph","click",()=>saveGlyph("holdout"));on("rejectGlyph","click",()=>saveGlyph("rejected"));
on("mergeGlyphs","click",async()=>{if(selected.size<2)throw Error("勾选至少两个同页候选再合并");const label=await textDialog("合并所选真实区域","原候选保留合并记录；新候选仍待校对。输入合并后的实际标签。");if(label===null)return;const g=await api("/api/glyphs/merge","POST",{ids:[...selected],label});selected.clear();await loadGlyphs();selectGlyph(g);await loadCoverage()});
on("splitGlyph","click",async()=>{
  if(!activeGlyph)throw Error("请先保存或选择要拆分的候选");const d=$("splitDialog");$("splitLabels").value=[...activeGlyph.label].join("|");$("splitCuts").value="";d.returnValue="cancel";d.showModal();
  const answer=await new Promise(resolve=>d.addEventListener("close",()=>resolve(d.returnValue),{once:true}));if(answer!=="ok")return;
  const labels=$("splitLabels").value.split("|");if(labels.length<2||labels.some(x=>!x))throw Error("请输入至少两个非空标签，以 | 分隔");
  const cutSource=$("splitCuts").value.trim();const cuts=cutSource?cutSource.split(/[,，]/).map(Number):labels.slice(1).map((_,i)=>(i+1)/labels.length);
  if(cuts.length!==labels.length-1||cuts.some((x,i)=>!Number.isFinite(x)||x<=0||x>=1||(i>0&&x<=cuts[i-1])))throw Error("切分比例数量应为标签数减一，且在 0–1 内严格递增");
  const bounds=[0,...cuts,1],r=editBox||activeGlyph.bbox,vertical=$("splitDirection").value==="vertical";
  const parts=labels.map((label,i)=>({label,bbox:vertical?[r[0],r[1]+(r[3]-r[1])*bounds[i],r[2],r[1]+(r[3]-r[1])*bounds[i+1]]:[r[0]+(r[2]-r[0])*bounds[i],r[1],r[0]+(r[2]-r[0])*bounds[i+1],r[3]]}));
  const result=await api(`/api/glyphs/${activeGlyph.id}/split`,"POST",{parts});selected.clear();await loadGlyphs();selectGlyph(result.glyphs[0]);await loadCoverage();toast("拆分候选已建立，请核对每个边界")
});
on("cloudRecognize","click",async()=>{if(!reviewDoc)throw Error("请先选样本");if(!await cloudConsent("手写字词识别定位",`“${reviewDoc.name}”第 ${reviewPage+1} 页的去印刷层笔迹图，可能含姓名、学号及作业内容；不发送整份字库。`))return;toast("正在请求识别服务…");const result=await api("/api/ai/recognize","POST",{document_id:reviewDoc.id,pages:[reviewPage],consent:true});await loadGlyphs();await loadCoverage();toast(result.message)});

on("blankAssignment","click",async()=>{await ensureProfile();if(dirty)await saveAssignment();const result=await api("/api/assignments","POST",{profile_id:profile});await refreshState();paintDocumentLists();await loadAssignment(result.id)});
on("assignmentSelect","change",async()=>{if($("assignmentSelect").value)await loadAssignment($("assignmentSelect").value)});
on("regionSelect","change",()=>{collectEditors();regionId=$("regionSelect").value;paintRegions()});
on("previewRegion","change",()=>{collectEditors();regionId=$("previewRegion").value;paintRegions()});
on("regionPage","change",()=>{$("previewPage").value=$("regionPage").value;invalidateReview();markDirty()});
on("previewPage","change",()=>{const page=Number($("previewPage").value),next=assignment?.answers.find(r=>r.page===page);if(next&&currentRegion()?.page!==page){collectEditors();regionId=next.id;paintRegions()}else drawAssignment()});on("togglePreview","click",()=>{showResult=!showResult;drawAssignment()});
on("answerText","input",()=>{const r=currentRegion();if(r){r.user_edited=true;if($("answerText").value.trim()){r.ai_error="";r.question_changed=false}}$("answerReviewed").checked=false;markDirty()});
on("regionQuestion","input",()=>{const r=currentRegion();if(r){r.user_edited=true;r.question_changed=true}$("answerReviewed").checked=false;markDirty()});
for(const id of["assignmentTitle","questions","fontSize","lineSpacing","letterSpacing","wordSpacing","inkWeight","missingPolicy","answerReviewed","allowInferred","uniformStroke","autoFitSize"])on(id,["answerReviewed","missingPolicy","allowInferred","uniformStroke","autoFitSize"].includes(id)?"change":"input",()=>{if(id==="fontSize")$("autoFitSize").checked=false;if(id!=="answerReviewed"&&id!=="assignmentTitle")invalidateReview();markDirty()});
for(const e of $("regionCoords").querySelectorAll("input"))e.addEventListener("input",()=>{const r=currentRegion();if(r){r.user_edited=true;r.placement_pending=false}invalidateReview();markDirty()});
on("saveAssignment","click",()=>saveAssignment(true));
async function regeneratePreview(){if(assignment?.answers.some(r=>r.reviewed)){invalidateReview();markDirty()}await renderCurrent()}
on("renderAssignment","click",regeneratePreview);on("renderAgain","click",regeneratePreview);
on("confirmAll","click",async()=>{if(!report||report.errors.some(e=>!e.includes("此区域答案尚未勾选审阅确认")))throw Error("请先处理缺字、位置和未完成题目，再统一审核");collectEditors();for(const r of assignment.answers)if(r.text.trim())r.reviewed=true;$("answerReviewed").checked=!!currentRegion()?.reviewed;markDirty();await renderCurrent(false);if(report?.pdf_url)toast("已统一确认，可下载完整 PDF")});
on("solveCurrent","click",async()=>{if(!currentRegion())throw Error("请先选择题目");collectEditors();if(dirty)await saveAssignment();const r=currentRegion(),question=r.question||"";if(!question.trim())throw Error("请先输入或修正本题题目");if(!await cloudConsent("重新解答本题",`仅上传本题题目转录，共 ${question.length} 字符；不上传原页或字形档案。`))return;const id=assignment.id,region=r.id,version=editVersion;const result=await api("/api/ai/solve","POST",{questions:question,consent:true});if(assignment?.id!==id||regionId!==region||editVersion!==version)throw Error("题目或作业已变化，未覆盖当前编辑；请重新解答");const target=currentRegion();target.text=result.text;target.ai_event=result.event;target.ai_error="";target.question_changed=false;target.user_edited=true;target.reviewed=false;$("answerText").value=target.text;$("answerReviewed").checked=false;markDirty();toast("AI 已重新解答本题，请在最后统一审核")});
on("regenerateRegion","click",async()=>{if(!currentRegion())throw Error("请先选择区域");currentRegion().seed++;invalidateReview();markDirty();await switchView("preview");await renderCurrent()});
on("addRegion","click",()=>{if(!assignment)throw Error("请先选择作业");collectEditors();const page=Number($("previewPage").value)||0,p=assignmentPages()[page],regions=p.meta.suggested_regions||[];const bbox=regions[0]||[50,150,p.width-50,p.height-60];const r={id:newId(),page,bbox:[...bbox],text:"",reviewed:false,seed:0,source:"manual",user_edited:true};assignment.answers.push(r);regionId=r.id;paintRegions();invalidateReview();markDirty()});
on("deleteRegion","click",()=>{if(!currentRegion())return;assignment.answers=assignment.answers.filter(r=>r.id!==regionId);if(!assignment.answers.length)assignment.answers=[{id:newId(),page:0,bbox:[50,65,545,785],text:"",seed:0,reviewed:false}];regionId=assignment.answers[0].id;paintRegions();invalidateReview();markDirty()});
on("suggestRegion","click",()=>{const r=currentRegion();if(!r)return;const p=assignmentPages()[r.page],suggestions=p.meta.suggested_regions;if(!suggestions?.length)throw Error("本页没有可靠的空白带建议，请在原题中手动框选");r.bbox=[...suggestions.reduce((a,b)=>(a[3]-a[1])>(b[3]-b[1])?a:b)];r.placement_pending=false;r.user_edited=true;writeCoords("regionCoords",r.bbox);invalidateReview();markDirty()});
on("syntaxHelp","click",()=>$("helpDialog").showModal());
on("answerImport","change",async()=>{const file=$("answerImport").files[0];if(!file||!assignment)return;if(file.size>200000)throw Error("答案文件过大，最多 200 KB");const text=await file.text();
  if(file.name.toLowerCase().endsWith(".json")){const data=JSON.parse(text);if(Array.isArray(data.answers)){collectEditors();assignment.answers=data.answers.map(r=>({id:newId(),page:Number(r.page||0),bbox:r.bbox||[50,65,545,785],text:String(r.text||""),reviewed:false,seed:0}));regionId=assignment.answers[0]?.id;paintRegions()}else{$("answerText").value=String(data.text||"")}}
  else $("answerText").value=text;const r=currentRegion();if(r){r.user_edited=true;if($("answerText").value.trim()){r.ai_error="";r.question_changed=false}}$("answerReviewed").checked=false;markDirty();$("answerImport").value="";
});
on("aiSolve","click",async()=>{if(!assignment)return;collectEditors();if(!await cloudConsent("生成可编辑答案",`仅上传题目转录文本，共 ${assignment.questions.length} 字符。不会上传原 PDF、照片或字形档案。`))return;$("aiSolve").disabled=true;try{const result=await api("/api/ai/solve","POST",{questions:assignment.questions,consent:true});$("answerText").value=result.text;$("answerReviewed").checked=false;markDirty();toast("AI 答案已放入当前区域，需审阅修改后才可导出")}finally{$("aiSolve").disabled=false}});
on("saveConfig","click",async()=>{await api("/api/config","PUT",{enabled:$("cloudEnabled").checked,prefer_ai:$("cloudFirst").checked,auto_review:$("autoReview").checked,auto_infer:$("autoInfer").checked,auto_compare:$("autoCompare").checked,supports_images:$("cloudVision").checked,base_url:$("cloudBase").value,model:$("cloudModel").value,api_key:$("cloudKey").value,max_tokens:Number($("maxTokens").value)});await loadConfig();toast("服务配置已保存在本机")});
on("clearCloudKey","click",async()=>{await api("/api/config","PUT",{clear_key:true,enabled:false});await loadConfig();toast("已清除本地 API 密钥并关闭云端能力")});
on("refreshEvents","click",()=>loadEvents());
draggableCanvas("sourceCanvas",()=>reviewDoc?.pages[reviewPage],()=>editBox,(r)=>{editBox=r;writeCoords("glyphCoords",r)},()=>drawSource());
draggableCanvas("assignmentCanvas",()=>assignmentPages()[Number($("previewPage").value)||0],()=>currentRegion()?.bbox,(r,done)=>{const current=currentRegion();if(!current||current.page!==Number($("previewPage").value))return;current.bbox=r;writeCoords("regionCoords",r);if(done){current.placement_pending=false;current.user_edited=true;invalidateReview();markDirty()}},()=>drawAssignment());
window.addEventListener("resize",()=>{if(view==="review")drawSource();if(view==="preview")drawAssignment()});
window.addEventListener("beforeunload",e=>{if(dirty||processingAI){e.preventDefault();e.returnValue=""}});
async function boot(){
  await refreshState();const saved=localStorage.getItem("ink-profile");await selectProfile(state.profiles.find(p=>p.id===saved)?.id||state.profiles[0]?.id||"");await loadConfig();
  const upgraded=localStorage.getItem("ink-ui-version")!=="4";localStorage.setItem("ink-ui-version","4");
  const next=upgraded?(coverage.accepted?"compose":"library"):localStorage.getItem("ink-view")||"library";
  await switchView(next);
}
boot().catch(error=>toast(error.message,true));
