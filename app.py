
from __future__ import annotations
import asyncio, hashlib, io, re, unicodedata, ipaddress, socket
from typing import Any
from urllib.parse import urlparse, urljoin
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, HttpUrl, Field
from pypdf import PdfReader

APP_VERSION = "1.6.4"
MAX_DOCUMENT_BYTES = 12 * 1024 * 1024
MAX_PDF_PAGES = 350
MAX_REDIRECTS = 3
MAX_OFFICIAL_REQUEST_ATTEMPTS = 3
RETRYABLE_OFFICIAL_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
TC_SEARCH_URL = "https://www.tribunalconstitucional.gob.do/consultas/secretar%C3%ADa/sentencias/"
SCJ_PORTAL_URL = "https://consultasentenciascj.poderjudicial.gob.do/"
SCJ_ID_SEPARATOR = r"(?:[ \t\r\n]{0,12}[-\u00ad\u2010-\u2014\u2212][ \t\r\n]{0,12}|[ \t\r\n]{1,12})"
SCJ_IDENTIFIER_SOURCE = (
    rf"(?<!\w)SCJ{SCJ_ID_SEPARATOR}[A-Z]{{2}}{SCJ_ID_SEPARATOR}"
    rf"(?:\d{{2}}|\d{{4}}){SCJ_ID_SEPARATOR}\d{{3,5}}(?!\w)"
)
SCJ_IDENTIFIER_PATTERN = re.compile(SCJ_IDENTIFIER_SOURCE, re.I)
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

class DecisionExcerptRequest(BaseModel):
    """Retrieve bounded, page-anchored text from an official judgment."""
    url: HttpUrl
    expected_court: str
    expected_number: str
    page: int = Field(ge=1)
    query: str | None = Field(default=None, min_length=3, max_length=180)
    max_chars: int = Field(default=5000, ge=200, le=10000)

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

def canonical_scj_identifier(value: str | None) -> str | None:
    value = normalize_space(value)
    value = re.sub(r"^N[ÚU]M\.?\s*", "", value, flags=re.I)
    match = SCJ_IDENTIFIER_PATTERN.fullmatch(value)
    if not match:
        return None
    return "-".join(re.findall(r"[A-Z]+|\d+", match.group(0).upper()))

def allowed_host(host: str | None) -> bool:
    if not host: return False
    host = host.lower().strip(".")
    return any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES)

def _institutional_headers(text: str) -> list[tuple[str, int, int]]:
    headers = []
    for match in re.finditer(r"[^\n]*", (text or "")[:900]):
        line = fold(match.group(0)).strip(" .:\t")
        if re.match(r"^(?:la\s+)?(?:primera|segunda|tercera)\s+sala\s+de\s+la\s+suprema corte de justicia\b", line):
            headers.append(("SCJ", match.start(), match.end()))
        elif re.fullmatch(r"(?:republica dominicana\s+)?tribunal constitucional(?:\s+de la republica dominicana)?", line):
            headers.append(("TC", match.start(), match.end()))
        elif re.fullmatch(r"(?:republica dominicana\s+)?suprema corte de justicia(?:\s+de la republica dominicana)?", line):
            headers.append(("SCJ", match.start(), match.end()))
    return headers

def _primary_identifier(text: str) -> tuple[str, str] | None:
    opening = (text or "")[:4500]
    patterns = (
        ("TC", re.compile(r"\bTC/\d{3,4}/\d{2}\b", re.I)),
        ("SCJ", SCJ_IDENTIFIER_PATTERN),
    )
    candidates = sorted(
        (
            match.start(), match.end(), court,
            canonical_scj_identifier(match.group(0))
            if court == "SCJ" else match.group(0).upper(),
        )
        for court, pattern in patterns for match in pattern.finditer(opening)
    )
    historical = re.search(
        r"(?im)^\s*SENTENCIA\s+DEL\s+[^\n]{1,140}?N[ÚU]M\.?\s*(\d{1,5})\s*$",
        opening[:700],
    )
    if historical:
        candidates.append((historical.start(), historical.end(), "SCJ",
                           f"NÚM. {int(historical.group(1))}"))
        candidates.sort()
    scj_title = re.search(
        rf"(?im)^\s*SENTENCIA\s+DEL\s+[^\n]{{1,140}}?N[ÚU]M\.?\s*"
        rf"(?P<number>{SCJ_IDENTIFIER_SOURCE})\s*$",
        opening[:700],
    )
    if scj_title:
        candidates.append((
            scj_title.start("number"), scj_title.end("number"), "SCJ",
            canonical_scj_identifier(scj_title.group("number")),
        ))
        candidates.sort()
    headers = _institutional_headers(opening)
    if len({header[0] for header in headers}) > 1:
        return None
    if headers:
        header_court, header_start, header_end = headers[0]
        historical_titles = [item for item in candidates
                             if item[2] == "SCJ" and item[3].startswith("NÚM. ")
                             and 0 <= header_start - item[0] <= 700]
        if header_court == "SCJ" and historical_titles:
            return min(historical_titles, key=lambda item: item[0])[2:4]
        titled_identifiers = [
            item for item in candidates
            if item[2] == header_court and item[3].startswith("SCJ-")
            and 0 <= header_start - item[0] <= 700
            and re.match(
                r"\s*SENTENCIA\s+DEL\b",
                opening[opening.rfind("\n", 0, item[0]) + 1:item[0]],
                re.I,
            )
        ]
        titled_numbers = {item[3] for item in titled_identifiers}
        if header_court == "SCJ" and len(titled_numbers) == 1:
            return min(titled_identifiers, key=lambda item: item[0])[2:4]
        if header_court == "SCJ" and len(titled_numbers) > 1:
            return None
        nearby = [item for item in candidates
                  if header_start - 120 <= item[0] <= header_end + 1200]
        aligned = [item for item in nearby if item[2] == header_court]
        if aligned:
            labeled = [item for item in aligned
                       if re.match(r"\s*(?:sentencia|decision|decisi[oó]n)\b",
                                   opening[opening.rfind("\n", 0, item[0]) + 1:item[0]], re.I)]
            if labeled:
                return min(labeled, key=lambda item: item[0])[2:4]
            return min(aligned, key=lambda item: abs(item[0] - header_end))[2:4]
        if nearby:
            return min(nearby, key=lambda item: abs(item[0] - header_end))[2:4]
        return None
    if not candidates or candidates[0][0] > 350:
        return None
    first = candidates[0]
    labeled = [item for item in candidates
               if re.match(r"\s*(?:sentencia|decision|decisi[oó]n)\b",
                           opening[opening.rfind("\n", 0, item[0]) + 1:item[0]], re.I)]
    if labeled:
        return min(labeled, key=lambda item: item[0])[2:4]
    context = fold(opening[max(0, first[0] - 50):first[0]])
    if first[0] > 80 and not re.search(r"\b(?:sentencia|decision|decisi[oó]n)\b", context):
        return None
    return first[2:4]

def tokenize(v: str | None) -> set[str]:
    stop = {"de","la","el","los","las","un","una","y","o","a","en","por","para","con","del","que","se","su","sus","al","como","es","son","ha","han","ser","fue","esta","este","sobre","entre","cuando","sin","si","no","lo","le","les","más","mas"}
    return {t for t in re.findall(r"[a-z0-9]{3,}", fold(v)) if t not in {fold(w) for w in stop}}

