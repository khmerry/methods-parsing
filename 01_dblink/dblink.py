# -*- coding: utf-8 -*-
"""
01_dblink : 추출된 표현(surface)을 외부 데이터베이스에 매칭하는 모듈 (v2).

카테고리별 전략
---------------
    ingredient -> PubChem(1순위) → 없으면 Wikidata 폴백(+교차 ID 수확)
    method     -> CHMO/OBI (OLS)                    (Wikidata 폴백은 노이즈↑라 생략)
    equipment  -> OBI/CHMO(OLS, 1순위) → 없으면 Wikidata 폴백
    vendor     -> Wikidata (회사/제조사 타입 필터)

Wikidata 사용 원칙 (이번 업그레이드 핵심)
-----------------------------------------
- 'wbsearchentities'(퍼지+별칭)로 후보 여러 개를 받고,
- **타입 필터**(설명 키워드 / 화학 식별자 속성)로 엉뚱한 것(동음이의)을 걸러낸다.
  (예: 'Kratos' 검색 1위는 아이돌 앨범, 'Gatan'은 성씨 → 필터로 제거)
- 화학물질이면 교차 ID(P662=PubChem, P683=ChEBI, P231=CAS)를 함께 수확한다.

API 키: 전부 불필요. 레이트리밋/429 백오프/User-Agent 예절은 지킨다.
의존성: 표준 라이브러리만(urllib, json).
"""

import urllib.request
import urllib.parse
import urllib.error
import json
import time
import re
import os

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
USER_AGENT = "methods-parsing-research/0.1"   # (권장) 괄호로 연락처 추가 가능
_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dblink_cache.json")

# method의 Wikidata 폴백은 노이즈가 커서 기본 끔(원하면 True)
WIKIDATA_FALLBACK_METHOD = False

# ---------------------------------------------------------------------------
# 캐시
# ---------------------------------------------------------------------------
try:
    with open(_CACHE_PATH, encoding="utf-8") as _f:
        _cache = json.load(_f)
except Exception:
    _cache = {}

def _save_cache():
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=0)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# HTTP (레이트리밋 + 429 백오프)
# ---------------------------------------------------------------------------
_last_call = {"t": 0.0}

def _http_get(url, min_interval=0.25, retries=3):
    """
    GET 호출 + 본문 반환.
    min_interval: 직전 호출과 최소 간격(초). PubChem 0.25s, Wikidata는 1.1s 권장.
    retries: 429(Too Many Requests) 시 Retry-After/지수백오프로 재시도.
    """
    for attempt in range(retries + 1):
        gap = time.time() - _last_call["t"]
        if gap < min_interval:
            time.sleep(min_interval - gap)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                _last_call["t"] = time.time()
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # 429/503(일시 과부하)면 쉬고 재시도, 그 외/최종 실패는 올림
            if e.code in (429, 503) and attempt < retries:
                wait = int(e.headers.get("Retry-After", "0") or 0) or (2 ** (attempt + 1))
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError:
            # 연결/타임아웃 등 일시 오류는 잠깐 쉬고 재시도
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise

# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
def canonical_key(s):
    """소문자 + 영숫자 외 기호를 '-'로 + 양끝 정리. 예: 'AXIS-Ultra DLD' -> 'axis-ultra-dld'"""
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")

# ---------------------------------------------------------------------------
# 1) PubChem : 이름 -> CID (ingredient)
# ---------------------------------------------------------------------------
def pubchem_name_to_cid(name):
    ckey = "pubchem:" + name.lower()
    if ckey in _cache:
        return _cache[ckey]
    result = None
    try:
        enc = urllib.parse.quote(name)
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{enc}/cids/TXT"
        txt = _http_get(url)
        result = txt.split()[0] if txt.strip() else None
        _cache[ckey] = result          # 성공(빈 결과 포함) -> 캐시
        _save_cache()
    except urllib.error.HTTPError as e:
        if e.code == 404:              # 진짜 '없음' -> 캐시
            _cache[ckey] = None
            _save_cache()
        result = None                  # 그 외(429 등 일시 오류)는 캐시하지 않음
    except Exception:
        result = None                  # 네트워크/타임아웃 -> 캐시하지 않음
    return result

