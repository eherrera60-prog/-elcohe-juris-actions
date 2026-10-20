
from __future__ import annotations
import hashlib, io, re, unicodedata
from typing import Any
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, HttpUrl, Field
from pypdf import PdfReader

APP_VERSION = "1.5.1"
TC_SEARCH_URL = "https://www.tribunalconstitucional.gob.do/consultas/secretar%C3%ADa/sentencias/"
SCJ_PORTAL_URL = "https://consultasentenciascj.poderjudicial.gob.do/"
ALLOWED_HOST_SUFFIXES = (
    "tribunalconstitucional.gob.do",
    "poderjudicial.gob.do",
    "consultasentenciascj.poderjudicial.gob.do",
    "tribunalsitestorage.blob.core.windows.net",
)

app = FastAPI(
    title="ELCOHE-JURIS RD Jurisprudence Actions",
    version=APP_VERSION,
    description="Verificación, lectura y análisis jurisprudencial dominicano."
)

class VerifyRequest(BaseModel):
    url: HttpUrl
    expected_court: str | None = None
    expected_number: str | None = None

class AnalyzeDecisionRequest(BaseModel):
    url: HttpUrl
    expected_court: str | None = None
    expected_number: str | None = None
    legal_issue: str | None = None
    max_passages: int = Field(default=6, ge=1, le=12)

class HoldingPromotionRequest(BaseModel):
    url: HttpUrl
    expected_court: str
    expected_number: str | None = None
    legal_issue: str
    legal_proposition: str = Field(min_length=20, max_length=2000)
    support_quote: str = Field(min_length=40, max_length=1800)
    support_page: int = Field(ge=1)
    outcome_quote: str | None = Field(default=None, max_length=1800)
    outcome_page: int | None = Field(default=None, ge=1)
    judicial_reasoning_reviewed: bool = False

def normalize_space(v: str | None) -> str:
    return re.sub(r"\s+", " ", v or "").strip()

def fold(v: str | None) -> str:
    v = normalize_space(v).lower()
    return "".join(ch for ch in unicodedata.normalize("NFD", v) if unicodedata.category(ch) != "Mn")

def allowed_host(host: str | None) -> bool:
    if not host: return False
    host = host.lower().strip(".")
    return any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES)

def tokenize(v: str | None) -> set[str]:
    stop = {"de","la","el","los","las","un","una","y","o","a","en","por","para","con","del","que","se","su","sus","al","como","es","son","ha","han","ser","fue","esta","este","sobre","entre","cuando","sin","si","no","lo","le","les","más","mas"}
    return {t for t in re.findall(r"[a-záéíóúñü0-9]{3,}", (v or "").lower()) if t not in stop}

