"""Apply a conservative 1.6.3.1 patch to an existing local checkout.
Run: python upgrade_1631.py  (from repository root)
"""
from pathlib import Path
p=Path('juris_163.py')
s=p.read_text(encoding='utf-8')
assert 'async def inspect_pdf(' in s and 'async def research_integrated(' in s, 'Unexpected module version; no changes made'
assert 'async def fallback_tc(' not in s, 'Patch already applied'
needle='    async def inspect_pdf(url: str, court: str, number: str | None, issue: str, limit: int):'
addition='''    async def fallback_tc(number: str, limit: int):
        """Search alternate official TC page forms; candidates remain unverified."""
        from urllib.parse import urlencode, urljoin
        from app import TC_SEARCH_URL
        number_match = re.fullmatch(r"TC\\s*/\\s*(\\d{3,4})\\s*/\\s*(\\d{2})", number.strip(), re.I)
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
                        filename_matches = bool(re.search(rf'tc[-_]?{re.escape(digits)}[-_]{re.escape(yy)}(?:\\D|$)', filename))
                        text_matches = bool(re.search(rf'TC\\s*/\\s*{re.escape(digits)}\\s*/\\s*{re.escape(yy)}\\b', label + ' ' + nearby, re.I))
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
                for match in re.finditer(r'(?<!\\w)([a-z])\\.\\s+(?=[A-ZÁÉÍÓÚÑ])', raw):
                    marker = match.group(1)
                    start = match.start()
                    next_match = re.search(r'(?<!\\w)[a-z]\\.\\s+(?=[A-ZÁÉÍÓÚÑ])', raw[match.end():])
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

'''
assert s.count(needle)==1
s=s.replace(needle,addition+needle)
s=s.replace('        passages = find_relevant_passages(extracted["pages"], issue, limit, detected)', '        passages = find_relevant_passages(extracted["pages"], issue, limit, detected)\n        if detected == "TC":\n            passages = refine_tc_candidates(passages, extracted["pages"], issue, limit)')
old='''        if search.get("status") != "LOCATED_RESULTS":
            return {"version": "1.6.3", "status": "SEARCH_UNVERIFIED",
                    "search": search, "results": [], "review_required": True}'''
new='''        if search.get("status") != "LOCATED_RESULTS":
            fallback = await fallback_tc(req.expected_number, req.max_candidates) if req.expected_number else None
            if fallback and fallback.get("status") == "LOCATED_RESULTS":
                search = fallback
            else:
                return {"version": "1.6.3.1", "status": "SEARCH_UNVERIFIED",
                        "search": search, "fallback": fallback, "results": [], "review_required": True}'''
assert s.count(old)==1
s=s.replace(old,new).replace('"version": "1.6.3"','"version": "1.6.3.1"')
p.write_text(s,encoding='utf-8')
app=Path('app.py')
a=app.read_text(encoding='utf-8')
assert 'APP_VERSION = "1.6.2"' in a
a=a.replace('APP_VERSION = "1.6.2"','APP_VERSION = "1.6.3.1"',1)
app.write_text(a,encoding='utf-8')
print('Patched juris_163.py and app.py; run tests before deployment.')