# ---------------------------------------------------------------------------
# 2) OLS4 : 온톨로지 검색 (method=CHMO, equipment=OBI)
# ---------------------------------------------------------------------------
def ols_search(term, ontology, exact=False):
    ckey = f"ols:{ontology}:{int(exact)}:{term.lower()}"
    if ckey in _cache:
        return _cache[ckey]
    result = None
    try:
        enc = urllib.parse.quote(term)
        url = f"https://www.ebi.ac.uk/ols4/api/search?q={enc}&ontology={ontology}&rows=1"
        if exact:
            url += "&exact=true"
        docs = json.loads(_http_get(url)).get("response", {}).get("docs", [])
        if docs:
            result = {"id": docs[0].get("obo_id"), "label": docs[0].get("label")}
        _cache[ckey] = result          # 성공(빈 결과 포함) -> 캐시
        _save_cache()
    except Exception:
        result = None                  # 일시 오류는 캐시하지 않음
    return result

# ---------------------------------------------------------------------------
# 3) Wikidata : 퍼지 검색 + 타입 필터 + 교차 ID 수확
# ---------------------------------------------------------------------------
def wikidata_search(term, limit=10):
    """wbsearchentities로 후보 리스트 [{qid,label,description}] 반환."""
    ckey = f"wds:{limit}:{term.lower()}"
    if ckey in _cache:
        return _cache[ckey]
    out = []
    try:
        q = urllib.parse.quote(term)
        url = ("https://www.wikidata.org/w/api.php?action=wbsearchentities"
               f"&search={q}&language=en&format=json&type=item&limit={limit}")
        for d in json.loads(_http_get(url, min_interval=1.1)).get("search", []):
            out.append({"qid": d["id"], "label": d.get("label", ""),
                        "description": d.get("description", "")})
        _cache[ckey] = out             # 성공 -> 캐시
        _save_cache()
    except Exception:
        out = []                       # 일시 오류는 캐시하지 않음
    return out

def _wd_claims(qid, prop):
    """특정 아이템(qid)의 특정 속성(prop) 값들을 반환."""
    ckey = f"wdc:{qid}:{prop}"
    if ckey in _cache:
        return _cache[ckey]
    vals = []
    try:
        url = (f"https://www.wikidata.org/w/api.php?action=wbgetclaims"
               f"&entity={qid}&property={prop}&format=json")
        cs = json.loads(_http_get(url, min_interval=1.1))["claims"].get(prop, [])
        for c in cs:
            v = c["mainsnak"].get("datavalue", {}).get("value")
            if v is not None:
                vals.append(v)
        _cache[ckey] = vals            # 성공 -> 캐시
        _save_cache()
    except Exception:
        vals = []                      # 일시 오류는 캐시하지 않음
    return vals

def wikidata_crossids(qid):
    """화학물질 아이템에서 교차 ID 수확: P662=PubChem CID, P683=ChEBI, P231=CAS."""
    ids = {}
    cid = _wd_claims(qid, "P662")
    if cid:
        ids["PubChem"] = "CID " + str(cid[0])
    chebi = _wd_claims(qid, "P683")
    if chebi:
        v = str(chebi[0])
        ids["ChEBI"] = v if v.upper().startswith("CHEBI") else "CHEBI:" + v
    cas = _wd_claims(qid, "P231")
    if cas:
        ids["CAS"] = str(cas[0])
    return ids

# 타입 필터용 '설명(description) 키워드'
_WD_KEYWORDS = {
    "vendor": ["company", "manufacturer", "corporation", "enterprise",
               "business", "brand", "firm", "maker"],
    "equipment": ["device", "instrument", "microscope", "spectrometer",
                  "spectrophotometer", "spectrofluorometer", "spectroradiometer",
                  "camera", "analyzer", "analyser", "apparatus", "laser",
                  "monochromator", "cryostat", "detector"],
}

