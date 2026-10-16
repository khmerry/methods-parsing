# -*- coding: utf-8 -*-
"""
3단계(통합): 전체 논문 -> Methods 추출 -> 후보 추출 -> DB 매칭.

[중요 교훈 / v2]
짧은 약어(BPA, XPS, TEM …)를 PubChem에 그대로 던지면 동명의 엉뚱한 화합물에
매칭된다(PubChem 1.19억 화합물). 그래서:
  (a) 본문 전체에서 'full name (ACR)' 정의를 모아 약어사전을 만들고,
      약어는 '풀네임으로' 바꿔서 DB에 질의한다.  (BPA -> benzylphosphonic acid)
  (b) 사전에 없는 '맨 약어'는 모호하므로 매칭하지 않고 LLM 단계로 넘긴다.
  (c) OLS 단일토큰 매칭은 '완전일치'만 신뢰(노이즈 제거).

카테고리를 모르는 후보를 DB로 역분류: PubChem->ingredient, CHMO->method, OBI->equipment.
vendor는 여기서 분류하지 않음(별도 'purchased from' 경로 + match_vendor).
"""

import re
from extract_methods import methods_from_path, find_methods, read_pdf_text
from extract_candidates import extract_candidates
from dblink import pubchem_name_to_cid, ols_search, canonical_key


# '(ABBR' 패턴 — 괄호 안 약어(뒤에 공백/쉼표/괄호). 예: (BPA, >97%) -> BPA
_ABBR_PAREN = re.compile(r"\(([A-Za-z][A-Za-z0-9\-]{1,9})(?=[\s,;:)])")


def _sh_match(words, letters):
    """
    Schwartz-Hearst 매칭: 약어 글자(letters)가 롱폼(words) 안에
    '순서대로' 등장하고, 약어 첫 글자 == 롱폼 첫 단어의 첫 글자인지 확인.
    """
    if not words or not words[0]:
        return False
    if words[0][0].lower() != letters[0]:
        return False
    text = " ".join(words).lower()
    i = 0
    for ch in text:
        if i < len(letters) and ch == letters[i]:
            i += 1
    return i == len(letters)


def build_acronym_map(full_text):
    """
    본문 전체에서 {약어: 풀네임} 사전을 만든다.
    Schwartz-Hearst로 '약어 글자와 맞아떨어지는 가장 짧은 풀네임'만 뽑아
    문맥 단어(we performed, in situ reaction of …)가 섞이지 않게 한다.
    """
    t = re.sub(r"-\n", "", full_text)
    t = re.sub(r"\s+", " ", t)
    amap = {}
    for m in _ABBR_PAREN.finditer(t):
        abbr = m.group(1)
        if not any(c.isupper() for c in abbr):          # 약어엔 대문자 필요
            continue
        letters = [c.lower() for c in abbr if c.isalnum()]
        if len(letters) < 2:
            continue
        pre = t[:m.start()].split()                     # 괄호 앞 단어들
        window = pre[-(len(letters) + 5):]              # 넉넉한 창(약어길이+5)
        # 오른쪽부터 늘려가며 '가장 짧은' 유효 롱폼을 찾는다
        best = None
        for start in range(len(window) - 1, -1, -1):
            cand = window[start:]
            if cand[0] and cand[0][0].isalpha() and _sh_match(cand, letters):
                best = " ".join(cand)
                break
        if best:
            amap.setdefault(abbr, best.strip(" ,.;:"))
    return amap


def _tokens(s):
    return set(t for t in re.split(r"[^a-z0-9]+", s.lower()) if t)


def _good_ols_match(surface, label):
    """OLS 퍼지 매칭 신뢰 판단. 단일토큰은 완전일치만, 복수토큰은 60% 이상 겹침."""
    st, lt = _tokens(surface), _tokens(label)
    if not st:
        return False
    if len(st) == 1:
        return surface.strip().lower() == label.strip().lower()   # 완전일치만
    return len(st & lt) / len(st) >= 0.6


def classify_by_db(surface, amap):
    """카테고리 미상 표현을 DB로 역분류. 매칭 시 dict, 아니면 None."""
    is_formula = bool(re.search(r"[A-Za-z]\d", surface))   # PbBr2, MoO3 같은 화학식
    if is_formula:
        term = surface                                 # 화학식은 그대로 질의
    else:
        term = amap.get(surface, surface)              # 약어면 풀네임으로 치환

    # 사전에 없는 '맨 약어'(모두 대문자)는 모호 -> 매칭하지 않음(LLM 단계로)
    bare_acronym = (surface not in amap) and (not is_formula) and bool(re.fullmatch(r"[A-Z]{2,6}", surface))
    if bare_acronym:
        return None

    # 1) ingredient : PubChem (풀네임/화학식으로 질의)
    cid = pubchem_name_to_cid(term)
    if cid:
        return {"category": "ingredient", "provenance": "established",
                "query": term, "normalized_ids": [{"db": "PubChem", "id": "CID " + cid}]}
    # 2) method : CHMO
    hit = ols_search(term, "chmo")
    if hit and _good_ols_match(term, hit["label"]):
        return {"category": "method", "provenance": "established",
                "query": term, "normalized_ids": [{"db": "CHMO", "id": hit["id"], "label": hit["label"]}]}
    # 3) equipment : OBI
    hit = ols_search(term, "obi")
    if hit and _good_ols_match(term, hit["label"]):
        return {"category": "equipment", "provenance": "established",
                "query": term, "normalized_ids": [{"db": "OBI", "id": hit["id"], "label": hit["label"]}]}
    return None


def run(path, limit=None):
    """논문 경로(.txt/.pdf) -> (methods, acronym_map, candidates, matched)."""
    if path.lower().endswith(".pdf"):
        full_text = read_pdf_text(path)
    else:
        with open(path, encoding="utf-8") as f:
            full_text = f.read()

    amap = build_acronym_map(full_text)                # 약어사전(전체 본문 기준)
    methods = find_methods(full_text)
    if not methods:
        return None, amap, [], []
    candidates = extract_candidates(methods)
    matched = []
    for s in candidates[:limit]:
        hit = classify_by_db(s, amap)
        if hit:
            matched.append({"surface": s, "canonical_key": canonical_key(s), **hit})
    return methods, amap, candidates, matched


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("사용법: python pipeline.py <논문.txt 또는 .pdf>"); sys.exit(0)
    methods, amap, cands, matched = run(sys.argv[1])
    print(f"Methods 글자수: {len(methods or '')} | 약어사전: {len(amap)}개 | 후보: {len(cands)} | 매칭: {len(matched)}\n")
    print(f"{'surface':22s} {'category':11s} {'id':22s} query(=질의에 쓴 표현)")
    print("-" * 95)
    for r in matched:
        nid = r["normalized_ids"][0]
        print(f"{r['surface']:22s} {r['category']:11s} {nid['db']+' '+nid['id']:22s} {r['query']}")