def overlap_ratio(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    return len(ta & tb) / max(len(ta), 1) if ta else 0.0

def detect_court(host: str | None, expected: str | None = None, text: str | None = None):
    """Identify the issuing court from its formal heading and principal identifier."""
    headers = _institutional_headers(text or "")
    if len({header[0] for header in headers}) > 1:
        return None
    identifier = _primary_identifier(text or "")
    if headers:
        header_court = headers[0][0]
        if identifier and identifier[0] != header_court:
            return None
        return header_court
    return identifier[0] if identifier else None


def primary_decision_number(text: str, court: str | None):
    """Find the principal identifier anchored to its formal heading or title."""
    if court not in ("TC", "SCJ"):
        return None
    identifier = _primary_identifier(text)
    return identifier[1] if identifier and identifier[0] == court else None


def decision_number_matches(text: str, court: str | None, expected: str | None):
    identified = primary_decision_number(text, court)
    if not expected: return identified is not None
    if court == "SCJ":
        expected_identifier = canonical_scj_identifier(expected)
        if expected_identifier:
            return identified is not None and expected_identifier == identified
    return identified is not None and fold(identified) == fold(expected)

def evidence_classification_fields() -> dict[str, Any]:
    return {
        "evidence_type": "EXTRACTED_SOURCE_TEXT",
        "citation_classification": "NOT_PERFORMED",
        "interpretation": None,
        "criterion_status": "NOT_VERIFIED",
        "verified_criterion": None,
    }

def court_matches(detected: str | None, expected: str | None) -> bool:
    if not expected:
        return detected in ("SCJ", "TC")
    return detected == expected.strip().upper()

def clean_page_text(text: str) -> str:
    """Remove identifiable SCJ running furniture, including mid-sentence insertions.

    Keep substantive text and paragraph boundaries; never silently repair words.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # SCJ footer can be injected in the middle of a judicial paragraph by pypdf.
    # Match the complete identifying footer rather than deleting arbitrary lines.
    text = re.sub(
        r"SCJ-[A-Z]{2}-(?:\d{2}|\d{4})-\d{3,5}\s+Nacional,\s*Rep\.?\s*Dom\.?\s*"
        r"Tel\.?\s*:\s*\(?\d{3}\)?\s*\d{3}[-–]\d{4}\s*[•·]?\s*"
        r"Direcci[oó]n\s+de\s+Internet\s*:\s*https?\s*:\s*//\s*"
        r"(?:www\.)?poderjudicial\.gob\.do\s*PR\s*/\s*p[aá]g\.?\s*\d+",
        " ", text, flags=re.I,
    )
    # Other full footer variants (where the decision number is not repeated).
    text = re.sub(
        r"(?:Av\.?\s*Enrique\s+Jim[eé]nez\s+Moya[^\n]{0,140})?"
        r"Nacional,\s*Rep\.?\s*Dom\.?\s*Tel\.?\s*:\s*"
        r"\(?\d{3}\)?\s*\d{3}[-–]\d{4}\s*[•·]?\s*"
        r"Direcci[oó]n\s+de\s+Internet\s*:\s*https?\s*:\s*//\s*"
        r"(?:www\.)?poderjudicial\.gob\.do\s*PR\s*/\s*p[aá]g\.?\s*\d+",
        " ", text, flags=re.I,
    )
    text = re.sub(r"(?im)^\s*(?:REP[ÚU]BLICA DOMINICANA|SUPREMA CORTE DE JUSTICIA)\s*$", "", text)
    text = re.sub(r"(?im)^\s*(?:Av\.?\s*Enrique Jim[eé]nez Moya|Direcci[oó]n de Internet:|Tel\.:|PR/p[aá]g\.)[^\n]*$", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()

def context_for_page(pages: list[dict], page_number: int, window: int = 1) -> dict:
    """Return neighboring pages as context; not proof of authorship."""
    return {"page": page_number, "context_pages": [p["page"] for p in pages if abs(p["page"] - page_number) <= window]}

MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre")
SPANISH_NUMBERS = {"uno":1,"dos":2,"tres":3,"cuatro":4,"cinco":5,"seis":6,"siete":7,
    "ocho":8,"nueve":9,"diez":10,"once":11,"doce":12,"trece":13,"catorce":14,
    "quince":15,"dieciseis":16,"dieciséis":16,"diecisiete":17,"dieciocho":18,
    "diecinueve":19,"veinte":20,"veintiuno":21,"veintidos":22,"veintidós":22,
    "veintitres":23,"veintitrés":23,"veinticuatro":24,"veinticinco":25,
    "veintiseis":26,"veintiséis":26,"veintisiete":27,"veintiocho":28,
    "veintinueve":29,"treinta":30,"treinta y uno":31}

def extract_source_date(text: str, *, require_issuance_marker: bool = False) -> str | None:
    source = text or ""
    if not require_issuance_marker:
        numeric = re.search(
            r"\b(?:20\d{2}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})\b",
            source,
        )
        if numeric:
            return numeric.group(0)
    issuance_date = re.search(
        r"\ben fecha\s+(\d{1,2}\s+de\s+(?:"
        + "|".join(MONTHS)
        + r")\s+de\s+20\d{2})\b",
        source,
        re.I,
    )
    return normalize_space(issuance_date.group(1)) if issuance_date else None

def extract_metadata_from_text(text: str, court: str | None) -> dict[str, Any]:
    """Extract identifying fields from the opening, never from cited judgments."""
    opening = (text or "")[:6500]
    meta = {"decision_number": primary_decision_number(text, court), "date": None,
            "case_number": None, "chamber": None}
    if court == "TC":
        m = re.search(r"\bTC-\d{2}-\d{4}-\d{4}\b", opening, re.I)
        if m: meta["case_number"] = m.group(0).upper()
        meta["chamber"] = "Tribunal Constitucional"
        # Date is the issuance formula, not a date in the procedural history.
        m = re.search(r"\ba los\s+([a-záéíóúñ ]+?)\s*\(\s*(\d{1,2})\s*\)\s*d[ií]as?\s+del\s+mes\s+de\s+(" + "|".join(MONTHS) + r")\s+del\s+a[nñ]o\s+dos\s+mil\s+[a-záéíóúñ ]+?\(\s*(20\d{2})\s*\)", opening, re.I)
        if m: meta["date"] = f"{int(m.group(2))} de {m.group(3).lower()} de {m.group(4)}"
        if not meta["date"]:
            m = re.search(r"\ba los\s+([a-záéíóúñ ]+?)\s*\(\s*(\d{1,2})\s*\)\s*d[ií]as?\s+del\s+mes\s+de\s+(" + "|".join(MONTHS) + r")\s+del\s+a[nñ]o", opening, re.I)
            if m:
                y = re.search(r"\b(20\d{2})\b", opening[m.end():m.end()+110])
                if y: meta["date"] = f"{int(m.group(2))} de {m.group(3).lower()} de {y.group(1)}"
    elif court == "SCJ":
        m = re.search(r"\b\(?(20\d{2}-R\d{5,})\)?", opening, re.I)
        if m: meta["case_number"] = m.group(1)
        if not meta["case_number"]:
            m = re.search(r"\b(?:expediente|exp\.)\s*(?:n[úu]m\.?|no\.?)\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9./-]{5,})", opening, re.I)
            if m: meta["case_number"] = m.group(1)
        m = re.search(r"\b(PRIMERA|SEGUNDA|TERCERA)\s+SALA\s+DE\s+LA\s+SUPREMA", opening, re.I)
        if m: meta["chamber"] = m.group(1).capitalize() + " Sala"
        m = re.search(r"\ben fecha\s+(\d{1,2})\s+de\s+(" + "|".join(MONTHS) + r")\s+de\s+(20\d{2})", opening, re.I)
        if m: meta["date"] = f"{int(m.group(1))} de {m.group(2).lower()} de {m.group(3)}"
    return meta

# Attribution is deliberately conservative: a keyword alone is not judicial authorship.
REVIEWED_COURT_INTRO = re.compile(
    r"(?:esta sala|este tribunal|esta suprema corte|este tribunal constitucional)\s+"
    r"(?:considera|advierte|estima|observa|comprueba|entiende|juzga|verifica|retiene|establece)", re.I)
LOWER_COURT_INTRO = re.compile(
    r"(?:la corte a qua|la corte de apelaci[oó]n|el tribunal a quo|la jurisdicci[oó]n de fondo|"
    r"la sentencia impugnada|la corte recurrida|la alzada)", re.I)
PARTY_INTRO = re.compile(
    r"(?:la parte recurrente|la parte recurrida|el recurrente|la recurrente|"
    r"la demandante|la demandada|la apelante|el apelante)\s+"
    r"(?:alega|sostiene|afirma|argumenta|aduce|expone|invoca|señala)", re.I)
SCJ_PARTY_ASSERTION = re.compile(
    r"^(?:\d+[).]\s*)?que\s+(?:la parte (?:recurrente|recurrida) no ha establecido|"
    r"la alzada fundament[oó])\b", re.I)
QUOTED_LOWER_COURT = re.compile(
    r"(?:finalmente\s+concluye\s+la\s+corte\s+motivando\s+lo\s+siguiente)|"
    r"(?:la corte|el tribunal|la alzada)\s+(?:motiv[oó]|expres[oó]|estableci[oó]|"
    r"concluy[oó]|consider[oó])\s+(?:lo siguiente|que)|"
    r"(?:la sentencia|el fallo)\s+(?:impugnada|recurrida)\s+(?:establece|expresa|señala)", re.I)
DISPOSITIVE_INTRO = re.compile(r"^\s*(?:FALLA|DECIDE|RESUELVE)\s*:?\s*$", re.I)
NUMBERED_START = re.compile(r"(?m)^\s*\d{1,3}[).]\s+")

def strip_running_furniture(text: str) -> str:
    # Apply the same cleanup to analysis and promotion; preserve line breaks.
    text = clean_page_text(text)
    kept = []
    for line in text.splitlines():
        t = normalize_space(line)
        if re.match(r"^(?:Exp\. n[uú]m\.|Partes:|Materia:|Decisi[oó]n:|Ponente:|WT/|P[aá]g\.\s*\d+)", t, re.I):
            continue
        kept.append(line)
    return "\n".join(kept)

def split_units(text: str):
    """Conservative segmentation. Never alter numbered markers or discard short units."""
    text = strip_running_furniture(text.replace("\r\n", "\n").replace("\r", "\n"))
    # An inline numbered boundary must follow terminal punctuation; do not
    # mistake article numbers, dates, or citations for paragraph markers.
    text = re.sub(r"(?<=[.!?;:])([ \t]+)(?=\d{1,3}[).][ \t]+[A-ZÁÉÍÓÚÑ])", "\n", text)
    parts = re.split(r"\n[ \t]*\n+|\n(?=[ \t]*\d{1,3}[).][ \t]+)", text)
    return [normalize_space(part) for part in parts if normalize_space(part)]



def locate_support_context(pages: list[dict], support_page: int, support_quote: str) -> dict:
    """Anchor the quote on its declared page; read adjacent pages for context only.

    A quote on another page must never satisfy the declared-page check.
    Page furniture is removed by split_units; context is not proof of authorship.
    """
    empty = {"located": False, "unit": "", "preceding": "", "following": "",
             "preceding_page": None, "following_page": None}
    if not (1 <= support_page <= len(pages)):
        return empty

    current = split_units(pages[support_page - 1]["text"])
    index = next((i for i, unit in enumerate(current)
                  if normalized_contains(unit, support_quote)), None)
    if index is None:
        return empty

    preceding = current[index - 1] if index > 0 else ""
    following = current[index + 1] if index + 1 < len(current) else ""
    preceding_page = support_page if preceding else None
    following_page = support_page if following else None

    if not preceding and support_page > 1:
        previous = split_units(pages[support_page - 2]["text"])
        if previous:
            preceding, preceding_page = previous[-1], support_page - 1
    if not following and support_page < len(pages):
        next_units = split_units(pages[support_page]["text"])
        if next_units:
            following, following_page = next_units[0], support_page + 1

    return {"located": True, "unit": current[index], "preceding": preceding,
            "following": following, "preceding_page": preceding_page,
            "following_page": following_page}


def attribution_context(passage: str, preceding: str = "", following: str = "") -> tuple[str, list[str]]:
    """Clasificación conservadora; contexto no sustituye revisión jurídica humana."""
    p = normalize_space(passage)
    prior = normalize_space(preceding)
    nxt = normalize_space(following)
    indicators = []
    # Prioridad de exclusiones: nunca promocionar argumentos o citas ajenas.
    quote_intro = QUOTED_LOWER_COURT.search(p) or QUOTED_LOWER_COURT.search(prior[-650:])
    if quote_intro:
        return "LOWER_COURT_QUOTATION_REVIEW", ["lower_court_quotation_or_intro"]
    if PARTY_INTRO.search(p) or re.search(r"(?:en su memorial de casaci[oó]n|la parte recurrente sostiene)", prior[-450:], re.I):
        return "PARTY_ARGUMENT_OR_MIXED_REVIEW", ["party_argument_or_mixed"]
    if re.search(r"\b(?:la doctrina ha expresado|seg[uú]n la doctrina)\b", p, re.I):
        return "DOCTRINAL_QUOTATION_REVIEW", ["doctrine_citation"]
    if DISPOSITIVE_INTRO.match(p) or re.match(r"^(?:PRIMERO|SEGUNDO|TERCERO)\s*:", p, re.I):
        return "DISPOSITIVE_REVIEW", ["dispositive"]
    # Marcador judicial positivo explícito: candidato, no prueba concluyente.
    if REVIEWED_COURT_INTRO.search(p):
        indicators.append("explicit_deciding_court_reference")
    if re.search(r"\b(?:esta sala|esta suprema corte|este tribunal|esta corte de casaci[oó]n)\b", p, re.I):
        indicators.append("court_self_reference")
    # La continuidad argumentativa es solo señal de revisión, nunca prueba
    # suficiente para atribución judicial positiva.
    if re.search(r"\b(?:en primer orden|esto se debe a que|en ese sentido|por consiguiente|en consecuencia)\b", p, re.I):
        indicators.append("argumentative_continuity")
    if re.search(r"\b(?:esta decisi[oó]n|consideraciones que se exponen en lo adelante)\b", prior[-650:], re.I):
        indicators.append("preceding_judicial_context_candidate")
    if re.search(r"\b(?:en consecuencia|por tanto|as[ií] las cosas)\b", nxt[:400], re.I):
        indicators.append("following_argumentative_context")
    if "explicit_deciding_court_reference" in indicators or "court_self_reference" in indicators:
        return "POSSIBLE_JUDICIAL_REASONING_REVIEW", indicators
    # Caso de continuidad: requiere dos señales independientes; siempre es
    # una hipótesis sujeta a revisión, no una atribución certificada.
    if "argumentative_continuity" in indicators and "preceding_judicial_context_candidate" in indicators:
        return "POSSIBLE_JUDICIAL_REASONING_REVIEW", indicators
    return "UNDETERMINED_REQUIRES_CONTEXT", indicators

def classify_passage(passage: str, context: str = "") -> tuple[str, list[str]]:
    return attribution_context(passage, context)

def score_passage(passage: str, issue: str | None, attribution: str | None = None):
    attribution = attribution or classify_passage(passage)[0]
    it, pt = tokenize(issue), tokenize(passage)
    score = 5.0 * len(it & pt) / len(it) if it else 0.0
    score += {
        "POSSIBLE_JUDICIAL_REASONING_REVIEW": 2.0,
        "LOWER_COURT_QUOTATION_REVIEW": -2.0,
        "PARTY_ARGUMENT_OR_MIXED_REVIEW": -2.0,
        "DOCTRINAL_QUOTATION_REVIEW": -1.5,
        "DISPOSITIVE_REVIEW": 0.0,
        "UNDETERMINED_REQUIRES_CONTEXT": -1.5,
    }.get(attribution, 0.0)
    if len(passage) < 120: score -= 0.5
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

async def official_request(client, method: str, url: str, **kwargs):
    """Retry safe requests only for transient transport or server failures."""
    for attempt in range(MAX_OFFICIAL_REQUEST_ATTEMPTS):
        try:
            response = await getattr(client, method)(url, **kwargs)
        except httpx.TransportError:
            if attempt + 1 == MAX_OFFICIAL_REQUEST_ATTEMPTS:
                raise
        else:
            if (
                response.status_code not in RETRYABLE_OFFICIAL_STATUS
                or attempt + 1 == MAX_OFFICIAL_REQUEST_ATTEMPTS
            ):
                return response
        await asyncio.sleep(0.1 * (attempt + 1))

async def official_stream_request(
    client, method: str, url: str, *, max_bytes: int, **kwargs
):
    """Read a bounded official response, retrying only transient failures."""
    for attempt in range(MAX_OFFICIAL_REQUEST_ATTEMPTS):
        retry = False
        try:
            async with client.stream(method, url, **kwargs) as response:
                if 300 <= response.status_code < 400:
                    return response, b""
                if response.status_code in RETRYABLE_OFFICIAL_STATUS:
                    if attempt + 1 < MAX_OFFICIAL_REQUEST_ATTEMPTS:
                        retry = True
                    else:
                        response.raise_for_status()
                else:
                    response.raise_for_status()
                    claimed = response.headers.get("content-length")
                    if claimed and claimed.isdigit() and int(claimed) > max_bytes:
                        raise HTTPException(413, "El documento supera el límite de lectura permitido.")
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise HTTPException(413, "El documento supera el límite de lectura permitido.")
                        chunks.append(chunk)
                    return response, b"".join(chunks)
        except httpx.HTTPStatusError as exc:
            if (
                exc.response.status_code not in RETRYABLE_OFFICIAL_STATUS
                or attempt + 1 == MAX_OFFICIAL_REQUEST_ATTEMPTS
            ):
                raise
            retry = True
        except httpx.TransportError:
            if attempt + 1 == MAX_OFFICIAL_REQUEST_ATTEMPTS:
                raise
            retry = True
        if retry:
            await asyncio.sleep(0.1 * (attempt + 1))
    raise HTTPException(502, "No se obtuvo una respuesta oficial.")

async def fetch_document(url: str):
    current = url
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=10), follow_redirects=False,
                                     headers={"User-Agent": f"ELCOHE-JURIS-RD/{APP_VERSION}"}, trust_env=False) as client:
            for hop in range(MAX_REDIRECTS + 1):
                validate_official_url(current)
                response, data = await official_stream_request(
                    client, "GET", current, max_bytes=MAX_DOCUMENT_BYTES
                )
                if response.status_code in (301, 302, 303, 307, 308):
                    if hop == MAX_REDIRECTS: raise HTTPException(502, "Demasiadas redirecciones.")
                    location = response.headers.get("location")
                    if not location: raise HTTPException(502, "Redirección sin destino.")
                    current = urljoin(current, location)
                    continue
                ct = (response.headers.get("content-type") or "").split(";")[0].lower()
                return {"final_url": current, "host": urlparse(current).hostname,
                        "content_type": ct, "data": data,
                        "text_response": data.decode(response.encoding or "utf-8", errors="replace") if "html" in ct else None,
                        "http_status": response.status_code,
                        "sha256": hashlib.sha256(data).hexdigest()}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502,
            "No fue posible obtener el documento oficial: "
            f"HTTP {exc.response.status_code}.",
        ) from exc
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

def extract_numbered_foundations(pages: list[dict], court: str | None = None) -> list[dict]:
    """Recover numbered reasons across pages, including PDF inline boundaries.

    This is an extraction aid, NOT proof of judicial authorship. Never
    attribute holdings without subsequent human review of the original PDF.
    """
    units = []
    current = None
    section = "UNDETERMINED"
    for page in pages:
        cleaned = strip_running_furniture(page["text"])
        cleaned = re.sub(r"(?im)^\s*Expediente n[úu]m\..*?(?=\n|$)", "", cleaned)
        # pypdf sometimes joins numbered paragraphs on the same line.
        # Only split after punctuation and an uppercase initial; avoid article
        # numbers, decimal quantities, and citations inside sentences.
        cleaned = re.sub(
            r"(?<=[.!?;:])([ \t]+)(?=\d{1,3}[).][ \t]*[A-ZÁÉÍÓÚÑ])",
            "\n", cleaned,
        )
        for line in cleaned.splitlines():
            line = line.strip()
            if not line:
                continue
            fl = fold(line)
            if court == "TC":
                if "ii. consideraciones y fundamentos" in fl:
                    section = "DECIDING_COURT"
                    continue
                if section == "DECIDING_COURT" and fl == "del tribunal constitucional":
                    continue
                if re.match(r"^i\.\s*antecedentes", fl):
                    section = "BACKGROUND"
                    continue
                if fl.startswith("decide:"):
                    if current:
                        units.append(current)
                        current = None
                    section = "DISPOSITIVE"
                    continue
            elif court == "SCJ":
                # The deliberation heading can be split across lines or can
                # omit the literal 'la Primera Sala, después de haber'.
                if ("despues de haber deliberado" in fl or
                    "luego de haber deliberado" in fl or
                    "la sala, despues de deliberar" in fl):
                    section = "DECIDING_COURT"
                    continue
                if fl in ("falla:", "decide:", "resuelve:"):
                    if current:
                        units.append(current)
                        current = None
                    section = "DISPOSITIVE"
                    continue
            if court == "TC":
                m = (re.match(r"^(\d{1,2}\.\d{1,2})\.?\s*(?=[A-ZÁÉÍÓÚÑ])", line)
                     if section == "DECIDING_COURT" else None)
                if m is None and section == "DECIDING_COURT":
                    m = re.match(r"^(\d{1,2})\.\s+(?=[A-ZÁÉÍÓÚÑ])", line)
            else:
                m = (re.match(r"^(\d{1,3})[).]\s*(?=[A-ZÁÉÍÓÚÑ])", line)
                     if section == "DECIDING_COURT" else None)
            if m:
                if current:
                    units.append(current)
                current = {"foundation_number": m.group(1), "page": page["page"],
                           "end_page": page["page"], "lines": [line],
                           "section": section}
            elif current and section == current["section"]:
                current["lines"].append(line)
                current["end_page"] = page["page"]
    if current:
        units.append(current)
    for u in units:
        u["text"] = normalize_space(" ".join(u.pop("lines")))
    return units

def extract_dispositive_passage(pages: list[dict]) -> dict | None:
    """Extract the final explicit operative section as source text, not legal analysis."""
    marker = re.compile(r"^\s*(?:FALLA|DECIDE|RESUELVE)\s*:\s*(.*)$", re.I)
    signature = re.compile(
        r"^\s*(?:(?:la presente|esta) (?:sentencia|decisi[oó]n)"
        r".{0,100}\b(?:dada y firmada|firmada por)\b|firmad[oa]\s*:|"
        r"c[eé]sar jos[eé] garc[ií]a lucas,\s*secretario general|"
        r"(?:www\.)?poderjudicial\.gob\.do)",
        re.I,
    )
    markers = []
    cleaned_pages = []
    for page in pages:
        text = strip_running_furniture(page.get("text", ""))
        lines = text.splitlines()
        cleaned_pages.append((page["page"], lines))
        for line_index, line in enumerate(lines):
            if marker.match(line):
                markers.append((len(cleaned_pages) - 1, line_index))
    if not markers:
        return None

    start_page_index, start_line_index = markers[-1]
    extracted_lines = []
    end_page = pages[start_page_index]["page"]
    for page_index in range(start_page_index, len(cleaned_pages)):
        page_number, lines = cleaned_pages[page_index]
        start = start_line_index if page_index == start_page_index else 0
        if page_index > start_page_index:
            page_counter = next(
                (
                    index for index, line in enumerate(lines)
                    if re.match(r"^\s*P[aá]gina\s+\d+\s+de\s+\d+\b", line, re.I)
                ),
                None,
            )
            if page_counter is not None:
                start = page_counter + 1
        for line in lines[start:]:
            if signature.match(line):
                break
            if normalize_space(line):
                extracted_lines.append(normalize_space(line))
                end_page = page_number
        if any(signature.match(line) for line in lines[start:]):
            break
    text = normalize_space(" ".join(extracted_lines))
    operative_text = re.sub(
        r"^(?:FALLA|DECIDE|RESUELVE)\s*:\s*", "", text, count=1, flags=re.I
    ).strip()
    if not operative_text:
        return None
    return {
        "page": pages[start_page_index]["page"],
        "end_page": end_page,
        "text": text,
    }


def annotate_foundation_attribution(units: list[dict], court: str | None = None) -> list[dict]:
    """Track nested quotations and party submissions; prefer uncertainty to false attribution."""
    quoted=False
    party_response_open=False
    for u in units:
        txt=u["text"]
        if u.get("section")!="DECIDING_COURT":
            u["attribution"]="NON_DECIDING_SECTION_REVIEW";u["attribution_indicators"]=["section_not_deciding_court"];continue
        if court=="TC":
            # TC reasons may quote SCJ, parties or jurisprudence. They remain mixed until reviewed.
            attr, indicators=attribution_context(txt)
            if attr=="UNDETERMINED_REQUIRES_CONTEXT":
                attr="DECIDING_COURT_SECTION_REVIEW";indicators=["tc_own_considerations_section","requires_individual_review"]
            u["attribution"]=attr;u["attribution_indicators"]=indicators;continue
        # Explicit opening and closure of reproduced appellate reasoning.
        if re.search(r"(?:ordenanza|sentencia|fallo)\s+(?:impugnada|recurrida).{0,100}(?:transcriben|transcribe|textualmente)|(?:la alzada|la corte a qua).{0,110}(?:transcri|motiv[oó].{0,30}siguientes)",txt,re.I):
            quoted=True;u["attribution"]="LOWER_COURT_INTRODUCTION_REVIEW";u["attribution_indicators"]=["explicit_lower_court_quote_start"];continue
        # An appellate quotation ends when the SCJ resumes its own numbered
        # account of cassation submissions or analysis. In SCJ-PS-23-1546 the
        # quotation is ground 2; ground 3 starts "El recurrente pretende".
        # The previous implementation never saw that transition and marked
        # grounds 3-31 as lower-court quotations.
        quote_exit = re.search(
            r"^(?:\d+[).]\s+)?(?:el recurrente pretende|la recurrente pretende|"
            r"los recurrentes pretenden|la parte recurrente pretende|"
            r"los recurridos pretenden|la parte recurrida pretende|"
            r"en el desarrollo de su|seg[uú]n resulta del an[aá]lisis|"
            r"de lo transcrito|de la transcripci[oó]n|"
            r"del examen de la sentencia impugnada|"
            r"del contenido de la sentencia impugnada|"
            r"de la lectura de la sentencia impugnada)",
            txt[:300], re.I)
        if quoted and quote_exit:
            quoted=False
            attr, indicators=attribution_context(txt)
            if attr=="UNDETERMINED_REQUIRES_CONTEXT":
                attr="DECIDING_COURT_NARRATIVE_REVIEW"
                indicators=["explicit_quote_scope_exit", "requires_individual_review"]
            else:
                indicators=["explicit_quote_scope_exit"]+indicators
            u["attribution"]=attr;u["attribution_indicators"]=indicators;continue
        if quoted:
            u["attribution"]="LOWER_COURT_QUOTATION_REVIEW";u["attribution_indicators"]=["within_lower_court_quotation_scope"];continue
        attr, indicators=attribution_context(txt)
        if court == "SCJ" and SCJ_PARTY_ASSERTION.match(txt):
            attr = "PARTY_ARGUMENT_OR_MIXED_REVIEW"
            indicators = ["numbered_scj_party_response"]
            party_response_open = True
        elif court == "SCJ" and party_response_open and re.match(r"^\d+[).]\s*que\b", txt, re.I):
            attr = "PARTY_ARGUMENT_OR_MIXED_REVIEW"
            indicators = ["continuation_of_scj_party_response", "mixed_unit_requires_review"]
            if re.search(r"\besta (?:primera sala|sala|suprema corte|corte de casaci[oó]n)\b", txt, re.I):
                party_response_open = False
        elif court == "SCJ":
            party_response_open = False
        if attr=="UNDETERMINED_REQUIRES_CONTEXT":
            attr="DECIDING_COURT_SECTION_REVIEW";indicators=["scj_deliberation_section","requires_individual_review"]
        u["attribution"]=attr;u["attribution_indicators"]=indicators
    return units


def find_relevant_passages(pages, issue, limit, court=None):
    """Rank numbered grounds without silently discarding low-scoring grounds.

    Scores are *priorities for human review*, not findings of authorship or
    verified holdings. In particular, quoted lower-court reasoning is labeled
    and deprioritized, never represented as the deciding court's ratio.
    """
    units = annotate_foundation_attribution(extract_numbered_foundations(pages, court), court)
    candidates = []
    for i, unit in enumerate(units):
        attr = unit["attribution"]
        score = score_passage(unit["text"], issue, attr)
        if attr in ("DECIDING_COURT_SECTION_REVIEW", "DECIDING_COURT_NARRATIVE_REVIEW",
                    "POSSIBLE_JUDICIAL_REASONING_REVIEW"):
            score += 0.8
        if attr.startswith("LOWER_COURT_"):
            score -= 2.5
        # A zero/negative score means LOW PRIORITY, not "not a foundation".
        # Previously this filter removed 30 of 31 detected grounds.
        preceding = units[i-1]["text"] if i else ""
        following = units[i+1]["text"] if i+1 < len(units) else ""
        candidates.append({
            "page": unit["page"], "end_page": unit["end_page"],
            "foundation_number": unit["foundation_number"],
            "score": round(score, 3), "text": unit["text"][:1800],
            "text_truncated": len(unit["text"]) > 1800,
            "attribution": attr,
            "attribution_indicators": unit["attribution_indicators"],
            "preceding_context": preceding[-500:] or None,
            "following_context": following[:500] or None,
            "attribution_is_determinative": False,
            "ratio_decidendi_verified": False,
            "review_required": True,
        })
    # Favor passages mentioning the issue and judicial reasoning. A later
    # numbered ground can be more useful than the first factual background.
    candidates.sort(key=lambda x: (-x["score"], x["page"],
                                   int(x["foundation_number"].split(".")[0])))
    out = []
    seen = set()
    for c in candidates:
        key = (c["foundation_number"], c["page"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out

def normalized_contains(haystack, needle): return fold(needle) in fold(haystack)

@app.get("/health", operation_id="healthCheck")
def health():
    return {"ok":True,"service":"ELCOHE-JURIS RD Jurisprudence Actions","version":APP_VERSION}

async def _search_tc_candidates(q: str, year: int | None, limit: int) -> dict:
    """Locate candidates on the official TC portal; never certify a holding here."""
    # The official TC form filters years separately; appending the year to
    # searchString changes the query and does not activate the year filter.
    search_term = normalize_space(q)
    if len(search_term) < 2:
        raise HTTPException(status_code=422, detail="q debe contener al menos dos caracteres no blancos.")
    params = {"searchString": search_term, "size": max(limit, 10)}
    if year is not None:
        if year < 2012 or year > 2100:
            raise HTTPException(status_code=422, detail="Año de sentencia fuera del rango permitido.")
        params.update({"filtery": str(year), "criteriay": "years"})
    else:
        params["searchCriteria"] = ""
    headers = {"User-Agent": f"ELCOHE-JURIS-RD/{APP_VERSION}",
               "Accept": "text/html,application/xhtml+xml"}
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     max_redirects=MAX_REDIRECTS, headers=headers) as client:
            response = await official_request(
                client, "get", TC_SEARCH_URL, params=params
            )
            response.raise_for_status()
    except (httpx.HTTPError, httpx.TooManyRedirects) as exc:
        return {"query": search_term, "search_executed": False,
                "results": [], "status": "ACCESS_FAILED",
                "message": "No se pudo consultar el portal oficial del TC.",
                "error_type": type(exc).__name__,
                "http_status": exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError) else None}

    final_url = str(response.url)
    if not allowed_host(response.url.host):
        return {"query": search_term, "search_executed": False,
                "results": [], "status": "ACCESS_FAILED",
                "message": "La consulta redirigió fuera de los dominios oficiales permitidos."}
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type:
        return {"query": search_term, "source": final_url,
                "search_executed": True, "results": [],
                "status": "EXTRACTION_FAILED",
                "message": "El portal no devolvió una página HTML interpretable."}

    soup = BeautifulSoup(response.text, "html.parser")
    search_input = soup.find("input", id="searchString")
    echoed_query = normalize_space(
        search_input.get("value", "") if search_input else ""
    )
    query_echo_matches = echoed_query.casefold() == search_term.casefold()
    if not query_echo_matches:
        return {
            "query": search_term,
            "source": final_url,
            "search_executed": False,
            "results": [],
            "status": "EXTRACTION_FAILED",
            "message": "La página oficial no confirmó la consulta solicitada.",
        }
    if year is not None:
        selected_year = re.search(
            r'(?m)\bvar\s+filter\s*=\s*["\']?(\d{4})["\']?\s*;',
            response.text,
        )
        if not selected_year or int(selected_year.group(1)) != year:
            return {
                "query": search_term,
                "source": final_url,
                "search_executed": False,
                "results": [],
                "status": "EXTRACTION_FAILED",
                "message": "La página oficial no confirmó el filtro de año solicitado.",
            }

    pattern = re.compile(r"\bTC\s*/\s*\d{3,4}\s*/\s*\d{2}\b", re.I)
    page_has_decision_reference = bool(
        pattern.search(soup.get_text(" ", strip=True))
    )
    exact = pattern.search(search_term)
    expected = re.sub(r"\s+", "", exact.group(0)).upper() if exact else None
    results, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(final_url, anchor.get("href", ""))
        parsed = urlparse(href)
        if parsed.scheme != "https" or not allowed_host(parsed.hostname):
            continue
        if parsed.path.rstrip("/") == urlparse(TC_SEARCH_URL).path.rstrip("/"):
            continue
        # Search link text, title and nearby result-card text. Never treat a
        # number elsewhere on the page as proof that this link is its judgment.
        label = normalize_space(anchor.get_text(" ", strip=True))
        label += " " + normalize_space(anchor.get("title", ""))
        parent = anchor.find_parent(["article", "li", "tr"])
        nearby = normalize_space(parent.get_text(" ", strip=True)) if parent else ""
        if len(nearby) > 1200 or (parent and len(parent.find_all("a", href=True)) > 3):
            nearby = ""
        number = pattern.search(label)
        if not number:
            # Avoid associating an unrelated navigation link with a ruling.
            number = pattern.search(nearby)
        if not number:
            # Some official PDF links contain the judgment number in their URL.
            # Restrict this fallback to PDF filenames, not arbitrary URLs.
            filename = parsed.path.rsplit("/", 1)[-1]
            if filename.lower().endswith(".pdf"):
                number = re.search(r"tc[-_]?(\d{3,4})[-_](\d{2})", filename, re.I)
                if number:
                    decision_number = f"TC/{number.group(1)}/{number.group(2)}"
                else:
                    continue
            else:
                continue
        else:
            decision_number = re.sub(r"\s+", "", number.group(0)).upper()
        if expected and decision_number != expected:
            continue
        if year and not decision_number.endswith(f"/{year % 100:02d}"):
            continue
        key = (decision_number, href)
        if key in seen:
            continue
        seen.add(key)
        source_text = label
        if nearby:
            source_text += " " + nearby
        candidate = {
            "court": "TC",
            "decision_number": decision_number,
            "source_url": href,
            "status": "CANDIDATE",
            "verification_status": "CANDIDATE",
            "source_status": "NOT_VERIFIED",
            "identity_status": "NOT_VERIFIED",
            "passage_status": "NOT_VERIFIED",
            "official_document_verified": False,
            "ratio_decidendi_verified": False,
        }
        tribunal = re.search(
            r"\bTribunal Constitucional(?: de la República Dominicana)?\b",
            source_text,
            re.I,
        )
        if tribunal:
            candidate["tribunal"] = normalize_space(tribunal.group(0))
        date = extract_source_date(source_text)
        if date:
            candidate["date"] = date
        results.append(candidate)
        if len(results) >= limit:
            break

    if results:
        status, message = "CANDIDATES_FOUND", "Coincidencias localizadas; verificar cada sentencia oficial antes de atribuir criterios."
    elif not page_has_decision_reference and any(
        normalize_space(heading.get_text(" ", strip=True)).casefold()
        == "no hay resultados para mostrar"
        for heading in soup.find_all(["h3", "h4"])
    ):
        status = "NO_RESULTS_CONFIRMED"
        message = (
            "El portal TC mostró explícitamente cero resultados para esta consulta; "
            "esto no demuestra que no existan otras sentencias."
        )
    else:
        status, message = (
            "EXTRACTION_FAILED",
            "No se identificaron enlaces de sentencias ni un estado oficial explícito de cero resultados.",
        )
    return {"query": search_term, "source": final_url,
            "search_executed": True, "results": results,
            "status": status, "message": message}


@app.get("/v1/tc/search", operation_id="searchTcDecisions")
async def search_tc_decisions(
    q: str = Query(min_length=2),
    year: int | None = None,
    limit: int = Query(default=10, ge=1, le=20),
):
    return await _search_tc_candidates(q, year, limit)


async def search_tc(q: str, year: int | None = None, limit: int = 10) -> dict:
    """Keep the legacy integrated-search status while the API uses explicit states."""
    result = await _search_tc_candidates(q, year, limit)
    if result["status"] == "CANDIDATES_FOUND":
        result["status"] = "LOCATED_RESULTS"
    return result


@app.get("/v1/scj/search-plan", operation_id="prepareScjSearch")
def prepare_scj_search(q:str=Query(min_length=2), chamber:str|None=None, matter:str|None=None, year_from:int|None=None, year_to:int|None=None):
    query = normalize_space(q)
    if len(query) < 2:
        raise HTTPException(422, "q debe contener al menos dos caracteres no blancos.")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise HTTPException(400, "year_from no puede ser mayor que year_to.")
    if (year_from is None) != (year_to is None):
        raise HTTPException(422, "Para preparar un rango SCJ se requieren year_from y year_to.")
    if year_from is not None and (year_from < 1908 or year_to > 2100):
        raise HTTPException(422, "Año SCJ fuera del rango permitido.")
    if year_from is not None and year_to - year_from > 10:
        raise HTTPException(422, "El rango SCJ no puede superar 10 años por consulta.")
    return {
        "query": query,
        "official_portal": SCJ_PORTAL_URL,
        "filters": {
            key: value for key, value in {
                "sala": chamber, "materia": matter,
                "desde": year_from, "hasta": year_to,
            }.items() if value is not None
        },
        "search_executed": False,
        "filters_applied": False,
        "limitations": [
            "Este endpoint solo prepara una consulta; no accede al portal SCJ.",
            "Los filtros solo se aplican al ejecutar /v1/scj/search.",
        ],
        "status": "SEARCH_PLAN_ONLY",
    }

@app.post("/v1/verify", operation_id="verifyOfficialDocument")
async def verify_document(req:VerifyRequest):
    doc=await fetch_document(str(req.url)); ex=extract_document_text(doc); court=detect_court(doc["host"],req.expected_court,ex["full_text"]); meta=extract_metadata_from_text(ex["full_text"],court)
    expected_match = decision_number_matches(ex["full_text"],court,req.expected_number)
    court_ok = court_matches(court, req.expected_court)
    status="VERIFIED_SOURCE" if ex["extraction_ok"] and court_ok else "NOT_VERIFIED"
    if not expected_match: status="NOT_VERIFIED"
    return {"status":status,"official_domain":True,"court":court,"expected_court_match":court_ok,"final_url":doc["final_url"],"document_type":ex["document_type"],"page_count":ex["page_count"],"metadata":meta,"expected_number_match":expected_match,"sha256":doc["sha256"]}


class OfficialPdfRequest(BaseModel):
    url: HttpUrl
    expected_court: str
    expected_number: str

@app.post("/v1/resolve-official-pdf", operation_id="resolveOfficialPdf")
async def resolve_official_pdf(req: OfficialPdfRequest):
    """Find official PDF candidates on an official HTML page; do not claim they were verified."""
    doc = await fetch_document(str(req.url))
    if doc["data"][:4] == b"%PDF":
        return {"status": "PDF_CANDIDATE", "candidates": [doc["final_url"]],
                "note": "Use verifyOfficialPdf to examine this document."}
    if "html" not in doc["content_type"]:
        raise HTTPException(415, "La fuente no es HTML ni PDF.")
    soup = BeautifulSoup(doc["text_response"] or "", "html.parser")
    pdf_links = []
    expected = re.sub(r"[^a-z0-9]", "", fold(req.expected_number))
    for element in soup.select('a[href], iframe[src], embed[src], object[data]'):
        href = element.get('href') or element.get('src') or element.get('data')
        if not href:
            continue
        url = urljoin(doc["final_url"], href)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not allowed_host(parsed.hostname):
            continue
        if not parsed.path.lower().endswith('.pdf'):
            continue
        label = " ".join(
            (element.get_text(" ", strip=True), element.get("title", ""),
             element.get("download", ""), element.get("aria-label", ""))
        ).casefold()
        filename = parsed.path.rsplit("/", 1)[-1]
        identity_in_filename = bool(
            expected and expected in re.sub(r"[^a-z0-9]", "", fold(filename))
        )
        judgment_label = bool(
            re.search(r"\b(?:sentencia|fallo|decisi[oó]n|judgment)\b", label)
        )
        pdf_links.append((url, identity_in_filename, judgment_label))

    identity_matches = list(dict.fromkeys(
        url for url, identity_match, _ in pdf_links if identity_match
    ))
    if identity_matches:
        candidates = identity_matches
    else:
        contextual_matches = list(dict.fromkeys(
            url for url, _, judgment_label in pdf_links if judgment_label
        ))
        candidates = contextual_matches if len(contextual_matches) == 1 else []
    candidates = candidates[:20]
    return {"status": "PDF_CANDIDATES_FOUND" if candidates else "PDF_NOT_DISCOVERED",
            "candidates": candidates, "source_url": doc["final_url"],
            "note": "Los enlaces se priorizan por el identificador solicitado o su contexto judicial; la identidad solo se confirma verificando el contenido del PDF."}

@app.post("/v1/verify-official-pdf", operation_id="verifyOfficialPdf")
async def verify_official_pdf(req: OfficialPdfRequest):
    """Validate actual PDF bytes and extract text, without certifying a judicial holding."""
    doc = await fetch_document(str(req.url))
    if doc["data"][:4] != b"%PDF":
        return {"status": "NOT_PDF", "source_url": doc["final_url"],
                "content_type": doc["content_type"],
                "note": "La ficha HTML no equivale al PDF judicial."}
    ex = extract_document_text(doc)
    court = detect_court(doc["host"], req.expected_court, ex["full_text"])
    court_ok = court_matches(court, req.expected_court)
    number_ok = decision_number_matches(ex["full_text"], court, req.expected_number)
    ok = ex["extraction_ok"] and court_ok and number_ok
    return {"status": "PDF_TEXT_VERIFIED" if ok else "PDF_REQUIRES_REVIEW",
            "court": court, "expected_court_match": court_ok,
            "expected_number_match": number_ok, "text_extraction_ok": ex["extraction_ok"],
            "page_count": ex["page_count"], "source_url": doc["final_url"],
            "sha256_pdf": doc["sha256"], "document_type": "PDF",
            "ratio_decidendi_verified": False, "review_required": True,
            "note": "Para cotejar fundamentos concretos utilice readOfficialDecisionExcerpt con páginas específicas."}

@app.post("/v1/analyze-decision", operation_id="analyzeOfficialDecision")
async def analyze_official_decision(req:AnalyzeDecisionRequest):
    doc=await fetch_document(str(req.url)); ex=extract_document_text(doc); court=detect_court(doc["host"],req.expected_court,ex["full_text"]); meta=extract_metadata_from_text(ex["full_text"],court)
    expected_match=decision_number_matches(ex["full_text"],court,req.expected_number)
    court_ok=court_matches(court,req.expected_court)
    all_foundations = extract_numbered_foundations(ex["pages"], court)
    passages=find_relevant_passages(ex["pages"],req.legal_issue,req.max_passages,court)
    for passage in passages:
        passage.update(context_for_page(ex["pages"],passage["page"]))
        passage.update(evidence_classification_fields())
    status="HOLDING_CANDIDATE" if ex["extraction_ok"] and court_ok and expected_match and passages else ("VERIFIED_SOURCE" if ex["extraction_ok"] and court_ok and expected_match else "NOT_VERIFIED")
    return {"status":status,"official_source_verified":bool(ex["extraction_ok"] and court_ok and expected_match),**evidence_classification_fields(),"expected_court_match":court_ok,"expected_number_match":expected_match,"metadata":meta,"relevant_passages":passages,"extraction_diagnostics":{"numbered_foundations_detected":len(all_foundations),"foundation_numbers_detected":[u["foundation_number"] for u in all_foundations[:100]],"candidate_passages_returned":len(passages),"selection_limit":req.max_passages,"note":"La ausencia de un fundamento en candidatos no prueba su inexistencia en el PDF."},"jurisprudential_card":{"tribunal":court,"sala":meta.get("chamber"),"sentencia":meta.get("decision_number"),"fecha":meta.get("date"),"expediente":meta.get("case_number"),"estado_verificacion":status,"cuestion_juridica":req.legal_issue,"fundamentos_candidatos":passages,"ratio_decidendi":None,"regla_juridica":None,"review_required":True,"fuente_oficial":doc["final_url"],"paginas":ex["page_count"]},"source_url":doc["final_url"],"sha256":doc["sha256"]}

@app.post("/v1/decision-excerpt", operation_id="readOfficialDecisionExcerpt")
async def read_official_decision_excerpt(req: DecisionExcerptRequest):
    """Expose original extraction for precise quotation, not a judicial holding.

    Query is optional. When provided, return a bounded window around the first
    normalized occurrence, and an exact-match flag. Do not silently 'repair'
    extraction artifacts or cross page boundaries.
    """
    doc = await fetch_document(str(req.url))
    ex = extract_document_text(doc)
    court = detect_court(doc["host"], req.expected_court, ex["full_text"])
    if not (allowed_host(doc["host"]) and ex["extraction_ok"]
            and court_matches(court, req.expected_court)
            and decision_number_matches(ex["full_text"], court, req.expected_number)):
        raise HTTPException(422, "Identidad o fuente oficial no verificadas.")
    if req.page > ex["page_count"]:
        raise HTTPException(422, "Página fuera del documento.")
    raw = ex["pages"][req.page - 1]["text"]
    cleaned = strip_running_furniture(raw)
    # Search a whitespace-normalized copy to keep offsets stable and expose
    # only literal extracted content, not an inferred or paraphrased passage.
    searchable = normalize_space(cleaned)
    found = None
    occurrence_count = None
    if req.query:
        # Count matching occurrences for transparency. This is not a legal finding.
        haystack, needle = fold(searchable), fold(req.query)
        found = haystack.find(needle)
        occurrence_count = haystack.count(needle)
    if found is not None and found >= 0:
        start = max(0, found - req.max_chars // 3)
    else:
        start = 0
    end = min(len(searchable), start + req.max_chars)
    return {
        "status": "EXTRACTED_TEXT_REQUIRES_LEGAL_REVIEW",
        **evidence_classification_fields(),
        "metadata": extract_metadata_from_text(ex["full_text"], court),
        "page": req.page,
        "page_count": ex["page_count"],
        "query_found": found >= 0 if found is not None else None,
        "query_occurrences_on_page": occurrence_count,
        "excerpt": searchable[start:end],
        "excerpt_start": start,
        "excerpt_end": end,
        "page_text_length": len(searchable),
        "excerpt_truncated": start > 0 or end < len(searchable),
        "source_url": doc["final_url"],
        "sha256": doc["sha256"],
        "ratio_decidendi_verified": False,
        "review_required": True,
    }

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
        support_exact=normalized_contains(page_text,req.support_quote) or normalized_contains(strip_running_furniture(page_text),req.support_quote)
        issue_score=overlap_ratio(req.legal_issue,req.support_quote)
        prop_score=overlap_ratio(req.legal_proposition,req.support_quote)
    context = locate_support_context(ex["pages"], req.support_page, req.support_quote)
    support_located = context["located"]
    # Assess the full unit, not a cherry-picked excerpt. Context never
    # substitutes for the quote's exact presence on the declared page.
    support_unit = context["unit"]
    preceding = context["preceding"]
    following = context["following"]
    # A quotation that cannot be anchored to the declared page must not
    # receive a positive authorship signal solely from the supplied input.
    if support_located:
        attribution, attribution_indicators = attribution_context(
            support_unit, preceding, following
        )
    else:
        attribution, attribution_indicators = "UNDETERMINED_REQUIRES_CONTEXT", ["support_unit_not_located"]
    gates["support_attribution_heuristic"] = attribution
    gates["support_unit_located"] = support_located
    gates["support_context_preceding"] = preceding[-500:] if preceding else None
    gates["support_context_following"] = following[:500] if following else None
    gates["support_context_preceding_page"] = context["preceding_page"]
    gates["support_context_following_page"] = context["following_page"]
    gates["support_attribution_indicators"] = attribution_indicators
    gates["support_unit_text_truncated"] = len(support_unit) > 1800
    gates["support_quote_exact_on_page"]=support_exact
    gates["support_quote_raw_match"]=normalized_contains(ex["pages"][req.support_page-1]["text"], req.support_quote) if page_ok else False
    gates["support_quote_cleaned_match"]=normalized_contains(strip_running_furniture(ex["pages"][req.support_page-1]["text"]), req.support_quote) if page_ok else False
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
    if not support_located: reasons.append("La cita no pudo vincularse a una unidad argumentativa completa.")
    if attribution != "POSSIBLE_JUDICIAL_REASONING_REVIEW": reasons.append("No se acreditó atribución judicial positiva mediante los indicios disponibles; revisión de contexto necesaria.")
    reviewer_ok = bool(req.reviewer_name and req.reviewer_confirmation == "CONFIRMO_LECTURA_INTEGRAL")
    gates["reviewer_attestation"] = reviewer_ok
    if not reviewer_ok: reasons.append("Falta identificación y declaración expresa de revisión integral del revisor.")
    if issue_score<0.15: reasons.append("Conexión insuficiente con la cuestión jurídica.")
    if prop_score<0.18: reasons.append("Anclaje insuficiente de la proposición en el pasaje.")
    if not outcome_ok: reasons.append("Resultado/dispositivo no verificado.")
    ok=official and court_ok and req.judicial_reasoning_reviewed and number_ok and page_ok and support_exact and support_located and attribution == "POSSIBLE_JUDICIAL_REASONING_REVIEW" and reviewer_ok and issue_score>=0.15 and prop_score>=0.18 and outcome_ok
    if not official: reasons.append("Documento oficial no confirmado o extracción insuficiente.")
    if ok: reasons.append("Los controles heurísticos no acreditan por sí solos la ratio decidendi; no se emite certificación automática.")
    return {"status":"HOLDING_REQUIRES_INDEPENDENT_LEGAL_REVIEW" if ok else "HOLDING_NOT_PROMOTED",**evidence_classification_fields(),"court":court,"metadata":meta,"legal_issue":req.legal_issue,"legal_proposition":None,"support":{"page":req.support_page,"quote":req.support_quote if support_exact else None,"outcome_page":req.outcome_page,"outcome_quote":req.outcome_quote if outcome_ok else None},"gates":gates,"reasons":reasons,"verification_note":"La verificación documental y los indicadores de atribución no prueban por sí mismos la ratio decidendi. Requiere juicio jurídico humano y cotejo del dispositivo.","source_url":doc["final_url"],"sha256":doc["sha256"]}

# Registro persistente opcional; sin DATABASE_URL no altera los endpoints existentes.
from registry import router as registry_router
app.include_router(registry_router)

# Aprobación jurídica humana auditable (sin modificar los endpoints legados).
from registry_promotion import router as registry_promotion_router
app.include_router(registry_promotion_router)
from juris_163 import install
install(app)
from juris_164 import install as install_164
install_164(app)
