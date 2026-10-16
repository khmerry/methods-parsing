# 01_dblink — 외부 DB 매칭

추출된 표현(surface)을 외부 데이터베이스에 매칭해 **정규화 ID**를 붙이고, 매칭되면 **`established`** 로 표시하는 단계.
파이프라인상 **고정밀·결정론 패스**이며, 여기서 안 잡힌 것은 이후 LLM 단계로 넘어간다.

## 중요: API 키는 전부 불필요
아래 4개 DB 모두 **인증/키 없이** 공개 접근 가능. 당신이 따로 발급받아야 할 API 키는 **없음**.

| 카테고리 | DB | 엔드포인트 | 키 | 레이트리밋/예절 |
|---|---|---|---|---|
| ingredient | **PubChem** PUG-REST | `https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/<name>/cids/TXT` | 불필요 | **≤ 5 req/s**, 호출당 ≤30s |
| method | **CHMO** (OLS4 경유) | `https://www.ebi.ac.uk/ols4/api/search?q=<term>&ontology=chmo` | 불필요 | 과도호출 자제 |
| equipment | **OBI** (OLS4 경유) | `…&ontology=obi` | 불필요 | 과도호출 자제 |
| ingredient 폴백 / equipment 폴백 / vendor | **Wikidata** | `wbsearchentities`(퍼지) + `wbgetclaims`(교차 ID) | 불필요 | **User-Agent 필수**, 429/Retry-After 준수, 간격 ≥1.1s |

> 선택(키 필요): **BioPortal**(온톨로지 포털, 키 필요), **Matscholar**(재료 NER, 키/상태 불확실) — v1에선 불필요.

## 커버리지 실측 (Kim 2022 재료/방법/장비로 검증)
- **PubChem (ingredient): 우수.** 무기염 포함 11/12 매칭(CsBr→24592, LiF→224478, PbBr₂→139549 …). 단 *methylammonium bromide* 는 이름 변형 때문에 MISS → 동의어/OPSIN 보강 여지.
- **CHMO (method): 양호.** XPS→CHMO:0000404, TEM→CHMO:0000080 등. 단 검색이 **랭킹식(퍼지)** 이라 PL 같은 상위어는 자식 용어로 매칭될 수 있음 → 필요 시 `exact=True`.
- **OBI (equipment): 듬성듬성.** `mass spectrometer`는 OBI:0000049로 잡히나 `scanning electron microscope`, `photoelectron spectrometer`는 미매칭. → **장비는 외부 DB보다 vendor/model 패턴(상용품)으로 잡는 게 주력**(별도 모듈에서).
- **ChEBI(무기물): 부적합.** CsBr/LiF 모두 미등록(생물학적 관심 화학물질 위주). → 무기재료는 PubChem 사용, ChEBI는 제외.
- **Wikidata (범용, v2): 폴백·교차ID 허브로 활용.**
  - **ingredient 폴백**: PubChem이 놓친 *methylammonium bromide* 를 Wikidata가 잡고 **PubChem CID 13000536 + CAS까지 역수확**(교차 ID 허브).
  - **vendor 타입 필터**: 설명(description) 키워드로 동음이의 제거 — 'Kratos'(아이돌 앨범)·'Gatan'(성씨) 같은 오탐을 거르고 회사 아이템만 채택. Hamamatsu/Sigma-Aldrich OK. 단 **Gatan은 미매칭**(설명에 키워드 없음/순위 밖), 같은 이름 회사 여럿이면 정확 자회사까진 못 집음 → **seed 사전 + Wikidata 검증 병행** 권장.

## "Application(용도)" 추출용 DB — 결론: 없음
재료/소자의 application(용도)에 대응하는 **깔끔한 외부 통제어휘·온톨로지는 존재하지 않음.**
application은 개방형 구(phrase)라 사전 매칭이 불가. (EDAM은 생물정보학 전용으로 부적합.)
→ **dblink 대상이 아니라 LLM/코퍼스 클러스터링 대상.** Matscholar도 APL을 사전매칭이 아닌 추출+클러스터링으로 처리함.

## 파일
- `dblink.py` — 매칭 라이브러리(표준 라이브러리만, 캐시·레이트리밋·429/503 백오프)
- `extract_methods.py` — (1단계) 전체 논문 → Methods 섹션 텍스트
- `extract_candidates.py` — (2단계) Methods → 대문자 명사/명사구 후보
- `pipeline.py` — (3단계) 1→2→3 통합 + DB 역분류(DB-as-classifier)
- `demo.ipynb` — dblink 매칭 예제
- `_dblink_cache.json` — 자동 생성 질의 캐시