def overlap_ratio(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    return len(ta & tb) / max(len(ta), 1) if ta else 0.0

def detect_court(host: str | None, expected: str | None = None):
    host = (host or "").lower()
    if "tribunalconstitucional" in host or "tribunalsitestorage" in host: return "TC"
    if "poderjudicial" in host: return "SCJ"
    return None

def court_matches(detected: str | None, expected: str | None) -> bool:
    if not expected:
        return detected in ("SCJ", "TC")
    return detected == expected.strip().upper()

def clean_page_text(text: str) -> str:
    # Remove repeated institutional headers/footers without deleting legal reasoning.
    text = re.sub(r"REP[ÚU]BLICA DOMINICANA\s+SUPREMA CORTE DE JUSTICIA\s+SCJ-[A-Z]{2}-\d{2}-\d{3,5}", " ", text, flags=re.I)
    text = re.sub(r"Exp\.\s*n[úu]m\.\s*[A-Za-z0-9./-]+\s+Partes:.*?Decisi[oó]n:\s*[A-ZÁÉÍÓÚ]+", " ", text, flags=re.I)
    text = re.sub(r"Av\. Enrique Jim[eé]nez Moya.*?(?:P[aá]g\.\s*\d+)", " ", text, flags=re.I)
    return normalize_space(text)

def context_for_page(pages: list[dict], page_number: int, window: int = 1) -> dict:
    """Return neighboring pages as context; not proof of authorship."""
    return {"page": page_number, "context_pages": [p["page"] for p in pages if abs(p["page"] - page_number) <= window]}

def extract_metadata_from_text(text: str, court: str | None) -> dict[str, Any]:
    sample = text[:500000]
    meta = {"decision_number": None, "date": None, "case_number": None, "chamber": None}
    for pat in [
        r"\bTC/\d{3,4}/\d{2}\b",
        r"\bSCJ-[A-Z]{2}-\d{2}-\d{3,5}\b",
        r"\bSentencia\s+(?:núm\.?|No\.?)\s*[A-Za-z0-9./-]+",
    ]:
        m = re.search(pat, sample, re.I)
        if m: meta["decision_number"] = normalize_space(m.group(0)); break
    for pat in [
        r"\b\d{1,2}\s+de\s+(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\s+de\s+\d{4}\b",
        r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b",
    ]:
        m = re.search(pat, sample, re.I)
        if m: meta["date"] = normalize_space(m.group(0)); break
    for pat in [
        r"\b(?:expediente|exp\.)\s*(?:n[úu]m\.?|no\.?|n[°º]\.?)\s*[A-Za-z0-9./-]+",
        r"\bTC-\d{2}-\d{4}-\d{4}\b",
    ]:
        m = re.search(pat, sample, re.I)
        if m:
            raw = normalize_space(m.group(0))
            meta["case_number"] = re.sub(r"^(?:expediente|exp\.)\s*(?:n[úu]m\.?|no\.?|n[°º]\.?)\s*", "", raw, flags=re.I)
            break
    for pat in [r"\bPrimera Sala\b", r"\bSegunda Sala\b", r"\bTercera Sala\b", r"\bSalas Reunidas\b", r"\bPleno\b"]:
        m = re.search(pat, sample, re.I)
        if m: meta["chamber"] = m.group(0); break
    if court == "TC": meta["chamber"] = "Tribunal Constitucional"
    return meta

def split_units(text: str):
    text = re.sub(r"\r\n?", "\n", text)
    parts = re.split(r"\n{2,}|(?<=[.;:])\s+(?=(?:Que|Considerando|Por cuanto|En ese sentido|Esta Sala|Este Tribunal)\b)", text, flags=re.I)
    return [normalize_space(p) for p in parts if len(normalize_space(p)) >= 80]

def score_passage(passage: str, issue: str | None):
    score = 0.0
    if issue:
        it, pt = tokenize(issue), tokenize(passage)
        if it: score += 5.0 * len(it & pt) / len(it)
    pf = fold(passage)
    for marker in ["esta sala","este tribunal","tribunal constitucional","considerando","por cuanto","en ese sentido","ha establecido","criterio","precedente","en consecuencia","procede","responsabilidad","vulneracion","violacion"]:
        if marker in pf: score += 0.4
    if len(passage) < 140: score -= 0.4
    return score

async def fetch_document(url: str):
    p = urlparse(url)
    if p.scheme != "https" or not allowed_host(p.hostname):
        raise HTTPException(400, "La URL debe ser HTTPS y pertenecer a un dominio oficial permitido.")
    try:
        async with httpx.AsyncClient(timeout=35, follow_redirects=True, headers={"User-Agent":"ELCOHE-JURIS-RD/1.5"}) as c:
            r = await c.get(url); r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"No fue posible obtener el documento oficial: {e}")
    final_url = str(r.url); host = urlparse(final_url).hostname
    if not allowed_host(host): raise HTTPException(400, "La redirección terminó fuera de dominios oficiales permitidos.")
    return {"final_url": final_url, "host": host, "content_type": (r.headers.get("content-type") or "").split(";")[0].lower(), "data": r.content, "text_response": r.text if "html" in (r.headers.get("content-type") or "") else None, "http_status": r.status_code, "sha256": hashlib.sha256(r.content).hexdigest()}

def extract_document_text(doc):
    ct, data = doc["content_type"], doc["data"]
    if "pdf" in ct or data[:4] == b"%PDF":
        reader = PdfReader(io.BytesIO(data))
        pages=[]
        for i,p in enumerate(reader.pages,1):
            try: txt = normalize_space(p.extract_text() or "")
            except Exception: txt = ""
            pages.append({"page":i,"text":txt})
        full = "\n\n".join(p["text"] for p in pages if p["text"])
        return {"document_type":"PDF","pages":pages,"full_text":full,"page_count":len(pages),"extraction_ok":bool(full.strip())}
    if "html" in ct:
        soup = BeautifulSoup(doc["text_response"] or "", "html.parser")
        text = normalize_space(soup.get_text("\n", strip=True))
        return {"document_type":"HTML","pages":[{"page":1,"text":text}],"full_text":text,"page_count":1,"extraction_ok":bool(text.strip())}
    raise HTTPException(415, f"Tipo no compatible: {ct or 'desconocido'}")

