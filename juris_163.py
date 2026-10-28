"""ELCOHE-JURIS RD 1.6.3: one-request documentary research.
Install by adding `from juris_163 import install; install(app)` at END of app.py.
This module uses the existing app.py security, extraction, and attribution helpers.
"""
from __future__ import annotations
import re
from fastapi import HTTPException
from pydantic import BaseModel, HttpUrl, Field

class IntegratedResearchRequest(BaseModel):
    legal_issue: str = Field(min_length=3, max_length=300)
    court: str = Field(default="TC", pattern="^(TC|SCJ)$")
    official_pdf_url: HttpUrl | None = None
    expected_number: str | None = Field(default=None, max_length=40)
    year: int | None = Field(default=None, ge=2012, le=2100)
    max_candidates: int = Field(default=3, ge=1, le=5)
    max_passages: int = Field(default=8, ge=1, le=12)


def install(app):
    # Import only at runtime, after app.py has declared all its helpers.
    from app import (search_tc, fetch_document, extract_document_text, detect_court,
                     court_matches, decision_number_matches, extract_metadata_from_text,
                     find_relevant_passages, allowed_host, normalize_space)
    from urllib.parse import urlparse
    from bs4 import BeautifulSoup

    async def fallback_tc(number: str, limit: int):
        """Search alternate official TC page forms; candidates remain unverified."""
        from urllib.parse import urlencode, urljoin
        from app import TC_SEARCH_URL
        number_match = re.fullmatch(r"TC\s*/\s*(\d{3,4})\s*/\s*(\d{2})", number.strip(), re.I)
        if not number_match:
            return {"status": "NO_EXACT_NUMBER", "results": [], "attempts": []}
        digits, yy = number_match.groups()
        canonical = f"TC/{digits}/{yy}"
        terms = [canonical, f"TC-{digits}-{yy}", f"{digits}/{yy}"]
        attempts, results, seen = [], [], set()
        for term in terms:
            for params in ({"searchString": term, "size": 20, "searchCriteria": ""},
                           {"searchString": term, "size": 20}):
                url = TC_SEARCH_URL + '?' + urlencode(params)
                try:
                    doc = await fetch_document(url)
                    if 'html' not in doc['content_type']:
                        attempts.append({"query": term, "status": "NOT_HTML"})
                        continue
                    soup = BeautifulSoup(doc['text_response'] or '', 'html.parser')
                    attempts.append({"query": term, "status": "HTML_RECEIVED"})
                    for tag in soup.select('a[href],iframe[src],embed[src],object[data]'):
                        raw = tag.get('href') or tag.get('src') or tag.get('data')
                        if not raw:
                            continue
                        target = urljoin(doc['final_url'], raw)
                        parsed = urlparse(target)
                        if parsed.scheme != 'https' or not allowed_host(parsed.hostname):
                            continue
                        label = normalize_space(tag.get_text(' ', strip=True) + ' ' + tag.get('title', ''))
                        parent = tag.find_parent(['article', 'li', 'tr'])
                        nearby = normalize_space(parent.get_text(' ', strip=True)) if parent else ''
                        if len(nearby) > 1200:
                            nearby = ''
                        filename = parsed.path.rsplit('/', 1)[-1].lower()
                        filename_matches = bool(re.search(rf'tc[-_]?{re.escape(digits)}[-_]{re.escape(yy)}(?:\D|$)', filename))
                        text_matches = bool(re.search(rf'TC\s*/\s*{re.escape(digits)}\s*/\s*{re.escape(yy)}\b', label + ' ' + nearby, re.I))
                        if not (filename_matches or text_matches):
                            continue
                        if target in seen:
                            continue
                        seen.add(target)
                        results.append({"court": "TC", "decision_number": canonical,
                                        "source_url": target, "status": "LOCATED_UNVERIFIED"})
                        if len(results) >= limit:
                            break
                except Exception as exc:
                    attempts.append({"query": term, "status": "SOURCE_ERROR", "error_type": type(exc).__name__})
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break
        return {"status": "LOCATED_RESULTS" if results else "EXTRACTION_UNVERIFIED",
                "results": results, "attempts": attempts}

    def refine_tc_candidates(passages, pages, issue, limit):
        """Prefer page-anchored TC subparagraphs; never certify judicial authorship."""
        from app import tokenize
        if not passages:
            return passages
        # Keep original candidates and add bounded subparagraphs only when
        # source page text contains the marker; never manufacture quotations.
        refined = []
        for item in passages:
            if item.get('foundation_number') != '10' or item.get('end_page', item['page']) <= item['page']:
                refined.append(item)
                continue
            for page in pages:
                if not (item['page'] <= page['page'] <= item['end_page']):
                    continue
                raw = normalize_space(page['text'])
                for match in re.finditer(r'(?<!\w)([a-z])\.\s+(?=[A-ZÁÉÍÓÚÑ])', raw):
                    marker = match.group(1)
                    start = match.start()
                    next_match = re.search(r'(?<!\w)[a-z]\.\s+(?=[A-ZÁÉÍÓÚÑ])', raw[match.end():])
                    end = match.end() + next_match.start() if next_match else min(len(raw), start + 1500)
                    excerpt = raw[start:end].strip()
                    if len(excerpt) < 80:
                        continue
                    tokens = tokenize(issue)
                    score = len(tokens & tokenize(excerpt)) / max(len(tokens), 1)
                    refined.append({"page": page['page'], "end_page": page['page'],
                                    "foundation_number": f"10.{marker}", "score": round(score, 3),
                                    "text": excerpt[:1800], "text_truncated": len(excerpt)>1800,
                                    "attribution": "SUBPARAGRAPH_REQUIRES_REVIEW",
                                    "attribution_is_determinative": False,
                                    "ratio_decidendi_verified": False, "review_required": True})
            # Retain parent as contextual fallback, never claim segmentation complete.
            refined.append(item)
        refined.sort(key=lambda x: (-x.get('score', 0), x['page']))
        return refined[:limit]

    async def inspect_pdf(url: str, court: str, number: str | None, issue: str, limit: int):
        doc = await fetch_document(url)
        if not doc["data"].startswith(b"%PDF"):
            return {"status": "NOT_PDF", "source_url": doc["final_url"]}
        extracted = extract_document_text(doc)
        detected = detect_court(doc["host"], court, extracted["full_text"])
        court_ok = court_matches(detected, court)
        number_ok = bool(number) and decision_number_matches(extracted["full_text"], detected, number)
        verified = bool(allowed_host(doc["host"]) and extracted["extraction_ok"] and court_ok and number_ok)
        metadata = extract_metadata_from_text(extracted["full_text"], detected)
        if not verified:
            return {"status": "DOCUMENT_REQUIRES_REVIEW", "source_url": doc["final_url"],
                    "metadata": metadata, "court_match": court_ok, "number_match": number_ok,
                    "sha256": doc["sha256"], "review_required": True}
        passages = find_relevant_passages(extracted["pages"], issue, limit, detected)
        if detected == "TC":
            passages = refine_tc_candidates(passages, extracted["pages"], issue, limit)
        # Identify likely dispositive pages, never assert outcome without legal review.
        last_pages = extracted["pages"][-min(8, extracted["page_count"]):]
        dispositive = []
        for p in last_pages:
            t = normalize_space(p["text"])
            marker = re.search(r"\b(?:DECIDE|FALLA|RESUELVE)\s*:", t, re.I)
            if marker:
                dispositive.append({"page": p["page"], "excerpt": t[marker.start():marker.start()+3200],
                                    "excerpt_truncated": len(t) > marker.start()+3200})
        return {
            "status": "DOCUMENT_VERIFIED_ANALYSIS_CANDIDATES",
            "official_document_verified": True,
            "legal_analysis_verified": False,
            "ratio_decidendi_verified": False,
            "review_required": True,
            "metadata": metadata, "page_count": extracted["page_count"],
            "source_url": doc["final_url"], "sha256_pdf": doc["sha256"],
            "legal_issue": issue,
            "candidate_reasoning": passages,
            "dispositive_excerpts": dispositive,
            "warning": "Los pasajes son candidatos de lectura. No se atribuye automáticamente una ratio decidendi ni se valida el criterio jurídico."
        }

    @app.post("/v1/research-integrated", operation_id="researchJurisprudenceIntegrated",
              tags=["Investigación integral 1.6.3"])
    async def research_integrated(req: IntegratedResearchRequest):
        """One request: locate (TC), resolve, verify PDF, extract candidate grounds and dispositive.
        SCJ supports official PDF supplied by caller; its search-plan is not an actual search.
        """
        if req.official_pdf_url:
            if not req.expected_number:
                raise HTTPException(422, "expected_number es obligatorio para verificar un PDF.")
            return {"version": "1.6.3.1", "mode": "DIRECT_OFFICIAL_PDF",
                    "result": await inspect_pdf(str(req.official_pdf_url), req.court,
                                                req.expected_number, req.legal_issue,
                                                req.max_passages)}
        if req.court == "SCJ":
            return {"version": "1.6.3.1", "status": "SCJ_SEARCH_NOT_IMPLEMENTED",
                    "note": "La función SCJ existente solo prepara consultas. Proporcione official_pdf_url y expected_number; no se inventan resultados."}
        search = await search_tc(q=req.expected_number or req.legal_issue,
                                 year=req.year, limit=req.max_candidates)
        if search.get("status") != "LOCATED_RESULTS":
            fallback = await fallback_tc(req.expected_number, req.max_candidates) if req.expected_number else None
            if fallback and fallback.get("status") == "LOCATED_RESULTS":
                search = fallback
            else:
                return {"version": "1.6.3.1", "status": "SEARCH_UNVERIFIED",
                        "search": search, "fallback": fallback, "results": [], "review_required": True}
        results = []
        for candidate in search.get("results", [])[:req.max_candidates]:
            url = candidate["source_url"]
            number = candidate["decision_number"]
            try:
                doc = await fetch_document(url)
                pdf_urls = []
                if doc["data"].startswith(b"%PDF"):
                    pdf_urls = [doc["final_url"]]
                elif "html" in doc["content_type"]:
                    soup = BeautifulSoup(doc["text_response"] or "", "html.parser")
                    from urllib.parse import urljoin
                    for el in soup.select('a[href],iframe[src],embed[src],object[data]'):
                        link = el.get('href') or el.get('src') or el.get('data')
                        if not link: continue
                        target = urljoin(doc["final_url"], link)
                        if urlparse(target).path.lower().endswith(".pdf") and allowed_host(urlparse(target).hostname):
                            if target not in pdf_urls: pdf_urls.append(target)
                if not pdf_urls:
                    results.append({"decision_number": number, "status": "PDF_NOT_RESOLVED",
                                    "candidate_url": url, "review_required": True})
                    continue
                found = None
                for pdf in pdf_urls[:5]:
                    item = await inspect_pdf(pdf, "TC", number, req.legal_issue, req.max_passages)
                    if item.get("official_document_verified"):
                        found = item
                        break
                results.append(found or {"decision_number": number, "status": "PDF_NOT_VERIFIED",
                                         "candidate_url": url, "review_required": True})
            except Exception as exc:
                results.append({"decision_number": number, "status": "SOURCE_ERROR",
                                "error_type": type(exc).__name__, "review_required": True})
        return {"version": "1.6.3.1", "mode": "TC_SEARCH_AND_VERIFY",
                "status": "RESULTS_REQUIRE_LEGAL_REVIEW", "query": req.legal_issue,
                "results": results, "legal_analysis_verified": False,
                "review_required": True}
