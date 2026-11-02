"""A small explicit math AST, glyph composition, and unchanged-page PDF overlay.

Content is parsed once and persisted independently of style/seeds. Unsupported
TeX is an error rather than silently disappearing from the generated answer.
"""
import hashlib
import io
import json
import math
import random
import re
from dataclasses import dataclass, field
from functools import lru_cache

import cv2
import fitz
import numpy as np
from PIL import Image

from extraction import SCALE, glyph_signature, glyph_usable, glyph_verified, profile_coverage, pt_box
from storage import DATA, dumps, glyph_record, one, page_record, rows, uid

SYMBOLS = {"alpha":"α","beta":"β","theta":"θ","pi":"π","Delta":"Δ",
           "lambda":"λ","mu":"μ","sigma":"σ","omega":"ω","infty":"∞",
           "times":"×","cdot":"·","div":"÷","pm":"±","le":"≤","leq":"≤",
           "ge":"≥","geq":"≥","ne":"≠","neq":"≠","to":"→","rightarrow":"→",
           "Rightarrow":"⇒","Leftrightarrow":"⇔","iff":"⇔","cup":"∪","cap":"∩","approx":"≈",
           "partial":"∂","nabla":"∇","int":"∫","sum":"∑","in":"∈","notin":"∉"}
BLACKBOARD_SETS = {"C":"ℂ","H":"ℍ","N":"ℕ","P":"ℙ","Q":"ℚ","R":"ℝ","Z":"ℤ"}
FUNCTIONS = {"sin","cos","tan","arcsin","arccos","arctan","sec","csc","cot","sinh","cosh","tanh","ln","log","exp","lim","det","max","min"}


class MathParser:
    def __init__(self, source, legacy_text=False):
        pattern=r"\\\\|\\[A-Za-z]+|\\.|[^\s]" + ("" if legacy_text else r"|\s+")
        self.tokens = re.findall(pattern, source)
        self.i = 0;self.legacy_text=legacy_text

    def peek(self):
        while self.i<len(self.tokens) and self.tokens[self.i].isspace():self.i+=1
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def pop(self):
        t=self.peek()
        if t is None: raise ValueError("公式意外结束，请检查括号和花括号")
        self.i+=1
        return t

    def group(self):
        if self.peek()=="{":
            self.pop(); result=self.seq({"}"})
            if self.pop()!="}": raise ValueError("公式缺少 }")
            return result
        return self.atom()

    def atom(self):
        t=self.pop()
        if t=="{":
            result=self.seq({"}"})
            if self.pop()!="}": raise ValueError("公式缺少 }")
            return result
        if t in ("(","["):
            expected=")" if t=="(" else "]"
            inner=self.seq({")","]"});closing=self.pop()
            if closing!=expected and not any(c.get("type")=="char" and c.get("value")=="," for c in inner["children"]):
                raise ValueError(f"公式缺少 {expected}")
            return {"type":"delimited","left":t,"right":closing,"body":inner}
        if t in (r"\frac",r"\dfrac",r"\tfrac"): return {"type":"frac","numerator":self.group(),"denominator":self.group()}
        if t in (r"\vec",r"\overrightarrow"):return {"type":"vector","body":self.group()}
        if t==r"\sqrt":
            index=None
            if self.peek()=="[":
                self.pop();index=self.seq({"]"})
                if self.pop()!="]":raise ValueError("根号指数缺少 ]")
            return {"type":"sqrt","body":self.group(),"index":index}
        if t==r"\begin":
            self.pop_expected("{")
            env=""
            while self.peek()!="}":env+=self.pop()
            self.pop_expected("}")
            if env not in {"matrix","pmatrix","bmatrix"}:
                raise ValueError(f"暂不支持环境 {env}；可使用 matrix、pmatrix 或 bmatrix")
            matrix_rows=[];current=[]
            while True:
                current.append(self.seq({"&",r"\\",r"\end"}))
                separator=self.pop()
                if separator=="&":continue
                matrix_rows.append(current);current=[]
                if separator==r"\end":break
            self.pop_expected("{")
            end=""
            while self.peek()!="}":end+=self.pop()
            self.pop_expected("}")
            if end!=env:raise ValueError("矩阵 begin/end 不匹配")
            if len({len(r) for r in matrix_rows})!=1:raise ValueError("矩阵每行的列数必须一致")
            if len(matrix_rows)>10 or len(matrix_rows[0])>10:raise ValueError("首版矩阵最多 10×10")
            return {"type":"matrix","rows":matrix_rows,"bracket":env}
        if t==r"\text" and not self.legacy_text:
            self.pop_expected("{");parts=[];depth=1
            while self.i<len(self.tokens):
                value=self.tokens[self.i];self.i+=1
                if value=="{":depth+=1;continue
                if value=="}":
                    depth-=1
                    if not depth:break
                    continue
                if value.startswith("\\"):
                    if len(value)!=2 or value[1] not in "{}%_$#& ":raise ValueError("文本中的排版命令暂不支持，请直接输入文字")
                    value=value[1]
                parts.append(value)
            if depth:raise ValueError("公式文字缺少 }")
            return {"type":"text","value":"".join(parts)}
        if t==r"\mathbb":
            body=self.group()
            if body.get("type")=="seq" and len(body["children"])==1:body=body["children"][0]
            letter=body.get("value") if body.get("type")=="char" else None
            if letter not in BLACKBOARD_SETS:raise ValueError("暂不支持此黑板粗体符号；没有忽略该命令。")
            return {"type":"char","value":BLACKBOARD_SETS[letter]}
        if t in (r"\text",r"\mathrm",r"\operatorname",r"\mathbf"):
            return self.group()
        if t in (r"\,",r"\;",r"\quad",r"\qquad",r"\ "):
            return {"type":"space","size":.25 if t in (r"\,",r"\;") else 1}
        if t.startswith("\\"):
            name=t[1:]
            if name in SYMBOLS:return {"type":"char","value":SYMBOLS[name]}
            if name in FUNCTIONS:return {"type":"seq","children":[{"type":"char","value":c} for c in name]}
            if name in "{}%_$":return {"type":"char","value":name}
            raise ValueError(f"暂不支持公式命令 {t}；请改写或在正文中输入。没有忽略该命令。")
        if t in ("}","^","_","&", ")", "]"):
            raise ValueError(f"公式位置错误：{t}")
        return {"type":"char","value":t}

    def pop_expected(self,t):
        if self.pop()!=t:raise ValueError(f"公式预期 {t}")

    def seq(self,stops=None):
        children=[]
        while self.peek() is not None and self.peek() not in (stops or set()):
            if self.peek() in (r"\left",r"\right",r"\displaystyle"):
                self.pop();continue
            base=self.atom()
            sup=sub=None;primes=0
            while self.peek() in ("^","_","'","′"):
                op=self.pop()
                if op in ("'","′"):primes+=1
                elif op=="^":
                    if sup:raise ValueError("重复上标，请使用花括号合并")
                    sup=self.group()
                else:
                    if sub:raise ValueError("重复下标，请使用花括号合并")
                    sub=self.group()
            if primes:
                prime={"type":"primes","count":primes}
                sup={"type":"seq","children":[prime,sup]} if sup else prime
            if sup or sub:base={"type":"scripts","base":base,"sup":sup,"sub":sub}
            children.append(base)
        return {"type":"seq","children":children}


def parse_document_legacy(text):
    if not isinstance(text,str) or len(text)>30000:
        raise ValueError("每个答案区域最多 30000 字符")
    text=text.replace("$$","$")
    lines=[]
    for source in text.split("\n"):
        pieces=source.split("$")
        if len(pieces)%2==0:raise ValueError("公式 $ 未成对；请使用 $公式$，矩阵在同一行输入")
        runs=[]
        for i,piece in enumerate(pieces):
            if not piece:continue
            if i%2:
                parser=MathParser(piece,legacy_text=True);ast=parser.seq()
                if parser.peek() is not None:raise ValueError("公式未完整解析")
                runs.append({"type":"math","body":ast})
            else:runs.append({"type":"text","value":piece})
        lines.append({"type":"line","runs":runs})
    return {"type":"document","lines":lines,"version":1}


def parse_document(text,legacy_text=False):
    """Parse common LaTeX delimiters without changing the saved answer text."""
    if not isinstance(text,str) or len(text)>30000:raise ValueError("每个答案区域最多 30000 字符")
    lines=[{"type":"line","runs":[]}];plain=[];i=0
    def escaped(pos):
        count=0;pos-=1
        while pos>=0 and text[pos]=="\\":count+=1;pos-=1
        return count%2==1
    def flush():
        if plain:lines[-1]["runs"].append({"type":"text","value":"".join(plain)});plain.clear()
    while i<len(text):
        if text[i]=="\n":flush();lines.append({"type":"line","runs":[]});i+=1;continue
        if text.startswith(r"\$",i) and not escaped(i):plain.append("$");i+=2;continue
        opening=closing=None
        if text.startswith(r"\(",i) and not escaped(i):opening=r"\(";closing=r"\)"
        elif text.startswith(r"\[",i) and not escaped(i):opening=r"\[";closing=r"\]"
        elif text.startswith("$$",i) and not escaped(i):opening=closing="$$"
        elif text[i]=="$" and not escaped(i):opening=closing="$"
        elif (text.startswith(r"\)",i) or text.startswith(r"\]",i)) and not escaped(i):raise ValueError("公式结束标记未配对")
        if not opening:plain.append(text[i]);i+=1;continue
        flush();start=i+len(opening);end=start
        while end<len(text) and not (text.startswith(closing,end) and not escaped(end)):end+=1
        if end>=len(text):raise ValueError("公式标记未配对；请检查 $、\\(...\\) 或 \\[...\\]")
        body=text[start:end]
        if opening=="$" and "\n" in body:
            before=text[text.rfind("\n",0,i)+1:i];after=text[end+1:text.find("\n",end+1) if "\n" in text[end+1:] else len(text)]
            if before.strip() or (after.strip() and not re.fullmatch(r"[.,;:!?]+",after.strip())):raise ValueError("跨行 $ 公式的起止标记须各占一行")
        if body:
            parser=MathParser(body,legacy_text=legacy_text);ast=parser.seq()
            if parser.peek() is not None:raise ValueError("公式未完整解析")
            lines[-1]["runs"].append({"type":"math","body":ast})
        i=end+len(closing)
    flush();return {"type":"document","lines":lines,"version":1}