def find_relevant_passages(pages, issue, limit):
    cands=[]
    for po in pages:
        for unit in split_units(clean_page_text(po["text"])):
            s = score_passage(unit, issue)
            if s>0: cands.append({"page":po["page"],"score":round(s,3),"text":unit[:1800],"attribution":"UNDETERMINED_REQUIRES_CONTEXT"})
    cands.sort(key=lambda x:(-x["score"],x["page"]))
    out=[]; seen=set()
    for c in cands:
        k=fold(c["text"][:300])
        if k in seen: continue
        seen.add(k); out.append(c)
        if len(out)>=limit: break
    return out

def normalized_contains(haystack, needle): return fold(needle) in fold(haystack)

@app.get("/health", operation_id="healthCheck")
def health():
    return {"ok":True,"service":"ELCOHE-JURIS RD Jurisprudence Actions","version":APP_VERSION}

@app.get("/v1/tc/search", operation_id="searchTcDecisions")
async def search_tc(q:str=Query(min_length=2), year:int|None=None, limit:int=Query(default=10,ge=1,le=20)):
    params={"searchString":q,"searchCriteria":"","size":max(limit,10)}
    if year: params["searchString"]=f"{q} {year}"
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers={"User-Agent":"ELCOHE-JURIS-RD/1.5"}) as c:
            r=await c.get(TC_SEARCH_URL,params=params); r.raise_for_status()
    except httpx.HTTPError as e:
        return {"status":"ERROR","message":f"No fue posible consultar el TC: {e}"}
    soup=BeautifulSoup(r.text,"html.parser"); results=[]; seen=set(); pat=re.compile(r"TC/\d{3,4}/\d{2}",re.I)
    for a in soup.find_all("a",href=True):
        m=pat.search(normalize_space(a.get_text(" ",strip=True)))
        if not m: continue
        href=a["href"]
        if href.startswith("/"): href="https://www.tribunalconstitucional.gob.do"+href
        if href in seen: continue
        seen.add(href); results.append({"court":"TC","decision_number":m.group(0).upper(),"source_url":href,"status":"LOCATED"})
        if len(results)>=limit: break
    return {"query":q,"source":str(r.url),"results":results,"status":"LOCATED_RESULTS" if results else "NO_RESULTS"}

@app.get("/v1/scj/search-plan", operation_id="prepareScjSearch")
def prepare_scj_search(q:str=Query(min_length=2), chamber:str|None=None, matter:str|None=None, year_from:int|None=None, year_to:int|None=None):
    if year_from and year_to and year_from>year_to: raise HTTPException(400,"year_from no puede ser mayor que year_to.")
    site_q=f'site:poderjudicial.gob.do "{q}"'
    if chamber: site_q+=f' "{chamber}"'
    if matter: site_q+=f' "{matter}"'
    return {"query":q,"official_portal":SCJ_PORTAL_URL,"filters":{k:v for k,v in {"sala":chamber,"materia":matter,"desde":year_from,"hasta":year_to}.items() if v is not None},"web_queries":[site_q,f'site:poderjudicial.gob.do/wp-content/uploads "{q}" sentencia'],"status":"SEARCH_PLAN_ONLY"}

@app.post("/v1/verify", operation_id="verifyOfficialDocument")
async def verify_document(req:VerifyRequest):
    doc=await fetch_document(str(req.url)); ex=extract_document_text(doc); court=detect_court(doc["host"],req.expected_court); meta=extract_metadata_from_text(ex["full_text"],court)
    expected_match = normalized_contains(ex["full_text"], req.expected_number) if req.expected_number else None
    court_ok = court_matches(court, req.expected_court)
    status="VERIFIED_SOURCE" if ex["extraction_ok"] and court_ok else "NOT_VERIFIED"
    if expected_match is False: status="NOT_VERIFIED"
    return {"status":status,"official_domain":True,"court":court,"expected_court_match":court_ok,"final_url":doc["final_url"],"document_type":ex["document_type"],"page_count":ex["page_count"],"metadata":meta,"expected_number_match":expected_match,"sha256":doc["sha256"]}