def wikidata_lookup(term, kind):
    """
    term을 Wikidata에서 찾되 kind(chemical/equipment/vendor)에 맞는 후보만 채택.
    - chemical: 화학 식별자 속성(P662/P683/P231)이 있거나 설명에 'chemical/compound'.
    - equipment/vendor: 설명(description)에 해당 키워드.
    채택되면 {qid,label,description,crossids} 반환, 없으면 None.
    """
    for c in wikidata_search(term, limit=10):
        desc = c["description"].lower()
        if kind == "chemical":
            cross = wikidata_crossids(c["qid"])          # 식별자 있으면 화학물질로 확정
            if cross or "chemical" in desc or "compound" in desc:
                return {**c, "crossids": cross}
        else:
            if any(k in desc for k in _WD_KEYWORDS.get(kind, [])):
                return {**c, "crossids": {}}
    return None

# ---------------------------------------------------------------------------
# 4) 디스패처
# ---------------------------------------------------------------------------
def match_entity(surface, category):
    """surface+category -> 외부 DB 매칭. 매칭되면 provenance='established'."""
    rec = {"surface": surface, "category": category,
           "canonical_key": canonical_key(surface),
           "normalized_ids": [], "provenance": None}

    if category == "ingredient":
        cid = pubchem_name_to_cid(surface)
        if cid:
            rec["normalized_ids"].append({"db": "PubChem", "id": "CID " + cid})
        else:
            wd = wikidata_lookup(surface, "chemical")       # 폴백 + 교차 ID
            if wd:
                rec["normalized_ids"].append({"db": "Wikidata", "id": wd["qid"], "label": wd["label"]})
                for db, i in wd["crossids"].items():
                    rec["normalized_ids"].append({"db": db, "id": i})

    elif category == "method":
        hit = ols_search(surface, "chmo") or ols_search(surface, "obi")
        if hit:
            rec["normalized_ids"].append({"db": "CHMO/OBI", "id": hit["id"], "label": hit["label"]})
        elif WIKIDATA_FALLBACK_METHOD:
            wd = wikidata_lookup(surface, "equipment")      # (키워드셋 공유; 기본 꺼짐)
            if wd:
                rec["normalized_ids"].append({"db": "Wikidata", "id": wd["qid"], "label": wd["label"]})

    elif category == "equipment":
        hit = ols_search(surface, "obi") or ols_search(surface, "chmo")
        if hit:
            rec["normalized_ids"].append({"db": "OBI/CHMO", "id": hit["id"], "label": hit["label"]})
        else:
            wd = wikidata_lookup(surface, "equipment")      # 폴백
            if wd:
                rec["normalized_ids"].append({"db": "Wikidata", "id": wd["qid"], "label": wd["label"]})

    if rec["normalized_ids"]:
        rec["provenance"] = "established"
    return rec

def match_vendor(name):
    """제조사/회사명을 Wikidata에서 회사 타입으로 확인."""
    wd = wikidata_lookup(name, "vendor")
    return {"surface": name, "canonical_key": canonical_key(name),
            "qid": wd["qid"] if wd else None,
            "label": wd["label"] if wd else None,
            "matched": bool(wd)}

# ---------------------------------------------------------------------------
# 직접 실행 시 테스트
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    for s, c in [("benzylphosphonic acid", "ingredient"),
                 ("methylammonium bromide", "ingredient"),   # PubChem MISS -> Wikidata 폴백?
                 ("X-ray photoelectron spectroscopy", "method"),
                 ("scanning electron microscope", "equipment")]:
        print(match_entity(s, c))
    for v in ["Kratos", "Hamamatsu Photonics", "Gatan", "Sigma-Aldrich"]:
        print(match_vendor(v))
