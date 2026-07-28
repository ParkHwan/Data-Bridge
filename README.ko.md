# Data Bridge — 근거를 제시하는 AI 팀

> 흩어진 기업 문서와 데이터를 연결해 실무 의사결정을 지원하는 **멀티 에이전트 AI 팀**.
> 모든 답변과 리포트에는 **근거(인용)** 가 함께 제공됩니다 — *grounded or nothing.*
>
> 🇬🇧 English: [README.md](README.md) (제출 기본 문서)
> 🔗 라이브 데모: https://databridge-227172390736.us-central1.run.app

## 무엇이 다른가

| 원칙 | 구현 |
|---|---|
| **Grounded or nothing** | 인용 없는 주장은 반환 자체가 거부됩니다. 문서 답변은 모델이 실제 사용한 근거 청크(`SOURCES` 마커)만 인용하고, 데이터 답변은 **실행된 SQL 원문**이 인용으로 붙습니다. |
| **챗봇이 아니라 AI 팀** | Root Orchestrator 가 질문을 분류해 전문 에이전트(Knowledge / Data / Report)에 위임합니다. 협업 과정(어떤 에이전트가 어떤 툴을 썼는지)이 UI에 그대로 표시됩니다. |
| **전처리 품질 = 답변 품질** | 문서 계층(breadcrumb)·섹션 경계를 보존한 청킹. 인용의 `제목 › 섹션 › 경로`가 실제 문서에서 검증 가능합니다. |

## 아키텍처 (Google Cloud 네이티브)

```
 Confluence/PDF ─▶ Ingest (Cloud Run job)
                     parse → Markdown+frontmatter(계층 breadcrumb)
                     → chunk → embed (Vertex AI gemini-embedding-001, 768d)
                     → Cloud SQL for PostgreSQL + pgvector  ※ plain pgvector 프로파일
                                                              → AlloyDB 와 연결문자열 교체만으로 호환
 BigQuery ────────▶ (Data Agent 가 라이브 조회 — 복사 없음)

 Agent 서비스 (Cloud Run, ADK + Gemini 2.5 Flash on Vertex AI)
   databridge_root ─┬─ knowledge_agent : pgvector 검색, 문서 인용
                    ├─ data_agent      : BigQuery NL2SQL (가드레일 포함), SQL 인용
                    └─ report_agent    : 액션 아이템 등 실무 문서 생성, 인용 승계

 데모 UI (동일 Cloud Run) — 답변 + 인용 패널 + 팀 활동 피드
```

### Data Agent 가드레일 (전부 코드로 강제)

- 단일 `SELECT` 만 허용 (DML/DDL 정적 차단)
- dry-run 으로 참조 테이블을 확인해 **allowlist 데이터셋** 밖이면 거부
- `maximum_bytes_billed` 200MB 비용 상한 + 결과 행 수 제한
- 읽기 전용 서비스 계정

## 빠른 시작 (로컬)

```bash
# 1) 로컬 pgvector + 의존성
docker compose up -d
uv pip install -e ".[server,gcp,dev]"

# 2) 샘플 코퍼스 인제스트
#    DATABRIDGE_EMBEDDER 는 필수이며 기본값이 없다. 3단계와 같은 값을 써야
#    인제스트와 질의가 같은 벡터 공간에서 동작한다.
DATABRIDGE_EMBEDDER=hashed uv run python scripts/ingest_samples.py

# 3) 서버. 해시 임베더는 임베딩만 로컬에서 처리하고, 에이전트는 어느 쪽이든
#    Gemini 를 호출하므로 Google 모델 자격증명이 필요하다.
DATABRIDGE_EMBEDDER=hashed GOOGLE_GENAI_USE_VERTEXAI=TRUE GOOGLE_CLOUD_PROJECT=<프로젝트> \
  uv run uvicorn databridge.server.app:app --port 8080
# → http://localhost:8080

# 3') Vertex 로 서빙하려면(ADC 필요) 2단계도 vertex 로 다시 인제스트한다
DATABRIDGE_EMBEDDER=vertex uv run python scripts/ingest_samples.py
DATABRIDGE_EMBEDDER=vertex GOOGLE_GENAI_USE_VERTEXAI=TRUE GOOGLE_CLOUD_PROJECT=<프로젝트> \
  uv run uvicorn databridge.server.app:app --port 8080
```

