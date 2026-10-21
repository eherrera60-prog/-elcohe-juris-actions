
from __future__ import annotations
import hashlib, io, re, unicodedata, ipaddress, socket
from typing import Any
from urllib.parse import urlparse, urljoin
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, HttpUrl, Field
from pypdf import PdfReader

APP_VERSION = "1.5.3"
MAX_DOCUMENT_BYTES = 12 * 1024 * 1024
MAX_PDF_PAGES = 350
MAX_REDIRECTS = 3
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
    reviewer_name: str | None = Field(default=None, min_length=3, max_length=120)
    reviewer_confirmation: str | None = Field(default=None, max_length=120)

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

def detect_court(host: str | None, expected: str | None = None, text: str | None = None):
    """Identify court from the document's own opening, not solely its hosting domain."""
    opening = fold((text or "")[:4500])
    tc = bool(re.search(r"\btribunal constitucional\b|\bTC/\d{3,4}/\d{2}\b", opening, re.I))
    scj = bool(re.search(r"\bsuprema corte de justicia\b|\bSCJ-[A-Z]{2}-\d{2}-\d{3,5}\b", opening, re.I))
    if tc and not scj: return "TC"
    if scj and not tc: return "SCJ"
    if tc and scj: return None  # Conflicting evidence requires manual inspection.
    return None


def primary_decision_number(text: str, court: str | None):
    """Find the decision number in the opening; do not search cited precedents."""
    opening = (text or "")[:4500]
    pattern = (r"\bSCJ-[A-Z]{2}-\d{2}-\d{3,5}\b" if court == "SCJ"
               else r"\bTC/\d{3,4}/\d{2}\b" if court == "TC" else None)
    if not pattern: return None
    match = re.search(pattern, opening, re.I)
    return match.group(0).upper() if match else None


def decision_number_matches(text: str, court: str | None, expected: str | None):
    identified = primary_decision_number(text, court)
    if not expected: return identified is not None
    return identified is not None and fold(identified) == fold(expected)

def court_matches(detected: str | None, expected: str | None) -> bool:
    if not expected:
        return detected in ("SCJ", "TC")
    return detected == expected.strip().upper()

def clean_page_text(text: str) -> str:
    """Preserve paragraph boundaries; remove only identifiable short running headers."""
    text = re.sub(r"REP[ÚU]BLICA DOMINICANA\s+SUPREMA CORTE DE JUSTICIA\s+SCJ-[A-Z]{2}-\d{2}-\d{3,5}", " ", text, flags=re.I)
    text = re.sub(r"Av\. Enrique Jim[eé]nez Moya[^\n]{0,180}P[aá]g\.\s*\d+", " ", text, flags=re.I)
    return re.sub(r"[ \t]+", " ", text).strip()

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
        r"\b(?:expediente|exp\.)\s*(?:n[úu]m\.?|no\.?|n[°º]\.?)\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9./-]{5,})",
        r"\b(TC-\d{2}-\d{4}-\d{4})\b",
    ]:
        m = re.search(pat, sample, re.I)
        if m:
            meta["case_number"] = m.group(1).rstrip(".,;")
            break
    for pat in [r"\bPrimera Sala\b", r"\bSegunda Sala\b", r"\bTercera Sala\b", r"\bSalas Reunidas\b", r"\bPleno\b"]:
        m = re.search(pat, sample, re.I)
        if m: meta["chamber"] = m.group(0); break
    if court == "TC": meta["chamber"] = "Tribunal Constitucional"
    primary = primary_decision_number(text, court)
    if primary: meta["decision_number"] = primary
    return meta

def split_units(text: str):
    """Segment by numbered paragraphs and judicial transitions, not mere keywords."""
    text = re.sub(r"\r\n?", "\n", text)
    parts = re.split(
        r"\n{2,}|\n(?=\s*\d{1,3}\.\s)|(?<=[.;:])\s+(?=(?:Que|Considerando|Por cuanto|En ese sentido|Esta Sala|Este Tribunal)\b)",
        text, flags=re.I,
    )
    return [normalize_space(p) for p in parts if len(normalize_space(p)) >= 80]