@dataclass
class Box:
    width: float=0
    ascent: float=0
    descent: float=0
    ops: list=field(default_factory=list)
    equal_x: float|None=None


def shift(box,x=0,y=0):
    result=[]
    for op in box.ops:
        n=dict(op)
        for key in ("rect","ink_rect"):
            if key in n:n[key]=[n[key][0]+x,n[key][1]+y,n[key][2]+x,n[key][3]+y]
        if "points" in n:n["points"]=[[a+x,b+y] for a,b in n["points"]]
        result.append(n)
    return result


def sequence(boxes,gap=0):
    x=0;ops=[];equal_x=None
    for i,b in enumerate(boxes):
        if i:x+=gap
        if equal_x is None and b.equal_x is not None:equal_x=x+b.equal_x
        ops.extend(shift(b,x));x+=b.width
    return Box(x,max([b.ascent for b in boxes]+[0]),max([b.descent for b in boxes]+[0]),ops,equal_x)


def visible_text(value,first=False):
    """Remove supported Markdown presentation markers without changing stored source."""
    if first:
        value=re.sub(r"^#{1,6}\s+","",value)
        value=re.sub(r"^[-*]\s+","• ",value)
    return re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__|`([^`]+)`",
                  lambda match:next(x for x in match.groups() if x is not None),value)


UNIT_PART=r"(?:mm|cm|km|m|in|ft|yd|mi|mg|kg|g|mL|L|ms|s|sec|min|h|hr|N|Pa|J|W|Hz|°C|°F|K|%)"
UNIT_POWER=r"(?:\^(?:\{[+-]?\d+\}|[+-]?\d+)|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻]+)?"
UNIT_RE=re.compile(rf"^{UNIT_PART}{UNIT_POWER}(?:[/*·×]{UNIT_PART}{UNIT_POWER})*[.,;:!?]?$",re.IGNORECASE)


def simple_math_text(node):
    if not node:return ""
    if node["type"]=="char":return node["value"]
    if node["type"]=="text":return node["value"]
    if node["type"]=="space":return ""
    if node["type"]=="seq":
        parts=[simple_math_text(child) for child in node["children"]]
        return None if any(part is None for part in parts) else "".join(parts)
    if node["type"]=="scripts" and not node["sub"]:
        base=simple_math_text(node["base"]);sup=simple_math_text(node["sup"])
        if base is not None and sup is not None and re.fullmatch(r"[+-]?\d+",sup):return base+"^"+sup
    return None


def ends_in_number(run):
    if run["type"]=="text":return bool(re.search(r"(?<![A-Za-z0-9_])[-+]?(?:\d+(?:\.\d*)?|\.\d+)$",visible_text(run["value"]).strip()))
    node=run["body"]
    while node and node["type"]=="seq":
        children=[child for child in node["children"] if child["type"]!="space"]
        node=children[-1] if children else None
    if not node:return False
    if node["type"]=="char":return node["value"].isdigit()
    if node["type"]=="scripts":return bool(simple_math_text(node) and (simple_math_text(node)[0].isdigit()))
    if node["type"]=="frac":
        numerator=simple_math_text(node["numerator"]);denominator=simple_math_text(node["denominator"])
        return bool(numerator and denominator and numerator.isdigit() and denominator.isdigit())
    return False


def unit_line_runs(lines):
    """Keep a standalone unit with the preceding numeric result without changing the saved AST."""
    joined=[]
    for line in lines:
        runs=line["runs"][:];unit=[]
        for run in runs:
            value=visible_text(run["value"]) if run["type"]=="text" else simple_math_text(run["body"])
            if value is None:unit=[];break
            unit.append(value)
        value=re.sub(r"\s+","","".join(unit))
        previous=joined[-1]["runs"] if joined else []
        last=next((run for run in reversed(previous) if run["type"]!="text" or run["value"].strip()),None)
        if value and UNIT_RE.fullmatch(value) and last and ends_in_number(last):
            if not (previous[-1]["type"]=="text" and previous[-1]["value"][-1:].isspace()):previous.append({"type":"text","value":" "})
            previous.extend(runs)
        else:joined.append({"type":"line","runs":runs})
    return joined


@lru_cache(maxsize=4096)
def _ink_clip(path,label,modified):
    """Trim transparent margins only; never remove a disconnected ink stroke."""
    with Image.open(path) as image:alpha=np.asarray(image.convert("RGBA"))[:,:,3]
    height,width=alpha.shape;full=(0,0,width,height)
    if not re.fullmatch(r"[A-Za-z0-9]+",label):return full
    ys,xs=np.where(alpha>4)
    if not len(xs):return full
    return (max(0,int(xs.min())-1),max(0,int(ys.min())-1),min(width,int(xs.max())+2),min(height,int(ys.max())+2))


@lru_cache(maxsize=4096)
def _visible_ink(path,clip,modified):
    with Image.open(path) as image:alpha=np.asarray(image.convert("RGBA").crop(clip))[:,:,3]
    ys,xs=np.where(alpha>4)
    return (int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1) if len(xs) else (0,0,alpha.shape[1],alpha.shape[0])


@lru_cache(maxsize=1024)
def _word_baseline_fraction(path,clip,modified):
    with Image.open(path) as image:mask=np.asarray(image.convert('RGBA').crop(clip))[:,:,3]>40
    ys,xs=np.where(mask)
    if not len(xs):return None
    bottoms=[np.flatnonzero(mask[:,x])[-1] for x in range(mask.shape[1]) if mask[:,x].any()]
    return float((np.median(bottoms)-ys.min()+1)/max(1,ys.max()-ys.min()+1))


def _letter_vertical_size(label,size):
    """Use a common baseline and x-height while preserving ascenders and descenders."""
    tall=any(c.isupper() or c.isdigit() or c in 'bdfhklt' for c in label)
    dotted=any(c in 'ij' for c in label)
    ascent=size*(1 if tall else .92 if dotted else .86)
    descent=size*.18 if any(c in 'gjpqy' for c in label) else 0
    return ascent+descent,ascent


class Composer:
    def __init__(self,profile_id,seed,settings):
        glyphs=[glyph_record(g) for g in rows("SELECT * FROM glyphs WHERE profile_id=? AND status='accepted' AND origin='extracted'",(profile_id,))]
        self.library={}
        for g in glyphs:
            if g["classification"]=="handwriting" and g["label"] and glyph_usable(g):
                self.library.setdefault(g["label"],[]).append(g)
        self.excluded_inferred=set()
        if settings.get("allow_inferred",True):
            from inference import inferred_recognizable
            predicted=[glyph_record(g) for g in rows("SELECT * FROM glyphs WHERE profile_id=? AND status='accepted' AND origin='inferred'",(profile_id,))]
            real_labels=set(self.library)
            for g in predicted:
                path=DATA/g["image"]
                check=g['metrics'].get('automatic_verification')
                if check and check.get('accepted') is not True and path.is_file() and check.get('signature')==glyph_signature(g):
                    self.excluded_inferred.add(g['label']);continue
                if g["label"] not in real_labels and path.is_file() and not inferred_recognizable(g["label"],str(path),path.stat().st_mtime_ns):
                    self.excluded_inferred.add(g["label"]);continue
                if g["label"] not in real_labels:self.library.setdefault(g["label"],[]).append(g)
            # A source-derived variant can be explicitly preferred over generic
            # AI guesses, but it remains inferred and still requires review.
            for label in set(self.library)-real_labels:
                preferred=[g for g in self.library[label] if g['metrics'].get('source_type')=='derived_from_real'
                           and g['metrics'].get('preferred_derived')]
                if preferred:self.library[label]=preferred
        # Prefer an intact real word over a mixture of real and inferred letters.
        # Extremely narrow word crops remain excluded when their letters exist.
        for label in list(self.library):
            if not re.fullmatch(r"[A-Za-z]{4,}",label) or not all(c in self.library for c in label):continue
            all_real=all(any(g['origin']=='extracted' for g in self.library[c]) for c in label)
            plausible=[]
            for g in self.library[label]:
                if g['origin']!='extracted':continue
                path=DATA/g['image'];clip=_ink_clip(str(path),label,path.stat().st_mtime_ns)
                x0,y0,x1,y1=_visible_ink(str(path),clip,path.stat().st_mtime_ns)
                if all_real and any(c in 'fgjpqy' for c in label):
                    fraction=_word_baseline_fraction(str(path),clip,path.stat().st_mtime_ns)
                    # A very deep crop can pull the word far below its line.
                    # Use individually extracted letters only when all exist.
                    if fraction is not None and fraction<.55:continue
                if x1-x0 >= (y1-y0)*len(label)*.22:plausible.append(g)
            if plausible:self.library[label]=plausible
            else:del self.library[label]
        self.inferred=set();self.derived=set();self.clips={}
        self.rng=random.Random(str(seed));self.previous={};self.context=None
        self.missing=set();self.single_variants=set();self.settings=settings;self.word_sizes=None
        self.coverage=profile_coverage(profile_id)
        style=self.coverage["style"]
        self.native_height=max(5,style.get("height",{}).get("median",14))
        self.gap=min(.12,max(.02,style.get("letter_gap",{}).get("median",.8)/self.native_height))*settings.get("letter_spacing",.7)
        self.word_gap=min(.42,max(.22,style.get("word_gap",{}).get("median",4)/self.native_height))*settings.get("word_spacing",1)
        self.stroke=max(.025,min(.075,style.get("stroke_width",{}).get("median",.7)/self.native_height))

    def word_size_profile(self):
        if self.word_sizes is not None:return self.word_sizes
        sizes={};f_baselines=[];descender_baselines={}
        for label in 'acemnorsuvwxzbdhklitfgjpqyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789':
            values=[]
            for g in self.library.get(label,[]):
                if g['origin']!='extracted':continue
                path=DATA/g['image']
                if not path.is_file():continue
                modified=path.stat().st_mtime_ns;clip=_ink_clip(str(path),label,modified)
                _,y0,_,y1=_visible_ink(str(path),clip,modified)
                scale=g['metrics'].get('image_scale',SCALE)
                if isinstance(scale,(int,float)) and math.isfinite(scale) and scale>0 and y1>y0:
                    values.append((y1-y0)/scale)
                if label=='f':
                    base=g['metrics'].get('baseline')
                    if isinstance(base,(int,float)) and .35<=base<=.98:f_baselines.append(base)
                if label in 'gjpqy':
                    base=g['metrics'].get('baseline')
                    if isinstance(base,(int,float)) and .45<=base<=.95:
                        descender_baselines.setdefault(label,[]).append(float(base))
            if values:sizes[label]=float(np.median(values))
        x=[sizes[c] for c in 'acemnorsuvwxz' if c in sizes]
        asc=[sizes[c] for c in 'bdhkl' if c in sizes]
        calibrated=len(x)>=3 and len(asc)>=2
        ref=float(np.median(asc)) if calibrated else None
        x_ratio=max(.48,min(.86,float(np.median(x))/ref)) if calibrated else .72
        def ratio(label,default,upper=1.):
            value=sizes.get(label,0)/ref if ref else default
            return max(x_ratio+.05,min(upper,value)) if label in sizes and ref else max(x_ratio+.05,default)
        letter_heights={label:ratio(label,1.,1.25) for label in sizes}
        caps=[sizes[c] for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if c in sizes]
        digit_sizes=[sizes[c] for c in '0123456789' if c in sizes]
        # Compare real x and digits on the same source line. Cross-page median
        # heights can otherwise confuse a change of writing size with style.
        def source_box(g):
            box=g['metrics'].get('image_ink_bbox') or g['metrics'].get('ink_bbox')
            if not isinstance(box,(list,tuple)) or len(box)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in box):return None
            return box if box[2]>box[0] and box[3]>box[1] else None
        xs=[g for g in self.library.get('x',[]) if g['origin']=='extracted' and g['document_id'] is not None and g['page'] is not None and source_box(g)]
        digit_samples=[g for label in '0123456789' for g in self.library.get(label,[]) if g['origin']=='extracted' and source_box(g)]
        pairs=[];used=set()
        for sample in sorted(xs,key=lambda g:(g['document_id'],g['page'],source_box(g)[1],source_box(g)[0])):
            xb=source_box(sample);xh=xb[3]-xb[1];near=[]
            for d in digit_samples:
                if d['id'] in used or sample['document_id']!=d['document_id'] or sample['page']!=d['page']:continue
                db=source_box(d);dh=db[3]-db[1]
                if xb[0]<db[2] and db[0]<xb[2]:continue
                gap=max(db[0]-xb[2],xb[0]-db[2])
                if .5<=xh/dh<=1.2 and gap<=max(30,6*max(xh,dh)) and abs(xb[3]-db[3])<=max(3,.25*max(xh,dh)):
                    near.append((gap,d['id'],xh/dh))
            if near:
                _,digit_id,value=min(near);used.add(digit_id);pairs.append(value)
        math_x=max(.65,min(.95,float(np.median(pairs)))) if len(pairs)>=3 else .86
        self.word_sizes={'x_height':x_ratio,'i_height':ratio('i',.86),'t_height':max(.75,ratio('t',.9)),
                         'f_baseline':float(np.median(f_baselines)) if f_baselines else None,
                         'descender_baselines':{c:float(np.median(v)) for c,v in descender_baselines.items()},
                         'letter_heights':letter_heights,
                         'cap_height':max(x_ratio+.05,min(1.25,float(np.median(caps))/ref)) if ref and len(caps)>=2 else 1.,
                         'digit_height':max(x_ratio+.05,min(1.25,float(np.median(digit_sizes))/ref)) if ref and len(digit_sizes)>=3 else 1.,
                         'calibrated':calibrated,'x_labels':len(x),'ascender_labels':len(asc),
                         'math_x_height':math_x,'math_pairs':len(pairs)}
        return self.word_sizes

    def glyph(self,label,size,word=False):
        pool=self.library.get(label,[])
        if not pool:
            self.missing.add(label)
            width=max(.28,fitz.get_text_length(label if label.isascii() else "?",fontsize=1))*size
            return Box(width,.82*size,.18*size,[{"kind":"fallback","label":label,"rect":[0,-.82*size,width,.18*size],"size":size}])
        verified=[g for g in pool if glyph_verified(g)]
        if verified:pool=verified
        if label=="√":
            shaped=[g for g in pool if g["metrics"].get("radical_slices")]
            if shaped:pool=shaped
        candidates=[g for g in pool if g["id"]!=self.previous.get(label)] or pool
        # Small superscripts can become disproportionately heavy when enlarged
        # to body size. Prefer a full-size real sample when another is available.
        if len(label)==1 and label.isalnum() and size>=self.settings.get("font_size",size)*.9:
            suitable=[g for g in candidates if g["origin"]!="extracted" or g["metrics"]["height"]>=self.native_height*.7]
            if suitable:candidates=suitable
        if len(pool)==1:self.single_variants.add(label)
        def score(g):
            m=g["metrics"]
            value=self.rng.random()*.35
            if self.context:
                value+=abs(m["stroke_width"]-self.context["metrics"]["stroke_width"])*.25
                value+=abs(m["slant_degrees"]-self.context["metrics"]["slant_degrees"])*.007
                if g["document_id"]==self.context["document_id"] and g["page"]==self.context["page"]:value-=.15
            return value
        g=min(candidates,key=score);self.previous[label]=g["id"];self.context=g
        m=g["metrics"]
        if g["origin"]=="inferred":
            (self.derived if m.get('source_type')=='derived_from_real' else self.inferred).add(label)
        word_or_name=bool(re.fullmatch(r"[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*",label))
        measured_word_baseline=False
        if word_or_name:
            height,baseline=_letter_vertical_size(label,size)
            if len(label)==1:
                stats=self.word_size_profile()
                short_height=size*(stats['x_height'] if word else stats['math_x_height']*stats['digit_height'])
                if label in 'acemnorsuvwxz':height=baseline=short_height
                elif label.isupper():height=baseline=size*stats['cap_height']
                elif label.isdigit():height=baseline=size*stats['digit_height']
                elif label in 'bdhkl':height=baseline=size
                elif label in 'gjpqy':
                    fraction=max(.45,min(.95,stats['descender_baselines'].get(label,.76)))
                    baseline=short_height;height=baseline/fraction
                elif word and label=='i':height=baseline=size*stats['i_height']
                elif label=='t':height=baseline=size*stats['t_height']
                elif label=='f' and stats['f_baseline'] is not None:
                    baseline=size;height=size*(1+min(.65,1/stats['f_baseline']-1))
            else:
                stats=self.word_size_profile()
                if any(c in 'bdhklf' for c in label):height=baseline=size
                elif any(c.isupper() for c in label):height=baseline=size*stats['cap_height']
                elif any(c.isdigit() for c in label):height=baseline=size*stats['digit_height']
                elif 't' in label:height=baseline=size*stats['t_height']
                elif any(c in 'ij' for c in label):height=baseline=size*stats['i_height']
                else:height=baseline=size*stats['x_height']
                if word and g['origin']=='extracted' and any(c in 'fgjpqy' for c in label):
                    path=DATA/g['image'];clip=_ink_clip(str(path),label,path.stat().st_mtime_ns)
                    fraction=_word_baseline_fraction(str(path),clip,path.stat().st_mtime_ns)
                    if fraction is not None and .35<=fraction<.8:
                        height=min(size*1.6,baseline/fraction);baseline=height*fraction;measured_word_baseline=True
        else:
            factor=.10 if label=='.' else .42 if label==':' else .28 if label in ',;' else .12 if label=='-' else .32 if label=='=' else .52 if label in '+<>' else .62 if label in '≠≤≥±÷×∈' else .86 if label in 'ℂℍℕℙℚℝℤ' else .74
            height=size*factor;baseline=m["baseline"]*height
        if g["id"] not in self.clips:
            path=DATA/g["image"];self.clips[g["id"]]=_ink_clip(str(path),label,path.stat().st_mtime_ns)
        clip=self.clips[g["id"]];width=height*(clip[2]-clip[0])/max(1,clip[3]-clip[1]);rect=None
        if word_or_name:
            path=DATA/g["image"];x0,y0,x1,y1=_visible_ink(str(path),clip,path.stat().st_mtime_ns)
            scale=height/max(1,y1-y0);width=(x1-x0)*scale
            rect=[-x0*scale,-baseline-y0*scale,width+(clip[2]-clip[0]-x1)*scale,
                  height-baseline+(clip[3]-clip[1]-y1)*scale]
        # Tiny original operator crops otherwise become dots after normalization.
        # Keep the real strokes, but give the horizontal symbols a readable width.
        if label=="=":width=max(width,size*.5)
        if label=="-":width=max(width,size*.42)
        if label==":":width=max(width,size*.16)
        # Operators occupy the centre of the mathematical x-height.
        if label in ',;':baseline=height*.72
        if label in '-=+<>≠≤≥±÷×∈/:' or label=='⇒':baseline=height*.5+size*.26
        op={"kind":"glyph","glyph_id":g["id"],"label":label,"image":g["image"],
            "rect":rect or [0,-baseline,width,height-baseline],"origin":g["origin"],
            "ink_clip":clip,"radical_slices":m.get("radical_slices") if label=="√" else None,
            "stroke_target":self.stroke*self.settings.get("font_size",size) if self.settings.get("uniform_stroke",True) else None}
        # Tight crops need transparent room for stroke calibration, including
        # genuine samples; never alter the stored source image.
        if (word_or_name or (g['origin']=='inferred' and len(label)==1)) and label not in '()[]√→' and op['stroke_target']:
            pad=max(.25,min(1.,op['stroke_target']*.8));op['padding']=pad
            x0,y0,x1,y1=op['rect'];op['rect']=[x0-pad,y0-pad,x1+pad,y1+pad]
        if rect is not None:
            op["ink_rect"]=[0,-baseline,width,height-baseline]
            descender=(len(label)==1 and (label in 'gjpqy' or (label=='f' and height>baseline)))
            if descender or measured_word_baseline:
                offset=-size*.10
            elif not any(c in 'fgjpqy' for c in label):
                offset=_visual_baseline_offset(op,size,self.settings.get("ink_weight",1))
            else:offset=0
            if offset:
                for key in ("rect","ink_rect"):
                    op[key][1]+=offset;op[key][3]+=offset
                baseline-=offset
        if op.get('padding'):
            alpha=_glyph_alpha(op,self.settings.get('ink_weight',1));ys,xs=np.where(alpha>40)
            if len(xs):
                x0,y0,x1,y1=op['rect'];sx=(x1-x0)/alpha.shape[1];sy=(y1-y0)/alpha.shape[0]
                op['ink_rect']=[x0+xs.min()*sx,y0+ys.min()*sy,x0+(xs.max()+1)*sx,y0+(ys.max()+1)*sy]
                left=min(0,op['ink_rect'][0]);width=max(.1,op['ink_rect'][2]-left)
                if left:
                    for key in ('rect','ink_rect'):op[key][0]-=left;op[key][2]-=left
                return Box(width,max(baseline,-op['ink_rect'][1]),max(0,height-baseline,op['ink_rect'][3]),[op])
        return Box(width,baseline,max(0,height-baseline),[op])

    def line_width(self,size):
        return self.stroke*(self.settings.get("font_size",size) if self.settings.get("uniform_stroke",True) else size)

    def text_word(self,word,size,nesting=None,align_equals=False):
        self.context=None
        boxes=[];i=0;previous='';previous_box=None;word_id=uid();nesting=nesting if nesting is not None else [0]
        word_spans=[m.span() for m in re.finditer(r'[A-Za-z]{2,}',word)]
        labels=sorted([x for x in self.library if len(x)>1 and (not align_equals or '=' not in x)],key=len,reverse=True)
        while i<len(word):
            match=next((x for x in labels if word.startswith(x,i) and
                        (len(x)<4 or not x.isalpha() or
                         (not word[i-1:i].isalpha() and not word[i+len(x):i+len(x)+1].isalpha()))),word[i])
            is_word=any(start<=i and i+len(match)<=end for start,end in word_spans)
            box=self.glyph(match,size,word=is_word)
            if match=='=' and nesting[0]==0:box.equal_x=0
            for char in match:
                if char in '([{':nesting[0]+=1
                elif char in ')]}':nesting[0]=max(0,nesting[0]-1)
            if boxes:
                # Words should read as units; punctuation and numbers keep their normal spacing.
                factor=.45 if previous[-1:].isalpha() and match[:1].isalpha() else 1
                gap=self.gap*size*factor
                if factor<1:
                    gap=_optical_word_gap(previous_box,box,gap,size,self.settings.get("ink_weight",1))
                    gap=_safe_math_gap(previous_box,box,gap,size,self.settings.get("ink_weight",1))
                    if previous_box and len(previous_box.ops)==len(box.ops)==1 and previous_box.ops[0].get("kind")==box.ops[0].get("kind")=="glyph":gap+=.65
                boxes.append(Box(gap))
            for op in box.ops:op['word_id']=word_id
            boxes.append(box);previous=match;previous_box=box;i+=len(match)
        return sequence(boxes)

    def math_gap(self,children,i,size):
        left,right=children[i-1],children[i]
        if left["type"]=="space" or right["type"]=="space":return 0
        char=lambda node:node.get("value","") if node.get("type")=="char" else ""
        a,b=char(left),char(right);relations="=<>≤≥≠≈∈∉";binary="+-±×·÷"
        has=lambda value,group:bool(value) and value in group
        unary_left=has(a,"+-±") and (i==1 or has(char(children[i-2]),relations+binary) or children[i-2]["type"]=="space")
        unary_right=has(b,"+-±") and has(a,relations+binary)
        def function(node):
            if node["type"]=="scripts":return function(node["base"])
            parts=node.get("children",[]) if node["type"]=="seq" else []
            return bool(parts) and all(n.get("type")=="char" for n in parts) and "".join(n["value"] for n in parts) in FUNCTIONS
        gap=self.gap*size;spacing=self.settings.get("letter_spacing",.7)
        if left["type"]=="text" or right["type"]=="text":
            if (left["type"]=="text" and left["value"][-1:].isspace()) or (right["type"]=="text" and right["value"][:1].isspace()):return gap
            return max(gap,size*self.word_gap)
        if (a=='.' and b.isdigit()) or (b=='.' and a.isdigit()):return min(gap,size*.03*spacing)
        if has(a,relations) or has(b,relations):return max(gap,size*.14*spacing)
        if (has(a,binary) and not unary_left) or (has(b,binary) and not unary_right):return max(gap,size*.13*spacing)
        if has(b,",;:."):return min(gap,size*.06*spacing)
        if has(a,",;:"):return max(gap,size*.12*spacing)
        if function(left) or function(right):return max(gap,size*.12*spacing)
        if left["type"]=="delimited" and right["type"]=="delimited":return max(gap,size*.12*spacing)
        return gap

    def math(self,node,size):
        if node is None:return Box()
        t=node["type"]
        if t=="char":
            value=node["value"]
            if value=="∉" and not self.library.get(value):
                body=self.math({"type":"char","value":"∈"},size)
                ops=list(body.ops)+[{"kind":"line","points":[[body.width*.15,-size*.03],[body.width*.85,-size*.63]],"width":self.line_width(size),"role":"membership_not"}]
                return Box(body.width,max(body.ascent,size*.63),body.descent,ops)
            if value=="ℝ" and not self.library.get(value) and self.library.get("R"):
                body=self.glyph("R",size);pad=size*.2
                ops=shift(body,pad)
                ops.append({"kind":"line","points":[[size*.05,-body.ascent],[size*.05,body.descent]],"width":self.line_width(size),"role":"blackboard_R"})
                return Box(body.width+pad,body.ascent,body.descent,ops)
            if value in "∪∩⇔≈∈" and not self.library.get(value):
                w=size*.7;weight=self.line_width(size)
                if value in "∪∩":
                    y1,y2=(-size*.58,-size*.10) if value=="∪" else (-size*.10,-size*.58)
                    ops=[{"kind":"curve","points":[[0,y1],[0,y2],[w,y2],[w,y1]],"width":weight,"role":"set_operator"}]
                elif value=="∈":
                    ops=[{"kind":"curve","points":[[w,-size*.58],[0,-size*.58],[0,-size*.08],[w,-size*.08]],"width":weight,"role":"membership"},
                         {"kind":"line","points":[[w*.18,-size*.33],[w,-size*.33]],"width":weight,"role":"membership"}]
                elif value=="≈":
                    ops=[{"kind":"curve","points":[[0,y],[w*.3,y-size*.13],[w*.7,y+size*.13],[w,y]],"width":weight,"role":"approximation"} for y in (-size*.43,-size*.21)]
                else:
                    ops=[{"kind":"line","points":[[0,-size*.4],[w,-size*.4]],"width":weight,"role":"equivalence"},
                         {"kind":"line","points":[[0,-size*.23],[w,-size*.23]],"width":weight,"role":"equivalence"},
                         {"kind":"line","points":[[size*.16,-size*.55],[0,-size*.315],[size*.16,-size*.08]],"width":weight,"role":"equivalence"},
                         {"kind":"line","points":[[w-size*.16,-size*.55],[w,-size*.315],[w-size*.16,-size*.08]],"width":weight,"role":"equivalence"}]
                return Box(w,size*.6,0,ops)
            return self.glyph(value,size)
        if t=="text":
            return sequence([Box(size*self.word_gap) if part.isspace() else self.text_word(part,size)
                             for part in re.findall(r"\s+|\S+",node["value"])])
        if t=="seq":
            children=node["children"]
            # Function names are parsed as their own sequence. Prefer a verified
            # natural handwritten word where available, without changing the AST.
            if all(n.get("type")=="char" for n in children):
                name="".join(n["value"] for n in children)
                if name in FUNCTIONS:return self.glyph(name,size) if name in self.library else self.text_word(name,size)
            boxes=[];equal_x=None;x=0;previous=None
            for i,n in enumerate(children):
                child=self.math(n,size)
                if i:
                    gap=self.math_gap(children,i,size)
                    gap=_safe_math_gap(previous,child,gap,size,self.settings.get('ink_weight',1))
                    boxes.append(Box(gap));x+=gap
                if equal_x is None and n.get('type')=='char' and n.get('value')=='=':equal_x=x
                boxes.append(child);x+=child.width;previous=child
            result=sequence(boxes);result.equal_x=equal_x
            return result
        if t=="space":return Box(size*node["size"])
        if t=="vector":
            body=self.math(node["body"],size);width=max(size*.62,body.width+size*.10)
            y=-body.ascent-size*.22;left=size*.04;right=width-size*.04;head=min(size*.18,(right-left)*.30)
            ops=shift(body,(width-body.width)/2)
            if self.library.get("→"):
                mark=self.glyph("→",size).ops[0].copy()
                mark.update(rect=[left,y-size*.12,right,y+size*.12],role="vector_arrow")
                ops.append(mark)
            else:
                ops.append({"kind":"line","points":[[left,y],[right,y]],"width":self.line_width(size),"role":"vector_arrow"})
                ops.append({"kind":"line","points":[[right-head,y-size*.10],[right,y],[right-head,y+size*.10]],"width":self.line_width(size),"role":"vector_arrowhead"})
            return Box(width,body.ascent+size*.36,body.descent,ops)
        if t=="primes":
            ops=[{"kind":"line","points":[[i*size*.22,-size*.56],[i*size*.22+size*.15,-size*.95]],"width":self.line_width(size),"role":"prime"} for i in range(node["count"])]
            return Box(size*.24*node["count"],size*.96,0,ops)
        if t=="frac":
            a=self.math(node["numerator"],size*.83);b=self.math(node["denominator"],size*.83)
            width=max(a.width,b.width)+size*.35;axis=-size*.28;pad=size*.19
            ay=axis-pad-a.descent;by=axis+pad+b.ascent
            ops=shift(a,(width-a.width)/2,ay)+shift(b,(width-b.width)/2,by)
            ops.append({"kind":"line","points":[[0,axis],[width,axis]],"width":self.line_width(size),"role":"fraction"})
            return Box(width,-(ay-a.ascent),by+b.descent,ops)
        if t=="scripts":
            b=self.math(node["base"],size);s=self.math(node["sup"],size*.62);u=self.math(node["sub"],size*.62)
            sx=b.width+size*.10
            sy=min(-size*.55,-b.ascent+size*.16)-s.descent
            # Subscripts sit beside the lower half of the base rather than
            # starting a full quarter-em below its baseline.
            uy=max(size*.12,b.descent*.35)+u.ascent*.55
            if node["sup"] and node["sub"] and sy+s.descent+size*.13>uy-u.ascent:
                uy=sy+s.descent+size*.13+u.ascent
            return Box(sx+max(s.width,u.width),max(b.ascent,-sy+s.ascent if node["sup"] else 0),
                       max(b.descent,uy+u.descent if node["sub"] else 0),b.ops+shift(s,sx,sy)+shift(u,sx,uy))
        if t=="sqrt":
            b=self.math(node["body"],size);index=self.math(node["index"],size*.48)
            head_x=index.width+size*.08 if node["index"] else 0
            head_width=size*.64;body_x=head_x+head_width+size*.16
            bar_y=-max(size*.8,b.ascent)-size*.20;bottom=max(b.descent,size*.12)+size*.08
            top=bar_y-(bottom-bar_y)*.06;right=body_x+b.width+size*.16
            ops=shift(b,body_x)+shift(index,0,bar_y+size*.22)
            if self.library.get("√"):
                mark=self.glyph("√",size).ops[0].copy()
                slices=mark.get("radical_slices")
                if slices:
                    # The original overbar slopes slightly; leave room above tall radicands.
                    bar_y-=size*.14
                    ops=shift(b,body_x)+shift(index,0,bar_y+size*.22)
                    head=slices["head"];bar=slices["bar"];anchor=slices["anchor_y"]
                    height=(bottom-bar_y)/(1-anchor);top=bar_y-anchor*height
                    mark.update(rect=[head_x,top,head_x+head_width,bottom],ink_clip=head,role="radical_head")
                    ops.append(mark)
                    left=head_x+head_width*(bar[0]-head[0])/(head[2]-head[0])
                    barop=mark.copy();barop.update(kind="stroke_image",role="radical",ink_clip=bar,
                        rect=[left,top+height*(bar[1]-head[1])/(head[3]-head[1]),right,
                              top+height*(bar[3]-head[1])/(head[3]-head[1])])
                    ops.append(barop)
                    return Box(right,-top+size*.05,max(b.descent,bottom),ops)
                with Image.open(DATA/mark["image"]) as source:
                    alpha=np.asarray(source.convert("RGBA"))[:,:,3]
                x0,y0,x1,y1=mark["ink_clip"];alpha=alpha[y0:y1,x0:x1]
                ys,xs=np.where(alpha>128)
                if not len(xs):raise ValueError("根号样本没有可用笔画，请重新校对")
                upper=ys<=ys.min()+max(1,round(alpha.shape[0]*.18))
                anchor_x=float(np.quantile(xs[upper],.8))
                near=upper & (np.abs(xs-anchor_x)<=max(1,alpha.shape[1]*.05))
                anchor_y=float(np.median(ys[near]))
                fx=(anchor_x+.5)/alpha.shape[1];fy=(anchor_y+.5)/alpha.shape[0]
                height=(bottom-bar_y)/max(.5,1-fy);top=bar_y-fy*height
                mark.update(rect=[head_x,top,head_x+head_width,bottom],role="radical_head")
                ops.append(mark)
                points=[[head_x+head_width*fx,bar_y],[right,bar_y+size*.025]]
            else:
                points=[[head_x,bottom-size*.20],[head_x+size*.16,bottom-size*.20],
                        [head_x+size*.28,bottom-size*.37],[head_x+size*.40,bottom],
                        [head_x+head_width*.82,bar_y],[right,bar_y+size*.025]]
            ops.append({"kind":"line","points":points,"width":self.line_width(size),"role":"radical"})
            return Box(right,-top+size*.05,max(b.descent,bottom),ops)
        if t=="delimited":
            b=self.math(node["body"],size)
            return self.delimit(b,node["left"],node["right"],size)
        if t=="matrix":
            rs=[[self.math(x,size*.88) for x in r] for r in node["rows"]]
            cols=[max(r[i].width for r in rs) for i in range(len(rs[0]))]
            pad=size*.6;heights=[max(b.ascent for b in r)+max(b.descent for b in r) for r in rs]
            height=sum(heights)+size*.45*(len(rs)-1);width=sum(cols)+pad*(len(cols)-1)
            ops=[];y=-height/2-size*.24
            for row,height_row in zip(rs,heights):
                baseline=y+max(b.ascent for b in row);x=0
                for b,cw in zip(row,cols):ops+=shift(b,x+(cw-b.width)/2,baseline);x+=cw+pad
                y+=height_row+size*.45
            result=Box(width,height/2+size*.24,max(0,height/2-size*.24),ops)
            if node["bracket"]=="bmatrix":return self.delimit(result,"[","]",size)
            if node["bracket"]=="pmatrix":return self.delimit(result,"(",")",size)
            return result
        raise ValueError(f"未知结构 {t}")

    def delimit(self,b,left,right,size):
        pad=size*.10*self.settings.get("letter_spacing",.7);top=-max(b.ascent,size*.78)-size*.10;bottom=max(b.descent,size*.15)+size*.10
        marks=[]
        for char in (left,right):
            if char in '()[]' and self.library.get(char):
                box=self.glyph(char,size);op=box.ops[0].copy();ratio=box.width/max(.1,box.ascent+box.descent)
                width=min(size*.55,max(size*.26,ratio*(bottom-top)))
                marks.append((op,width))
            else:marks.append((None,size*.30))
        (left_op,lw),(right_op,rw)=marks
        ops=shift(b,lw+pad);rightx=lw+pad+b.width+pad
        for char,x,side,op,bw in [(left,0,1,left_op,lw),(right,rightx,-1,right_op,rw)]:
            if op is not None:
                if char in '[]':
                    # Preserve handwritten endpoints when extending a short
                    # sample around a tall matrix; stretch only its middle.
                    x0,y0,x1,y1=op['ink_clip'];h=y1-y0
                    if h<3:
                        op.update(rect=[x,top,x+bw,bottom],role='bracket');ops.append(op)
                        continue
                    first=y0+max(1,round(h*.27));last=y1-max(1,round(h*.27))
                    cap=min(size*.4,(bottom-top)*.24,(first-y0)*bw/max(1,x1-x0))
                    overlap=min(.15,cap*.1)
                    for part,clip,y_start,y_end in [
                        ('top',[x0,y0,x1,first],top,top+cap+overlap),
                        ('middle',[x0,first,x1,last],top+cap-overlap,bottom-cap+overlap),
                        ('bottom',[x0,last,x1,y1],bottom-cap-overlap,bottom)]:
                        piece=op.copy();piece.update(kind='stroke_image',rect=[x,y_start,x+bw,y_end],ink_clip=clip,role='bracket',bracket_part=part);ops.append(piece)
                else:
                    op.update(rect=[x,top,x+bw,bottom],role="parenthesis");ops.append(op)
                continue
            if char in '[]':
                xx=x if side==1 else x+bw
                pts=[[xx+side*bw,top],[xx,top],[xx,bottom],[xx+side*bw,bottom]]
                ops.append({"kind":"line","points":pts,"width":self.line_width(size),"role":"bracket"})
            else:
                xx=x+bw if side==1 else x
                ops.append({"kind":"curve","points":[[xx,top],[xx-side*bw*1.3,top+(bottom-top)*.2],
                            [xx-side*bw*1.3,bottom-(bottom-top)*.2],[xx,bottom]],"width":self.line_width(size),"role":"parenthesis"})
        return Box(rightx+rw,-top,bottom,ops)

    def layout(self,ast,rect,size,compact_blank_lines=False):
        x0,y0,x1,y1=rect;width=x1-x0;ops=[];issues=[];y=y0;content_bottom=y0;lines=[]
        leading=size*self.settings.get("line_spacing",1.65)
        for line in unit_line_runs(ast["lines"]):
            text=''.join(run['value'] for run in line['runs'] if run['type']=='text').strip()
            # Only align standalone equations; prose with inline math keeps its normal flow.
            equation=not text or (all(run['type']=='text' for run in line['runs']) and
                                  bool(re.fullmatch(r'[\w\s+\-*/=^().,:\[\]<>≤≥≠]+',text)))
            tokens=[];nesting=[0]
            for index,run in enumerate(line["runs"]):
                if run["type"]=="math":
                    value=simple_math_text(run["body"])
                    tokens.append((self.math(run["body"],size),False,ends_in_number(run),value))
                else:
                    for value in re.findall(r"\s+|\S+",visible_text(run["value"],index==0)):
                        tokens.append((Box(size*self.word_gap) if value.isspace() else self.text_word(value,size,nesting,equation),
                                       value.isspace(),ends_in_number({"type":"text","value":value}),value))
            # The final numeric result and its unit are an indivisible wrapping token.
            grouped=[];i=0
            while i<len(tokens):
                j=i+1
                while j<len(tokens) and tokens[j][1]:j+=1
                if tokens[i][2] and j<len(tokens) and tokens[j][3] and UNIT_RE.fullmatch(tokens[j][3]):
                    end=j+1
                    while end<len(tokens):
                        connector=end
                        while connector<len(tokens) and tokens[connector][1]:connector+=1
                        part=connector+1
                        while part<len(tokens) and tokens[part][1]:part+=1
                        if connector>=len(tokens) or tokens[connector][3] not in ('/','*','·','×') or part>=len(tokens) or not tokens[part][3] or not UNIT_RE.fullmatch(tokens[part][3]):break
                        end=part+1
                    grouped.append(sequence([token[0] for token in tokens[i:end]]));i=end
                else:grouped.append(tokens[i][0]);i+=1
            line_boxes=[];line_width=0
            def flush():
                nonlocal line_boxes,line_width
                while line_boxes and not line_boxes[-1].ops:line_boxes.pop()
                lines.append((sequence(line_boxes) if line_boxes else None,equation))
                line_boxes=[];line_width=0
            for token in grouped:
                if token.width>width+.01:issues.append("单个词或公式宽度超出作答区域；请增大区域、手动换行或修改字号")
                if line_width+token.width>width and line_boxes:flush()
                if not line_boxes and not token.ops:continue
                line_boxes.append(token);line_width+=token.width
            flush()
        # An equals at the beginning of the next line explicitly continues the
        # previous expression. Other neighbouring equations are not grouped.
        indents=[0.0]*len(lines)
        for i in range(1,len(lines)):
            previous,previous_equation=lines[i-1];box,equation=lines[i]
            if not previous_equation or not equation or previous is None or box is None or previous.equal_x is None or box.equal_x is None:continue
            first=next((op for op in box.ops if op.get('kind') in ('glyph','fallback','stroke_image','line','curve')),None)
            if first and first.get('label')=='=' and abs(first['rect'][0]-box.equal_x)<.01:
                indents[i]=indents[i-1]+previous.equal_x-box.equal_x
        for (box,_),indent in zip(lines,indents):
            if box is None:y+=0 if compact_blank_lines else leading;continue
            if box.width+indent>width+.01 and indent>.01:
                issues.append('等号对齐后超出作答区域；请增大区域或调整换行')
            baseline=y+max(box.ascent,size*.85)
            ops+=shift(box,x0+indent,baseline)
            content_bottom=baseline+max(box.descent,size*.15)
            y=content_bottom+max(size*.3,leading-size)
        if content_bottom>y1+.1:issues.append(f"答案高度超出区域约 {content_bottom-y1:.1f} pt；请扩展区域或移动部分步骤到另一明确区域")
        return ops,issues


def _erosion_preserves_strokes(original,adjusted):
    before=original>100;after=adjusted>100
    count,labels,stats,_=cv2.connectedComponentsWithStats(before.astype(np.uint8),8)
    for index in range(1,count):
        x,y,w,h,area=stats[index]
        if area<2:continue
        retained=(labels[y:y+h,x:x+w]==index)&after[y:y+h,x:x+w]
        if retained.sum()<area*.75:return False
        parts,_,pieces,_=cv2.connectedComponentsWithStats(retained.astype(np.uint8),8)
        if sum(piece[4]>=max(2,area*.03) for piece in pieces[1:parts])>1:return False
    return True


def _dilation_preserves_strokes(original,adjusted):
    before=np.pad((original>100).astype(np.uint8),1)
    after=np.pad((adjusted>100).astype(np.uint8),1)
    # Keep separate marks separate and preserve enclosed counters such as e/o.
    return (cv2.connectedComponents(after,8)[0]>=cv2.connectedComponents(before,8)[0]
            and cv2.connectedComponents(1-after,8)[0]>=cv2.connectedComponents(1-before,8)[0])


def _build_image_stream(op,weight):
    im=Image.open(DATA/op["image"]).convert("RGBA")
    if op.get("ink_clip"):
        clip=op["ink_clip"]
        if not (0<=clip[0]<clip[2]<=im.width and 0<=clip[1]<clip[3]<=im.height):raise ValueError("真实字形已变化，请重新生成预览")
        im=im.crop(clip)
    target=op.get("stroke_target");padding=op.get('padding',0)
    if target or padding:
        rect=op["rect"];width=max(.1,rect[2]-rect[0]);height=max(.1,rect[3]-rect[1])
        # Use physical page points at up to 576 dpi to avoid coarse stroke steps.
        scale=min(8,math.sqrt(4_000_000/max(.01,width*height)))
        w,h=max(2,round(width*scale)),max(2,round(height*scale))
        inner_w=min(w,max(2,round((width-2*padding)*scale)))
        inner_h=min(h,max(2,round((height-2*padding)*scale)))
        alpha=cv2.resize(np.asarray(im)[:,:,3],(inner_w,inner_h),interpolation=cv2.INTER_CUBIC)
        left=(w-inner_w)//2;top=(h-inner_h)//2
        alpha=cv2.copyMakeBorder(alpha,top,h-inner_h-top,left,w-inner_w-left,cv2.BORDER_CONSTANT,value=0)
        original=alpha.copy()
        for threshold in ((100,128) if target else ()):
            mask=np.pad((alpha>threshold).astype(np.uint8),2)
            dist=cv2.distanceTransform(mask,cv2.DIST_L2,cv2.DIST_MASK_PRECISE)
            if not np.any(dist):break
            observed=float(np.percentile(dist[dist>0],70)*2/scale)
            radius=min(6,max(0,abs(target*weight-observed)*scale/2))
            dilate=target*weight>observed
            def morph(n):
                if n==0:return alpha
                kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*n+1,2*n+1))
                return cv2.dilate(alpha,kernel) if dilate else cv2.erode(alpha,kernel)
            edges=lambda a:np.concatenate((a[0,:],a[-1,:],a[:,0],a[:,-1]))>100
            # Try a smaller correction when the full one would damage a stroke.
            for factor in (1,.5,.25,.125):
                amount=radius*factor;low=int(amount);fraction=amount-low
                adjusted=np.clip(morph(low)*(1-fraction)+morph(low+1)*fraction,0,255).astype(np.uint8)
                if (dilate and not np.any(edges(adjusted)&~edges(original)) and _dilation_preserves_strokes(original,adjusted)) or (not dilate and _erosion_preserves_strokes(original,adjusted)):
                    alpha=adjusted;break
        rgba=np.zeros((h,w,4),dtype=np.uint8);rgba[:,:,3]=alpha;im=Image.fromarray(rgba)
    elif abs(weight-1)>.04:
        alpha=np.array(im)[:,:,3]
        adjusted=cv2.dilate(alpha,np.ones((2,2),np.uint8)) if weight>1 else cv2.erode(alpha,np.ones((2,2),np.uint8))
        edges=lambda a:np.concatenate((a[0,:],a[-1,:],a[:,0],a[:,-1]))>100
        if (weight>1 and not np.any(edges(adjusted)&~edges(alpha))) or (weight<1 and _erosion_preserves_strokes(alpha,adjusted)):
            im.putalpha(Image.fromarray(adjusted))
    stream=io.BytesIO();im.save(stream,format="PNG")
    return stream.getvalue()


@lru_cache(maxsize=512)
def _cached_image_stream(image,clip,width,height,target,weight,modified,padding=0):
    return _build_image_stream({"image":image,"ink_clip":clip,"rect":[0,0,width,height],"stroke_target":target,"padding":padding},weight)


def image_stream(op,weight=1):
    rect=op['rect'];path=DATA/op['image'];clip=tuple(op['ink_clip']) if op.get('ink_clip') else None
    return _cached_image_stream(op['image'],clip,round(rect[2]-rect[0],4),round(rect[3]-rect[1],4),
                                op.get('stroke_target'),weight,path.stat().st_mtime_ns,op.get('padding',0))


@lru_cache(maxsize=64)
def _cached_alpha(image,clip,width,height,target,weight,modified,padding=0):
    op={"image":image,"ink_clip":clip,"rect":[0,0,width,height],"stroke_target":target,"padding":padding}
    with Image.open(io.BytesIO(image_stream(op,weight))) as im:
        return np.array(im.convert("RGBA"))[:,:,3]


def _glyph_alpha(op,weight):
    r=op["rect"];path=DATA/op["image"]
    return _cached_alpha(op["image"],tuple(op["ink_clip"]),round(r[2]-r[0],4),round(r[3]-r[1],4),
                         op.get("stroke_target"),weight,path.stat().st_mtime_ns,op.get('padding',0))


def _visual_baseline_offset(op,size,weight):
    alpha=_glyph_alpha(op,weight);ys=np.where(alpha>100)[0]
    if len(ys)<4:return 0
    rect=op["rect"];position=rect[1]+float(np.quantile(ys,.95))/max(1,alpha.shape[0]-1)*(rect[3]-rect[1])
    return max(-size*.05,min(size*.05,-size*.10-position))


def _glyph_mask(op,weight,scale=4):
    alpha=_glyph_alpha(op,weight);r=op["rect"]
    x0,y0,x1,y1=[round(v*scale) for v in r]
    mask=cv2.resize(alpha,(max(1,x1-x0),max(1,y1-y0)),interpolation=cv2.INTER_AREA)>75
    return x0,y0,mask


def _optical_word_gap(left,right,gap,size,weight):
    if not left or len(left.ops)!=1 or len(right.ops)!=1:return gap
    a,b=left.ops[0],right.ops[0]
    if a.get("kind")!="glyph" or b.get("kind")!="glyph":return gap
    ax,ay,am=_glyph_mask(a,weight);bx,by,bm=_glyph_mask(b,weight)
    start=max(ay,by);end=min(ay+am.shape[0],by+bm.shape[0]);separations=[]
    for y in range(start,end):
        la=np.flatnonzero(am[y-ay]);lb=np.flatnonzero(bm[y-by])
        if len(la) and len(lb):separations.append(bx+lb[0]-ax-la[-1])
    if not separations:return gap
    minimum=min(separations)+(left.width+gap)*4
    adjustment=(minimum-max(1.5,gap*4))/4
    return max(gap-min(left.width,right.width)*.22,gap-adjustment)


def _glyphs_overlap(a,b,weight):
    ax,ay,am=_glyph_mask(a,weight);bx,by,bm=_glyph_mask(b,weight)
    x0=max(ax,bx);y0=max(ay,by);x1=min(ax+am.shape[1],bx+bm.shape[1]);y1=min(ay+am.shape[0],by+bm.shape[0])
    if x0>=x1 or y0>=y1:return False
    return bool(np.any(am[y0-ay:y1-ay,x0-ax:x1-ax] & bm[y0-by:y1-by,x0-bx:x1-bx]))


def _safe_math_gap(left,right,gap,size,weight):
    """Expand only touching adjacent glyphs; never reduce the chosen spacing."""
    if not left or len(left.ops)!=1 or len(right.ops)!=1:return gap
    a,b=left.ops[0],right.ops[0]
    if a.get('kind')!='glyph' or b.get('kind')!='glyph':return gap
    step=max(.125,size*.02);limit=gap+size*.5
    # Mask positions are quantized to quarter points; keep one pixel of
    # clearance so translating the word on the page cannot create a collision.
    clearance=.25
    while gap<limit and a['rect'][2]+clearance>left.width+gap+b['rect'][0]:
        placed=shift(right,left.width+gap-clearance)[0]
        if not _glyphs_overlap(a,placed,weight):break
        gap=min(limit,gap+step)
    return gap


def draw_ops(page,ops,weight=1,progress=None):
    if progress and not ops:progress(1)
    for i,op in enumerate(ops,1):
        if op["kind"] in ("glyph","stroke_image"):
            rect=fitz.Rect(op["rect"])
            page.insert_image(rect,stream=image_stream(op,weight),keep_proportion=False)
        elif op["kind"]=="fallback":
            r=op["rect"];label=op["label"] if op["label"].isascii() else "?"
            page.insert_text((r[0],r[3]-op["size"]*.18),label,fontsize=op["size"],color=(.5,.5,.5))
        elif op["kind"]=="line":
            page.draw_polyline([fitz.Point(x) for x in op["points"]],color=(0,0,0),width=op["width"]*weight,lineCap=1,lineJoin=1)
        elif op["kind"]=="curve":
            page.draw_bezier(*[fitz.Point(x) for x in op["points"]],color=(0,0,0),width=op["width"]*weight)
        if progress and (i%32==0 or i==len(ops)):progress(i/len(ops))


def validate_settings(settings):
    default={"font_size":16,"line_spacing":1.65,"letter_spacing":.7,"word_spacing":1,"ink_weight":1,"missing_policy":"strict","allow_inferred":True,"uniform_stroke":True,"auto_fit":True}
    default.update(settings or {})
    for k,lo,hi in [("font_size",8,40),("line_spacing",1.15,3),("letter_spacing",.6,1.8),("word_spacing",.5,2),("ink_weight",.8,1.2)]:
        try:v=float(default[k])
        except (TypeError,ValueError):raise ValueError(f"样式 {k} 无效")
        if not math.isfinite(v) or not lo<=v<=hi:raise ValueError(f"样式 {k} 应在 {lo}–{hi} 之间")
        default[k]=v
    if default["missing_policy"] not in ("strict","reference"):raise ValueError("缺字策略无效")
    if not all(isinstance(default[k],bool) for k in ("allow_inferred","uniform_stroke","auto_fit")):raise ValueError("推测字形或自动样式开关无效")
    return default


def answer_region_rect(region,page):
    rect=pt_box(region["bbox"],page.rect.width,page.rect.height)
    if region.get("source")!="ai" or region.get("user_edited"):return rect
    # Do not start an AI answer inside the final printed line of a question.
    bottoms=[b[3] for b in page.get_text("blocks") if b[6]==0 and b[0]<rect[2] and b[2]>rect[0] and b[1]<=rect[1]<b[3]]
    if bottoms:
        top=max(bottoms)+4
        if top<rect[3]-.4:rect[1]=top
    return rect


def fitted_font_size(profile_id,answers,doc,settings):
    """Find a single bounded size for untouched AI answers, without changing their text."""
    base=settings["font_size"]
    active=[r for r in answers if r["text"].strip()]
    if not settings["auto_fit"] or not active or any(r.get("source")!="ai" or r.get("user_edited") for r in active):return base
    floor=math.ceil(max(12,base*.8)*2)/2
    count=math.floor((base-floor)*2)
    if count<1:return base
    prepared=[]
    for r in active:
        try:parsed=parse_document(r["text"])
        except ValueError:return base
        page=int(r["page"])
        if not 0<=page<len(doc):return base
        prepared.append((r,parsed,answer_region_rect(r,doc[page])))
    def fits(size):
        trial={**settings,"font_size":size}
        for r,parsed,rect in prepared:
            composer=Composer(profile_id,r.get("seed",0),trial)
            _,issues=composer.layout(parsed,rect,size,compact_blank_lines=True)
            if any("超出" in issue for issue in issues):return False
        return True
    if fits(base):return base
    candidates=[base-.5*i for i in range(1,count+1)]
    if not fits(candidates[-1]):return base
    lo,hi=0,len(candidates)-1
    while lo<hi:
        middle=(lo+hi)//2
        if fits(candidates[middle]):hi=middle
        else:lo=middle+1
    return candidates[lo]


def render_assignment(assignment_id,preview_only=False,progress=None,expected_revision=None):
    progress=progress or (lambda value,message:None)
    progress(.01,"正在准备答案和页面")
    a=one("SELECT * FROM assignments WHERE id=?",(assignment_id,))
    if expected_revision is not None and a["revision"]!=expected_revision:
        raise ValueError("作业已修改，请重新生成预览")
    settings=validate_settings(json.loads(a["settings"]));answers=json.loads(a["answers"])
    if len(answers)>480:raise ValueError("最多 480 个作答区域")
    doc=None
    if a["document_id"]:
        source=one("SELECT * FROM documents WHERE id=? AND status='ready'",(a["document_id"],))
        original=DATA/source["original"]
        if original.suffix.lower()!=".pdf":original=DATA/"pages"/source["id"]/"original.pdf"
        doc=fitz.open(original)
    else:
        doc=fitz.open();doc.new_page(width=595.28,height=841.89)
    progress(.015,"正在检查作答空间和统一字高")
    actual_size=fitted_font_size(a["profile_id"],answers,doc,settings)
    layout_settings={**settings,"font_size":actual_size}
    report={"assignment_id":assignment_id,"revision":a["revision"],"parser_version":5,"format_version":1,"size_version":31,"settings":settings,"pages":len(doc),"errors":[],"warnings":[],
            "missing":[],"regions":[],"generated":True,"provenance":"真实字形优先；允许时使用 AI 推测或基于真实笔迹的衍生字形；结构线按尺寸绘制；所有来源见 placements.origin",
            "size_policy":"依据可用真实字形校准字高与基线；公式变量参考同行 x/数字比例；输出保留所有可见笔画",
            "stroke_policy":"依据真实样本中位笔宽进行两次有限校准，原样本不修改" if settings["uniform_stroke"] else "不统一笔宽；仍按笔画微调设置调整输出，原样本不修改"}
    report["effective_font_size"]=actual_size
    if actual_size<settings["font_size"]:report["warnings"].append(f"空间紧张，已统一将字高由 {settings['font_size']:g} 调整为 {actual_size:g} pt；请检查可读性。")
    all_ops={i:[] for i in range(len(doc))};region_ops=[]
    for region_number,region in enumerate(answers,1):
        progress(.02+.14*(region_number-1)/max(1,len(answers)),f"正在排版答案区域 {region_number} / {len(answers)}")
        page=int(region["page"])
        if not 0<=page<len(doc):raise ValueError("作答区域页码无效")
        source_rect=pt_box(region["bbox"],doc[page].rect.width,doc[page].rect.height)
        rect=answer_region_rect(region,doc[page])
        parse_error=None
        try:parsed=parse_document(region["text"])
        except ValueError as error:parsed=None;parse_error=str(error)
        if parsed is not None and parsed!=region.get("ast"):
            try:previous=parse_document(region["text"],legacy_text=True)
            except ValueError:previous=None
            try:legacy=parse_document_legacy(region["text"])
            except ValueError:legacy=None
            stored=region.get("ast")
            if not ((previous is not None and previous==stored) or (legacy is not None and legacy==stored) or (stored is None and region.get("parse_error"))):
                raise ValueError("答案结构与文字源不一致，请重新保存答案")
        composer=Composer(a["profile_id"],region.get("seed",0),layout_settings)
        ops,issues=composer.layout(parsed,rect,actual_size) if parsed is not None else ([],["公式需要修正："+parse_error])
        compacted=False
        if (parsed is not None and region.get("source")=="ai" and not region.get("user_edited")
            and any("答案高度超出区域" in issue for issue in issues)
            and any(not line["runs"] for line in parsed["lines"])):
            candidate=Composer(a["profile_id"],region.get("seed",0),layout_settings)
            compact_ops,compact_issues=candidate.layout(parsed,rect,actual_size,compact_blank_lines=True)
            if not any("答案高度超出区域" in issue for issue in compact_issues):
                composer,ops,issues,compacted=candidate,compact_ops,compact_issues,True
        if composer.word_sizes is not None:report['word_size_calibration']=composer.word_sizes
        if region.get("ai_error"):issues.append("本题答案未完成："+region["ai_error"])
        if region.get("placement_pending"):issues.append("AI 未找到可靠的书写位置，请调整此题作答区域")
        if region.get("question_changed"):issues.append("题目已修改，请重新解答或修改本题答案")
        if "[unclear]" in region.get("question","").lower():issues.append("题目有无法辨认的内容，请核对并修改本题")
        if not region.get("reviewed") and region["text"].strip():issues.append("此区域答案尚未勾选审阅确认")
        missing=sorted(composer.missing)
        if missing:issues.append("缺少可用字形："+" ".join(missing))
        if missing and settings["missing_policy"]=="reference":
            report["warnings"].append("灰色印刷参考字（不属于个人笔迹）："+" ".join(missing))
            unsupported=[label for label in missing if not label.isascii()]
            if unsupported:report["warnings"].append("这些缺字无法用印刷参考字显示，输出为问号："+" ".join(unsupported))
        if composer.inferred:report["warnings"].append("使用 AI 推测补字，非真实提取，请检查字形："+" ".join(sorted(composer.inferred)))
        if composer.derived:report["warnings"].append("使用真实笔迹衍生的推测字形，非对应字符的真实样本："+" ".join(sorted(composer.derived)))
        if composer.excluded_inferred:report["warnings"].append("已排除易混淆的 AI 推测字形："+" ".join(sorted(composer.excluded_inferred)))
        if composer.single_variants:report["warnings"].append("仅有一个字形版本，无法避免重复："+" ".join(sorted(composer.single_variants)))
        report["missing"]+=missing
        report["regions"].append({"id":region["id"],"page":page,"bbox":rect,"source_bbox":source_rect,"errors":issues,"missing":missing,"auto_compacted_blank_lines":compacted,
                                   "question_key":region.get("question_key"),"question":region.get("question"),"question_bbox":region.get("question_bbox"),
                                   "placements":ops,"seed":region.get("seed",0),"text":region["text"],"ast":parsed})
        report["errors"] += [f"第 {page+1} 页区域 {region_number}：{issue}" for issue in issues]
        all_ops[page]+=ops;region_ops.append((page,rect,ops))
    if not any(r["text"].strip() for r in answers):report["errors"].append("请先输入答案")
    used={op['glyph_id'] for ops in all_ops.values() for op in ops if op.get('glyph_id')}
    report['glyph_checks']=[]
    if used:
        placeholders=','.join('?' for _ in used)
        for row in rows(f"SELECT * FROM glyphs WHERE id IN ({placeholders})",tuple(used)):
            g=glyph_record(row)
            if not glyph_verified(g):report['glyph_checks'].append({'id':g['id'],'origin':g['origin'],'document_id':g['document_id'],'page':g['page']})
    if report['glyph_checks']:report['errors'].append(f"本次使用的 {len(report['glyph_checks'])} 个字形需要自动复核")
    # Render generated ink independently, then compare pixel masks with untouched original.
    # This catches ink collisions, not merely overlapping bounding rectangles.
    for page_number,page in enumerate(doc):
        page_start=.16+.72*page_number/len(doc);page_span=.72/len(doc)
        label=f"第 {page_number+1} / {len(doc)} 页"
        progress(page_start,f"正在检查{label}的笔迹")
        blank=fitz.open();p=blank.new_page(width=page.rect.width,height=page.rect.height)
        draw_ops(p,all_ops[page_number],settings["ink_weight"],
                 lambda part:progress(page_start+page_span*.4*part,f"正在检查{label}的笔迹"))
        inkpix=p.get_pixmap(matrix=fitz.Matrix(SCALE,SCALE),alpha=False)
        generated=np.frombuffer(inkpix.samples,np.uint8).reshape(inkpix.height,inkpix.width,3).min(axis=2)<180
        rawpix=page.get_pixmap(matrix=fitz.Matrix(SCALE,SCALE),alpha=False)
        old=np.frombuffer(rawpix.samples,np.uint8).reshape(rawpix.height,rawpix.width,3).min(axis=2)<190
        overlap=int((generated & old).sum())
        if overlap>6:
            report["errors"].append(f"第 {page_number+1} 页有 {overlap} 个像素压到原题/原笔迹，请移动作答区域")
        coords=np.argwhere(generated)
        if len(coords) and (coords[:,0].min()<6 or coords[:,1].min()<6 or coords[:,0].max()>generated.shape[0]-7 or coords[:,1].max()>generated.shape[1]-7):
            report["errors"].append(f"第 {page_number+1} 页笔迹接近或超出页边界")
        blank.close()
        # Cross-region overlap: preserve independently authored blocks and warn on touching ink.
        rectangles=[(r,op) for pg,r,op in region_ops if pg==page_number and op]
        for i,(r,op) in enumerate(rectangles):
            for s,other in rectangles[i+1:]:
                if fitz.Rect(r).intersects(fitz.Rect(s)):
                    report["errors"].append(f"第 {page_number+1} 页两个作答区域重叠，请调整")
        # Detect glyph collisions within each line, allowing compound structural math strokes.
        glyph_ops=sorted((x for x in all_ops[page_number] if x["kind"] in ("glyph","fallback")),key=lambda op:op["rect"][1])
        collision_count=0
        for i,g in enumerate(glyph_ops):
            if i%32==0:progress(page_start+page_span*(.4+.2*i/max(1,len(glyph_ops))),f"正在检查{label}的碰撞")
            gr=fitz.Rect(g["rect"])
            for h in glyph_ops[i+1:]:
                if h["rect"][1]>=gr.y1:break
                hr=fitz.Rect(h["rect"])
                intersection=gr & hr
                if intersection.is_empty:continue
                if (g['kind']==h['kind']=='glyph' and _glyphs_overlap(g,h,settings['ink_weight'])) or (('fallback' in (g['kind'],h['kind'])) and intersection.get_area()>min(gr.get_area(),hr.get_area())*.14):
                    collision_count+=1
        if collision_count:report["errors"].append(f"第 {page_number+1} 页检测到 {collision_count} 处字形边界碰撞，请检查公式或行距")
        progress(page_start+page_span*.6,f"正在绘制{label}的预览")
        draw_ops(page,all_ops[page_number],settings["ink_weight"],
                 lambda part:progress(page_start+page_span*(.6+.4*part),f"正在绘制{label}的预览"))
    report["errors"]=list(dict.fromkeys(report["errors"]))
    report["warnings"]=list(dict.fromkeys(report["warnings"]))
    report["missing"]=sorted(set(report["missing"]))
    output_id=uid();folder=DATA/"exports"/output_id;folder.mkdir(parents=True)
    doc.set_metadata({"title":a["title"],"subject":"HandwritingGenerated; local personal glyph composition", "creator":"Personal Handwriting Library"})
    for number,page in enumerate(doc):
        progress(.88+.1*number/len(doc),f"正在生成第 {number+1} / {len(doc)} 页预览图片")
        page.get_pixmap(matrix=fitz.Matrix(1.4,1.4)).save(folder/f"{number}.png")
    progress(.98,"正在保存预览结果")
    # A preview may contain placeholders/errors. A download can NEVER bypass preflight.
    if not report["errors"]:
        doc.save(folder/"result.pdf",garbage=4,deflate=True)
        report["pdf_url"]=f"/exports/{output_id}/result.pdf"
    report["id"]=output_id;report["preview_urls"]=[f"/exports/{output_id}/{i}.png" for i in range(len(doc))]
    report["audit_url"]=f"/exports/{output_id}/audit.json"
    (folder/"audit.json").write_text(dumps(report))
    doc.close()
    return report


def supplement_template(profile_id,characters=None):
    coverage=profile_coverage(profile_id)
    characters=list(dict.fromkeys(characters or coverage["missing"]))[:120]
    if not characters:raise ValueError("没有需要补写的字符")
    doc=fitz.open();cells=[]
    for start in range(0,len(characters),24):
        page=doc.new_page(width=595.28,height=841.89)
        page.insert_text((36,36),"PERSONAL HANDWRITING / REAL SAMPLE SHEET",fontsize=13)
        page.insert_text((36,56),"Write each labelled character 3 times. Scan, import, then review the proposed glyphs.",fontsize=9)
        for i,c in enumerate(characters[start:start+24]):
            row,col=divmod(i,4);x=36+col*132;y=88+row*115
            label=c if c.isascii() else f"U+{ord(c):04X}"
            page.insert_text((x,y),label,fontsize=16)
            for k in range(3):
                rect=[x+k*40,y+12,x+k*40+36,y+75]
                page.draw_rect(rect,color=(.65,.72,.78),width=.5)
                page.draw_line((rect[0],y+62),(rect[2],y+62),color=(.8,.82,.85),width=.3)
                cells.append({"page":start//24,"label":c,"bbox":rect})
        page.insert_text((36,807),f"Profile: {profile_id[:10]} | Do not use computer-generated writing as a sample.",fontsize=9)
    oid=uid();folder=DATA/"exports"/oid;folder.mkdir()
    doc.save(folder/"template.pdf");doc.close()
    (folder/"cells.json").write_text(dumps(cells))
    return {"pdf_url":f"/exports/{oid}/template.pdf","cells_url":f"/exports/{oid}/cells.json","characters":characters}