## 전체 파이프라인 (`python pipeline.py <논문.txt|.pdf>`)
1. **Methods 추출**: 'Methods/Experimental…' 단독 줄 제목 → 다음 종료 제목 직전까지.
2. **후보 추출(Methods만)**: 대문자 시작 연속토큰 + 전부대문자 약어 + 화학식.
3. **DB 역분류**: PubChem→ingredient / CHMO→method / OBI→equipment.
   - **약어는 '풀네임'으로 치환해 질의**(핵심). 풀네임은 **Schwartz–Hearst 알고리즘**으로
     본문 전체에서 추출(LLM 불필요). 엔티티 추출은 Methods만, 약어사전만 본문 전체 사용.
   - 화학식(MoO3 등)은 그대로 질의. 사전에 없는 '맨 약어'는 모호 → 건너뜀(LLM 단계로).

### 왜 '약어 풀네임 치환'이 필수인가 (실측 교훈)
짧은 약어를 PubChem에 그대로 던지면 동명의 **엉뚱한 화합물**에 매칭된다
(BPA→Bisphenol A, XPS·TEM→임의 화합물; PubChem 1.19억 화합물). Schwartz–Hearst로
풀네임 치환 후: **BPA→benzylphosphonic acid(CID 81312), XPS→CHMO:0000404(method)** 로 정상화.
→ 이는 Matscholar가 **분류(문맥)와 정규화(DB)를 분리**한 것과 같은 원리.

### Kim 2022 실측 (25개 매칭)
- **정답 21/25 (~84%)**: 화학물질 14(FABr·GABr·CsBr·BPA·CB·THF·MoO3·PbBr2·LiF·Al·N2·O2·H2O·Au)
  + 분석법 7(XPS·UPS·TEM·EELS·EDS·HAADF·STEM, 전부 CHMO로 정확 분류).
- **오탐 4**: Perovskite(광물)·Cambridge(지명)·Apara(변수명)·FA0(식 조각)
  — 전부 '일반 대문자 단어를 PubChem에 blind 조회'해서 생김.
  → **근본 해결은 '카테고리 먼저(LLM/문맥) → DB는 해당 카테고리로만 정규화'**(아키텍처 B).
- **재현율 공백(의도된/예상된)**: MABr(PubChem 미등록→Wikidata 폴백 필요),
  DMSO/TPBi/ZADN(본문 정의 없음→맨 약어 스킵), 장비 모델명·vendor(OBI 미약→vendor 패턴/LLM).

## 사용법
```python
from dblink import match_entity, match_vendor

match_entity("benzylphosphonic acid", "ingredient")
# -> {'normalized_ids':[{'db':'PubChem','id':'CID 81312'}], 'provenance':'established', ...}

match_entity("methylammonium bromide", "ingredient")   # PubChem MISS -> Wikidata 폴백
# -> normalized_ids: Wikidata Q65059102 + PubChem CID 13000536 + CAS 6876-37-5

match_vendor("Hamamatsu Photonics")   # -> {'qid':'Q5644023', 'matched':True, ...}
```
또는 터미널에서 `python dblink.py` (자체 테스트 실행).

### 매칭 로직(카테고리별)
- ingredient: **PubChem** → 없으면 **Wikidata(화학 타입 필터) 폴백 + 교차 ID(PubChem/ChEBI/CAS) 수확**
- method: **CHMO → OBI** (Wikidata 폴백은 노이즈↑라 기본 OFF, `WIKIDATA_FALLBACK_METHOD`)
- equipment: **OBI → CHMO** → 없으면 **Wikidata(장비 타입 필터) 폴백**
- vendor: **Wikidata** `wbsearchentities` + 회사 타입 필터
- 캐시(`_dblink_cache.json`)·레이트리밋·429 백오프 내장.

## 다음 단계(메모)
1. 장비 commercial 패턴 추출기(`(모델, 벤더)` / `Vendor Model`) — 외부 DB 밖, 별도 모듈.
2. vendor 퍼지 매칭(별칭·instance of manufacturer) 보강.
3. 이름 변형 보강: PubChem 동의어, OPSIN(체계명→구조).
4. 코퍼스 단계: canonical_key 1차 병합 → 임베딩 클러스터링(정규화·신규유형).

## 출처
- PubChem 프로그래밍 접근: https://pubchem.ncbi.nlm.nih.gov/docs/programmatic-access
- OLS4 (EMBL-EBI): https://www.ebi.ac.uk/ols4
- ChEBI API: https://www.ebi.ac.uk/chebi/tools
- Wikidata Query Service (User-Agent/limits는 WDQS User Manual 참조)
