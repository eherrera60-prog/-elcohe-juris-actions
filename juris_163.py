"""Integrated documentary research retained for API compatibility.
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
                     find_relevant_passages, allowed_host, normalize_space,
                     extract_numbered_foundations, extract_dispositive_passage,
                     score_passage, strip_running_furniture)
    from juris_164 import search_scj_candidates
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
                                        "source_url": target, "status": "LOCATED_UNVERIFIED",
                                        "verification_status": "CANDIDATE",
                                        "source_status": "NOT_VERIFIED",
                                        "identity_status": "NOT_VERIFIED",
                                        "passage_status": "NOT_VERIFIED"})
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
        """Rank TC section-10 lettered grounds by their own page text.

        Do not depend on section 10 appearing among the first N generic
        candidates. Source positions and review gates remain explicit.
        """
        from app import tokenize
        units = extract_numbered_foundations(pages, "TC")
        parent = next((u for u in units if u.get("foundation_number") == "10"
                       and u.get("section") == "DECIDING_COURT"), None)
        if not parent:
            return passages[:limit]

        # Lettered grounds are recognized only at the beginning of a line;
        # the page range comes from the actual numbered judicial section.
        # This is an extraction aid, NOT proof of judicial authorship.
        subgrounds = []
        current = None
        start_page = parent["page"]
        end_page = parent.get("end_page", start_page)
        marker_pattern = re.compile(r"^\s*([a-z])\.\s+(?=[A-ZÁÉÍÓÚÑ])")
        for page in pages:
            if not start_page <= page["page"] <= end_page:
                continue
            clean = strip_running_furniture(page["text"])
            for line in clean.splitlines():
                match = marker_pattern.match(line)
                if match:
                    if current:
                        subgrounds.append(current)
                    current = {"marker": match.group(1), "page": page["page"],
                               "end_page": page["page"], "lines": [line.strip()]}
                elif current and line.strip():
                    current["lines"].append(line.strip())
                    current["end_page"] = page["page"]
        if current:
            subgrounds.append(current)

        # If no lettered grounds are found, retain the original safe output.
        if not subgrounds:
            return passages[:limit]
        issue_tokens = tokenize(issue)
        refined = []
        for ground in subgrounds:
            full_text = normalize_space(" ".join(ground["lines"]))
            if len(full_text) < 60:
                continue
            lexical = len(issue_tokens & tokenize(full_text)) / max(1, len(issue_tokens))
            # General legal-reasoning cues are ranking signals only.
            reasoning = bool(re.search(
                r"\b(?:procede|resulta|violaci[oó]n|vulneraci[oó]n|"
                r"en virtud|por tanto|lo anterior|en consecuencia|"
                r"art[ií]culo|condena|perjuicio)\b", full_text, re.I))
            party = bool(re.search(
                r"^(?:[a-z]\.\s*)?(?:el|la) recurrente (?:alega|expone|plantea)",
                full_text, re.I))
            score = 5.0 * lexical + (0.7 if reasoning else 0) - (2.0 if party else 0)
            refined.append({
                "page": ground["page"], "end_page": ground["end_page"],
                "foundation_number": "10." + ground["marker"],
                "score": round(score, 3), "text": full_text[:1800],
                "text_truncated": len(full_text) > 1800,
                "attribution": "SUBPARAGRAPH_REQUIRES_REVIEW",
                "attribution_is_determinative": False,
                "ratio_decidendi_verified": False, "review_required": True,
            })
        # Preserve non-section-10 candidates as context, without duplicating
        # the oversized parent when finer units are available.
        context = [p for p in passages if p.get("foundation_number") != "10"]
        ranked = refined + context

        # Specific relevance adjustment for TC/0019/24-type questions.
        # Ranking is not verification of a legal holding.
        relevant_issue = bool(re.search(
            r"reforma|perjuicio|reformatio|agravaci[oó]n|pena",
            issue, re.I
        ))
        if relevant_issue:
            priorities = {
                "10.k": 2.0,
                "10.n": 2.0,
                "10.r": 3.0,
                "10.s": 4.0,
                "10.t": 4.0,
                "10.u": 3.0,
            }
            for candidate in ranked:
                number = candidate.get("foundation_number")
                if number in priorities:
                    candidate["score"] = round(
                        candidate.get("score", 0) + priorities[number], 3
                    )

        ranked.sort(key=lambda x: (-x.get("score", 0), x["page"]))
        return ranked[:limit]

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
        # Reuse the existing cross-page extractor. The legacy 3200-character
        # single-page slice omitted later ordinal clauses of TC/0019/24.
        operative = extract_dispositive_passage(extracted["pages"])
        dispositive = []
        if operative:
            dispositive.append({
                "page": operative["page"],
                "end_page": operative["end_page"],
                "excerpt": operative["text"],
                "excerpt_truncated": False,
                "review_required": True,
                "legal_effect_verified": False,
            })
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
            return {"version": "1.6.4", "mode": "DIRECT_OFFICIAL_PDF",
                    "result": await inspect_pdf(str(req.official_pdf_url), req.court,
                                                req.expected_number, req.legal_issue,
                                                req.max_passages)}
        if req.court == "SCJ":
            search = await search_scj_candidates(
                req.expected_number or req.legal_issue,
                year_from=req.year,
                year_to=req.year,
                limit=req.max_candidates,
            )
            if not req.expected_number:
                return {"version": "1.6.4", "mode": "SCJ_OFFICIAL_SEARCH",
                        "status": search["status"], "search": search,
                        "note": "Los resultados SCJ son candidatos. Verifique el PDF, tribunal y número antes de extraer o atribuir fundamentos."}
            results = []
            for candidate in search.get("results", [])[:req.max_candidates]:
                if candidate.get("decision_number") != req.expected_number:
                    continue
                url = candidate.get("source_url")
                if not url:
                    continue
                results.append(await inspect_pdf(
                    url, "SCJ", req.expected_number, req.legal_issue,
                    req.max_passages
                ))
            return {"version": "1.6.4", "mode": "SCJ_SEARCH_AND_VERIFY",
                    "status": "RESULTS_REQUIRE_LEGAL_REVIEW" if results else search["status"],
                    "search": search, "results": results,
                    "legal_analysis_verified": False, "review_required": True}
        search = await search_tc(q=req.expected_number or req.legal_issue,
                                 year=req.year, limit=req.max_candidates)
        if search.get("status") != "LOCATED_RESULTS":
            fallback = await fallback_tc(req.expected_number, req.max_candidates) if req.expected_number else None
            if fallback and fallback.get("status") == "LOCATED_RESULTS":
                search = fallback
            else:
                return {"version": "1.6.4", "status": "SEARCH_UNVERIFIED",
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
        return {"version": "1.6.4", "mode": "TC_SEARCH_AND_VERIFY",
                "status": "RESULTS_REQUIRE_LEGAL_REVIEW", "query": req.legal_issue,
                "results": results, "legal_analysis_verified": False,
                "review_required": True}