def classify_passage(passage: str) -> tuple[str, list[str]]:
    """Heuristic triage only. A marker is never proof of judicial authorship."""
    p = fold(passage)
    party_markers = (
        "la parte recurrente", "la parte recurrida", "la recurrente sostiene",
        "la recurrente alega", "la parte demandante", "la parte demandada",
        "alega que", "argumenta que", "sostiene que", "aduce que",
        "en su memorial", "en su escrito de defensa", "como medio de casacion",
        "el recurrente sostiene", "el recurrente alega", "la apelante sostiene",
    )
    court_markers = (
        "esta sala considera", "esta sala advierte", "esta sala estima",
        "esta corte considera", "este tribunal considera", "esta jurisdiccion entiende",
        "esta primera sala", "esta segunda sala", "esta tercera sala",
        "esta sala ha juzgado", "esta sala ha establecido",
        "por tales motivos", "por estas razones", "procede rechazar",
        "procede casar", "se impone rechazar", "se impone casar",
    )
    found_party = [x for x in party_markers if x in p]
    found_court = [x for x in court_markers if x in p]
    if found_party:
        return "PARTY_ARGUMENT_OR_MIXED_REVIEW", found_party[:4]
    if found_court:
        return "POSSIBLE_JUDICIAL_REASONING_REVIEW", found_court[:4]
    return "UNDETERMINED_REQUIRES_CONTEXT", []


def score_passage(passage: str, issue: str | None):
    score = 0.0
    if issue:
        it, pt = tokenize(issue), tokenize(passage)
        if it: score += 5.0 * len(it & pt) / len(it)
    pf = fold(passage)
    for marker in ["esta sala", "este tribunal", "considerando", "por cuanto", "en ese sentido", "ha establecido", "criterio", "precedente", "en consecuencia", "procede", "responsabilidad", "vulneracion", "violacion"]:
        if marker in pf: score += 0.4
    attribution, _ = classify_passage(passage)
    if attribution == "PARTY_ARGUMENT_OR_MIXED_REVIEW": score -= 2.0
    elif attribution == "POSSIBLE_JUDICIAL_REASONING_REVIEW": score += 1.2
    if len(passage) < 140: score -= 0.4
    return score

def validate_official_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not allowed_host(parsed.hostname) or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise HTTPException(400, "Se requiere HTTPS, puerto estándar y dominio oficial permitido.")
    # Defense in depth: reject local/private/reserved address resolutions.
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(502, "No se pudo resolver el dominio oficial.")
    for result in addresses:
        address = ipaddress.ip_address(result[4][0])
        if not address.is_global:
            raise HTTPException(400, "La dirección de red no es pública.")


async def fetch_document(url: str):
    current = url
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=10), follow_redirects=False,
                                     headers={"User-Agent": "ELCOHE-JURIS-RD/1.5.3"}, trust_env=False) as client:
            for hop in range(MAX_REDIRECTS + 1):
                validate_official_url(current)
                async with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if hop == MAX_REDIRECTS: raise HTTPException(502, "Demasiadas redirecciones.")
                        location = response.headers.get("location")
                        if not location: raise HTTPException(502, "Redirección sin destino.")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    claimed = response.headers.get("content-length")
                    if claimed and claimed.isdigit() and int(claimed) > MAX_DOCUMENT_BYTES:
                        raise HTTPException(413, "El documento supera el límite de 12 MB.")
                    chunks = []; size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_DOCUMENT_BYTES:
                            raise HTTPException(413, "El documento supera el límite de 12 MB.")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    ct = (response.headers.get("content-type") or "").split(";")[0].lower()
                    return {"final_url": current, "host": urlparse(current).hostname,
                            "content_type": ct, "data": data,
                            "text_response": data.decode(response.encoding or "utf-8", errors="replace") if "html" in ct else None,
                            "http_status": response.status_code,
                            "sha256": hashlib.sha256(data).hexdigest()}
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise HTTPException(502, f"No fue posible obtener el documento oficial: {type(exc).__name__}")
    raise HTTPException(502, "No se obtuvo el documento.")