@app.post("/v1/analyze-decision", operation_id="analyzeOfficialDecision")
async def analyze_official_decision(req:AnalyzeDecisionRequest):
    doc=await fetch_document(str(req.url)); ex=extract_document_text(doc); court=detect_court(doc["host"],req.expected_court); meta=extract_metadata_from_text(ex["full_text"],court)
    expected_match=normalized_contains(ex["full_text"],req.expected_number) if req.expected_number else None
    court_ok=court_matches(court,req.expected_court)
    passages=find_relevant_passages(ex["pages"],req.legal_issue,req.max_passages)
    for passage in passages:
        passage.update(context_for_page(ex["pages"],passage["page"]))
    status="HOLDING_CANDIDATE" if ex["extraction_ok"] and court_ok and (expected_match is not False) and passages else ("VERIFIED_SOURCE" if ex["extraction_ok"] and court_ok and expected_match is not False else "NOT_VERIFIED")
    return {"status":status,"official_source_verified":bool(ex["extraction_ok"] and court_ok and expected_match is not False),"expected_court_match":court_ok,"expected_number_match":expected_match,"metadata":meta,"relevant_passages":passages,"jurisprudential_card":{"tribunal":court,"sala":meta.get("chamber"),"sentencia":meta.get("decision_number"),"fecha":meta.get("date"),"expediente":meta.get("case_number"),"estado_verificacion":status,"cuestion_juridica":req.legal_issue,"fundamentos_candidatos":passages,"ratio_decidendi":None,"regla_juridica":None,"fuente_oficial":doc["final_url"],"paginas":ex["page_count"]},"source_url":doc["final_url"],"sha256":doc["sha256"]}

@app.post("/v1/promote-holding", operation_id="promoteVerifiedHolding")
async def promote_verified_holding(req:HoldingPromotionRequest):
    doc=await fetch_document(str(req.url)); ex=extract_document_text(doc); court=detect_court(doc["host"],req.expected_court); meta=extract_metadata_from_text(ex["full_text"],court)
    reasons=[]; gates={}
    official=allowed_host(doc["host"]) and ex["extraction_ok"]; gates["official_source"]=official
    court_ok=court_matches(court,req.expected_court); gates["expected_court_match"]=court_ok
    number_ok = normalized_contains(ex["full_text"],req.expected_number) if req.expected_number else True
    gates["expected_number_match"]=number_ok
    page_ok = 1<=req.support_page<=ex["page_count"]; gates["support_page_exists"]=page_ok
    support_exact=False; issue_score=0.0; prop_score=0.0
    if page_ok:
        page_text=ex["pages"][req.support_page-1]["text"]
        support_exact=normalized_contains(page_text,req.support_quote)
        issue_score=overlap_ratio(req.legal_issue,req.support_quote)
        prop_score=overlap_ratio(req.legal_proposition,req.support_quote)
    gates["support_quote_exact_on_page"]=support_exact
    gates["issue_grounding_score"]=round(issue_score,3)
    gates["proposition_grounding_score"]=round(prop_score,3)
    outcome_ok=bool(req.outcome_quote and req.outcome_page)
    if req.outcome_quote:
        if not req.outcome_page or not (1<=req.outcome_page<=ex["page_count"]): outcome_ok=False
        else: outcome_ok=normalized_contains(ex["pages"][req.outcome_page-1]["text"],req.outcome_quote)
    gates["outcome_quote_verified"]=outcome_ok
    gates["judicial_reasoning_reviewed"]=req.judicial_reasoning_reviewed
    if not court_ok: reasons.append("Tribunal esperado no coincide con el origen del documento.")
    if not req.judicial_reasoning_reviewed: reasons.append("Falta revisión humana de autoría y contexto del fundamento.")
    if not number_ok: reasons.append("Número esperado no confirmado.")
    if not support_exact: reasons.append("Pasaje no encontrado íntegramente en la página indicada.")
    if issue_score<0.15: reasons.append("Conexión insuficiente con la cuestión jurídica.")
    if prop_score<0.18: reasons.append("Anclaje insuficiente de la proposición en el pasaje.")
    if not outcome_ok: reasons.append("Resultado/dispositivo no verificado.")
    ok=official and court_ok and req.judicial_reasoning_reviewed and number_ok and page_ok and support_exact and issue_score>=0.15 and prop_score>=0.18 and outcome_ok
    return {"status":"VERIFIED_HOLDING" if ok else "HOLDING_NOT_PROMOTED","court":court,"metadata":meta,"legal_issue":req.legal_issue,"legal_proposition":req.legal_proposition if ok else None,"support":{"page":req.support_page,"quote":req.support_quote if support_exact else None,"outcome_page":req.outcome_page,"outcome_quote":req.outcome_quote if outcome_ok else None},"gates":gates,"reasons":reasons,"verification_note":"La confirmación automatizada de citas no sustituye la lectura jurídica humana del razonamiento y dispositivo.","source_url":doc["final_url"],"sha256":doc["sha256"]}
