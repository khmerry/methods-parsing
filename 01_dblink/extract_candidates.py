# -*- coding: utf-8 -*-
"""
2단계: Methods 본문에서 '대문자 명사/명사구' 후보를 뽑는다.

전략 (정규식 기반, 외부 의존성 없음)
-------------------------------------
세 가지 패턴을 모아서 후보로 삼는다:
  (1) 대문자로 시작하는 연속 토큰 = 고유명사/명사구 근사 (예: 'AXIS-Ultra DLD', 'Themis Z')
  (2) 전부 대문자 약어 (예: XPS, UPS, TEM, EELS, THF)
  (3) 화학식류 (예: PbBr2, MoO3)
그 다음 불용어(문장 첫머리 기능어, Figure/Table 등)를 제거하고 중복을 없앤다.

주의: 이건 '재현율 위주'의 거친 후보다. 정밀도는 뒷단(DB 매칭/LLM)에서 걸러진다.
      소문자 일반명사(spin coating, benzylphosphonic acid 등)는 이 단계에서 못 잡는다.
      → 더 좋은 방법은 spaCy 명사청크(noun-chunk)지만, 여기선 무의존성 버전을 제공.
"""

import re

# 문장 첫머리에 자주 와서 '대문자로 시작'하는 비(非)개체 단어들 + 참조/구역 단어
_STOP = set("""
the we a an in on of for to by with and or but as at from into onto than then thus
this that these those it its their our your his her they he she you i
during after before finally then here there also first second third both each all any
due unless however therefore moreover furthermore where when while which who whom
images samples figure fig table supplementary extended data methods materials
preparation fabrication characterization lifetime optical results discussion conclusion
abstract introduction acknowledgements references appendix section using used use
measured obtained performed deposited prepared dissolved added shown given based
""".split())


def _clean(text):
    """PDF 줄바꿈 하이픈 복원 + 공백 정리.  예: 'bro-\\nmide' -> 'bromide'."""
    text = re.sub(r"-\n", "", text)      # 줄 끝 하이픈으로 끊긴 단어 합치기
    text = re.sub(r"\s+", " ", text)     # 모든 공백을 한 칸으로
    return text


# (1) 대문자 시작 연속 토큰 (단어 사이 공백 1개 허용, 하이픈/슬래시/점/콜론 포함)
_seq_re = re.compile(r"[A-Z][A-Za-z0-9.\-/:]*(?:\s[A-Z0-9][A-Za-z0-9.\-/:]*)*")
# (2) 전부 대문자 약어 (뒤에 숫자 허용): XPS, MoO? no. XPS, TEM, THF ...
_acr_re = re.compile(r"\b[A-Z]{2,}[0-9]*\b")
# (3) 화학식류: 대문자+(소문자?)+숫자 가 이어지는 형태 PbBr2, MoO3
_formula_re = re.compile(r"\b(?:[A-Z][a-z]?\d+)+[A-Za-z0-9]*\b")


def extract_candidates(methods_text):
    """Methods 본문 -> 후보 표현 리스트(중복 제거, 등장 순서 유지)."""
    text = _clean(methods_text)
    raw = []
    raw += [m.group() for m in _seq_re.finditer(text)]
    raw += [m.group() for m in _acr_re.finditer(text)]
    raw += [m.group() for m in _formula_re.finditer(text)]

    seen = set()
    out = []
    for s in raw:
        s = s.strip(" ,.;:()[]")                 # 양끝 문장부호 제거
        if len(s) < 2:
            continue
        if s.isdigit():                          # 순수 숫자 제외
            continue
        low = s.lower()
        if low in _STOP:                         # 불용어(단일 단어) 제외
            continue
        key = low
        if key in seen:                          # 중복 제거(대소문자 무시)
            continue
        seen.add(key)
        out.append(s)
    return out


if __name__ == "__main__":
    import sys
    from extract_methods import methods_from_path
    m = methods_from_path(sys.argv[1]) if len(sys.argv) > 1 else ""
    cands = extract_candidates(m or "")
    print(f"후보 {len(cands)}개:\n")
    print(", ".join(cands))
