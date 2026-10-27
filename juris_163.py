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
            return {"version": "1.6.3", "mode": "DIRECT_OFFICIAL_PDF",
                    "result": await inspect_pdf(str(req.official_pdf_url), req.court,
                                                req.expected_number, req.legal_issue,
                                                req.max_passages)}
        if req.court == "SCJ":
            return {"version": "1.6.3", "status": "SCJ_SEARCH_NOT_IMPLEMENTED",
                    "note": "La función SCJ existente solo prepara consultas. Proporcione official_pdf_url y expected_number; no se inventan resultados."}
        search = await search_tc(q=req.expected_number or req.legal_issue,
                                 year=req.year, limit=req.max_candidates)
        if search.get("status") != "LOCATED_RESULTS":
            return {"version": "1.6.3", "status": "SEARCH_UNVERIFIED",
                    "search": search, "results": [], "review_required": True}
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
        return {"version": "1.6.3", "mode": "TC_SEARCH_AND_VERIFY",
                "status": "RESULTS_REQUIRE_LEGAL_REVIEW", "query": req.legal_issue,
                "results": results, "legal_analysis_verified": False,
                "review_required": True}