품질 게이트: `uv run pytest -q` / `uv run ruff check .` / `uv run mypy`
(테스트 개수는 로컬 DB 기동 여부에 따라 달라진다 — DB 가 없으면 통합 테스트가 skip 된다)

## 평가 (골든셋)

자작 데모 코퍼스 11문항으로 세 전문 에이전트와 거절 경로를 모두 검증한다: 지식 7(영어 5 +
트라이그램 recall을 검증하는 한국어 2), 데이터 2(BigQuery NL2SQL), 리포트 1, 거절 1. 평가기는
**관측 가능한 계약**(최종 에이전트·툴 순서·인용 종류·키워드 임계·거절)을 검사하며, 모든 항목이
통과해야만 게이트가 green이다(`PASS`/`FAIL`/`REFUSAL_OK`/`ERROR`). 최근 owner 실행: **10/11**이며
실패는 `DG-009` 다 — 기대값이 `bigquery-public-data.thelook_ecommerce` 에 대해 상수로 박혀 있는데
이 데이터셋은 고정 스냅샷이 아니어서, 에이전트 답이 맞고 박아둔 값이 낡았다.

**인제스트와 질의는 같은 임베더로 실행해야 한다.** `DATABRIDGE_EMBEDDER` 는 필수이며
`hashed` 또는 `vertex` 를 받고 기본값이 없다. 둘 다 768차원이라 이 설정은 새 불일치를 막을 뿐,
이미 저장된 인덱스가 어느 임베더로 만들어졌는지는 아직 증명하지 못한다. 과거에 보고된
`DG-004` 불안정성은 불일치 상태에서 측정된 것이었고, 임베더를 맞춰 재인제스트한 뒤에는
10회 중 10회 정상 응답·인용했다. 10회로는 실패율 상한이 넓으므로 안정성 인증은 아니다.

골든 파일은 대상 스페이스를 선언하고 `--space` 는 그 값을 덮어쓰지 않고 검증하므로,
스페이스 키 불일치는 첫 질문 전에 차단된다. 저장된 인덱스의 임베더 provenance 강제는
후속 스키마 마이그레이션에서 다룬다.

```bash
# 인덱스를 만든 인제스트와 같은 임베더를 쓴다 (위와 같은 이유)
DATABRIDGE_EMBEDDER=vertex GOOGLE_CLOUD_PROJECT=<프로젝트> \
  uv run python scripts/run_golden.py
```

평가기 자체(`src/databridge/evals/`)는 ADK 비의존이며 오프라인 단위 테스트가 있다.
[v0.2.3](docs/releases/v0.2.3.md) 참조.

## GCP 스택

| 구성 | 서비스 |
|---|---|
| LLM / 임베딩 | **Vertex AI** — Gemini 2.5 Flash / gemini-embedding-001 |
| 에이전트 프레임워크 | **ADK** (Agent Development Kit) — root + sub-agents |
| 벡터 저장소 | **Cloud SQL for PostgreSQL + pgvector** (plain 프로파일 — **AlloyDB** 호환) |
| 정형 데이터 | **BigQuery** (공개 데이터셋 `thelook_ecommerce`) |
| 배포 | **Cloud Run** (서비스 + migrate/ingest 잡, scale-to-zero) |
| CI/CD | **Cloud Build** — `main` 푸시 시 게이트(스토어 통합 테스트 포함) → 잠긴 의존성 빌드 → 스키마 마이그레이션 → digest 배포 ([cloudbuild.yaml](cloudbuild.yaml), 인프라: [scripts/setup_cicd.sh](scripts/setup_cicd.sh)) |

## 데모 데이터

전부 자작 가상 시나리오(Aurora Insights / Atlas Migration)와 BigQuery 공개
데이터셋만 사용합니다. 실기업 데이터는 포함되지 않습니다 (설계 D-10,
[CONTRIBUTING.md](CONTRIBUTING.md) 참조).

## 설계 문서

의사결정·기각 대안·리뷰 이력: [docs/design/architecture.md](docs/design/architecture.md)
