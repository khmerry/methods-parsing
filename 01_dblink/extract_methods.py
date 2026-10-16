# -*- coding: utf-8 -*-
"""
1단계: 전체 논문 텍스트에서 'Methods(실험) 섹션'만 잘라낸다.

접근
----
- 'Methods / Materials and Methods / Experimental Section …' 같은 제목이
  '거의 단독 줄'로 나오는 지점을 시작점으로 잡는다.
- 그 뒤 처음 나오는 '종료 제목'(References, Data availability, Acknowledgements,
  Results, Discussion …) 직전까지를 Methods 본문으로 본다.
- PDF를 직접 받으면 pypdf로 텍스트를 먼저 뽑는다(설치돼 있을 때).

한계: PDF 추출 텍스트는 제목이 깨질 수 있어 100% 정확하진 않다.
      실패 시 None을 돌려주며, 그때는 수동 지정/보정이 필요하다.
"""

import re
import os

# 시작(Methods) 제목 후보 — 긴 표현을 먼저 둬서 우선 매칭
_METHODS = (r"materials\s+and\s+methods|methods\s+and\s+materials|"
            r"experimental\s+section|experimental\s+procedures|"
            r"experimental\s+methods|experimental|methods")

# 종료 제목 후보 — Methods가 끝났다고 볼 수 있는 다음 섹션들
_STOP = (r"results(?:\s+and\s+discussion)?|discussion|conclusions?|"
         r"acknowledge?ments?|references|data\s+availability|"
         r"author\s+contributions?|competing\s+interests?|online\s+content|"
         r"additional\s+information|author\s+information|bibliography")

# '단독 줄 제목' 패턴: 줄 시작 + (선택적 번호) + 제목 + 줄 끝
_start_re = re.compile(r"(?im)^[ \t]*(?:\d+[\.\)]?\s*)?(" + _METHODS + r")[ \t]*:?[ \t]*$")
_stop_re  = re.compile(r"(?im)^[ \t]*(?:\d+[\.\)]?\s*)?(" + _STOP + r")[ \t]*:?[ \t]*$")


def find_methods(full_text):
    """전체 텍스트에서 Methods 본문 문자열을 반환. 못 찾으면 None."""
    starts = [m.start() for m in _start_re.finditer(full_text)]
    if not starts:
        return None
    start = starts[0]                      # 첫 번째 단독-줄 Methods 제목
    # 시작점 이후의 첫 종료 제목을 끝으로
    ends = [m.start() for m in _stop_re.finditer(full_text) if m.start() > start]
    end = min(ends) if ends else len(full_text)
    return full_text[start:end].strip()


def read_pdf_text(pdf_path):
    """PDF -> 전체 텍스트 (pypdf 필요: pip install pypdf)."""
    from pypdf import PdfReader          # 필요할 때만 import
    reader = PdfReader(pdf_path)
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def methods_from_path(path):
    """.txt 또는 .pdf 경로를 받아 Methods 본문을 반환."""
    if path.lower().endswith(".pdf"):
        text = read_pdf_text(path)
    else:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    return find_methods(text)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        m = methods_from_path(sys.argv[1])
        print("찾음:", bool(m))
        if m:
            print("길이:", len(m), "글자\n----- 앞부분 -----")
            print(m[:400])