def extract_document_text(doc):
    ct, data = doc["content_type"], doc["data"]
    if "pdf" in ct or data[:4] == b"%PDF":
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted: raise HTTPException(422, "PDF cifrado: requiere revisión manual.")
            if len(reader.pages) > MAX_PDF_PAGES: raise HTTPException(413, "PDF demasiado extenso.")
        except HTTPException: raise
        except Exception: raise HTTPException(422, "PDF inválido o ilegible.")
        pages=[]
        for i,p in enumerate(reader.pages,1):
            try: txt = (p.extract_text() or "").replace("\r\n", "\n")
            except Exception: txt = ""
            pages.append({"page":i,"text":txt})
        full = "\n\n".join(p["text"] for p in pages if p["text"])
        return {"document_type":"PDF","pages":pages,"full_text":full,"page_count":len(pages),"extraction_ok":len(full.strip()) >= 100}
    if "html" in ct:
        soup = BeautifulSoup(doc["text_response"] or "", "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]): tag.decompose()
        text = normalize_space(soup.get_text("\n", strip=True))
        return {"document_type":"HTML","pages":[{"page":1,"text":text}],"full_text":text,"page_count":1,"extraction_ok":bool(text.strip())}
    raise HTTPException(415, f"Tipo no compatible: {ct or 'desconocido'}")

def find_relevant_passages(pages, issue, limit):
    cands=[]
    for po in pages:
        for unit in split_units(clean_page_text(po["text"])):
            s = score_passage(unit, issue)
            if s <= 0: continue
            attribution, markers = classify_passage(unit)
            cands.append({
                "page": po["page"], "score": round(s, 3),
                "text": unit[:1800], "attribution": attribution,
                "attribution_indicators": markers,
                "review_required": True,
            })
    cands.sort(key=lambda x: (-x["score"], x["page"]))
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
    doc=await fetch_document(str(req.url)); ex=extract_document_text(doc); court=detect_court(doc["host"],req.expected_court,ex["full_text"]); meta=extract_metadata_from_text(ex["full_text"],court)
    expected_match = decision_number_matches(ex["full_text"],court,req.expected_number)
    court_ok = court_matches(court, req.expected_court)
    status="VERIFIED_SOURCE" if ex["extraction_ok"] and court_ok else "NOT_VERIFIED"
    if not expected_match: status="NOT_VERIFIED"
    return {"status":status,"official_domain":True,"court":court,"expected_court_match":court_ok,"final_url":doc["final_url"],"document_type":ex["document_type"],"page_count":ex["page_count"],"metadata":meta,"expected_number_match":expected_match,"sha256":doc["sha256"]}

@app.post("/v1/analyze-decision", operation_id="analyzeOfficialDecision")
async def analyze_official_decision(req:AnalyzeDecisionRequest):
    doc=await fetch_document(str(req.url)); ex=extract_document_text(doc); court=detect_court(doc["host"],req.expected_court,ex["full_text"]); meta=extract_metadata_from_text(ex["full_text"],court)
    expected_match=decision_number_matches(ex["full_text"],court,req.expected_number)
    court_ok=court_matches(court,req.expected_court)
    passages=find_relevant_passages(ex["pages"],req.legal_issue,req.max_passages)
    for passage in passages:
        passage.update(context_for_page(ex["pages"],passage["page"]))
    status="HOLDING_CANDIDATE" if ex["extraction_ok"] and court_ok and expected_match and passages else ("VERIFIED_SOURCE" if ex["extraction_ok"] and court_ok and expected_match else "NOT_VERIFIED")
    return {"status":status,"official_source_verified":bool(ex["extraction_ok"] and court_ok and expected_match),"expected_court_match":court_ok,"expected_number_match":expected_match,"metadata":meta,"relevant_passages":passages,"jurisprudential_card":{"tribunal":court,"sala":meta.get("chamber"),"sentencia":meta.get("decision_number"),"fecha":meta.get("date"),"expediente":meta.get("case_number"),"estado_verificacion":status,"cuestion_juridica":req.legal_issue,"fundamentos_candidatos":passages,"ratio_decidendi":None,"regla_juridica":None,"review_required":True,"fuente_oficial":doc["final_url"],"paginas":ex["page_count"]},"source_url":doc["final_url"],"sha256":doc["sha256"]}

@app.post("/v1/promote-holding", operation_id="promoteVerifiedHolding")
async def promote_verified_holding(req:HoldingPromotionRequest):
    doc=await fetch_document(str(req.url)); ex=extract_document_text(doc); court=detect_court(doc["host"],req.expected_court,ex["full_text"]); meta=extract_metadata_from_text(ex["full_text"],court)
    reasons=[]; gates={}
    official=allowed_host(doc["host"]) and ex["extraction_ok"]; gates["official_source"]=official
    court_ok=court_matches(court,req.expected_court); gates["expected_court_match"]=court_ok
    number_ok = decision_number_matches(ex["full_text"],court,req.expected_number)
    gates["expected_number_match"]=number_ok
    page_ok = 1<=req.support_page<=ex["page_count"]; gates["support_page_exists"]=page_ok
    support_exact=False; issue_score=0.0; prop_score=0.0
    if page_ok:
        page_text=ex["pages"][req.support_page-1]["text"]
        support_exact=normalized_contains(page_text,req.support_quote)
        issue_score=overlap_ratio(req.legal_issue,req.support_quote)
        prop_score=overlap_ratio(req.legal_proposition,req.support_quote)
    attribution, attribution_indicators = classify_passage(req.support_quote)
    gates["support_attribution_heuristic"] = attribution
    gates["support_attribution_indicators"] = attribution_indicators
    gates["support_quote_exact_on_page"]=support_exact
    gates["issue_grounding_score"]=round(issue_score,3)
    gates["proposition_grounding_score"]=round(prop_score,3)
    outcome_ok=bool(req.outcome_quote and req.outcome_page)
    if req.outcome_quote:
        if not req.outcome_page or not (1<=req.outcome_page<=ex["page_count"]): outcome_ok=False
        else: outcome_ok=normalized_contains(ex["pages"][req.outcome_page-1]["text"],req.outcome_quote)
    gates["outcome_quote_verified"]=outcome_ok
    gates["judicial_reasoning_reviewed"]=req.judicial_reasoning_reviewed
    if not court_ok: reasons.append("El tribunal no pudo confirmarse en el contenido del documento.")
    if not req.judicial_reasoning_reviewed: reasons.append("Falta revisión humana de autoría y contexto del fundamento.")
    if not number_ok: reasons.append("Número esperado no confirmado.")
    if not support_exact: reasons.append("Pasaje no encontrado íntegramente en la página indicada.")
    if attribution != "POSSIBLE_JUDICIAL_REASONING_REVIEW": reasons.append("No se acreditó atribución judicial positiva mediante los indicios disponibles; revisión de contexto necesaria.")
    reviewer_ok = bool(req.reviewer_name and req.reviewer_confirmation == "CONFIRMO_LECTURA_INTEGRAL")
    gates["reviewer_attestation"] = reviewer_ok
    if not reviewer_ok: reasons.append("Falta identificación y declaración expresa de revisión integral del revisor.")
    if issue_score<0.15: reasons.append("Conexión insuficiente con la cuestión jurídica.")
    if prop_score<0.18: reasons.append("Anclaje insuficiente de la proposición en el pasaje.")
    if not outcome_ok: reasons.append("Resultado/dispositivo no verificado.")
    ok=official and court_ok and req.judicial_reasoning_reviewed and number_ok and page_ok and support_exact and attribution == "POSSIBLE_JUDICIAL_REASONING_REVIEW" and reviewer_ok and issue_score>=0.15 and prop_score>=0.18 and outcome_ok
    if not official: reasons.append("Documento oficial no confirmado o extracción insuficiente.")
    return {"status":"VERIFIED_HOLDING" if ok else "HOLDING_NOT_PROMOTED","court":court,"metadata":meta,"legal_issue":req.legal_issue,"legal_proposition":req.legal_proposition if ok else None,"support":{"page":req.support_page,"quote":req.support_quote if support_exact else None,"outcome_page":req.outcome_page,"outcome_quote":req.outcome_quote if outcome_ok else None},"gates":gates,"reasons":reasons,"verification_note":"La confirmación automatizada de citas no sustituye la lectura jurídica humana del razonamiento y dispositivo.","source_url":doc["final_url"],"sha256":doc["sha256"]}
