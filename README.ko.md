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

# 2) 샘플 코퍼스 인제스트 (GCP 없이: 해시 임베더 / Vertex: DATABRIDGE_EMBEDDER=vertex)
uv run python scripts/ingest_samples.py

# 3) 서버 (Vertex AI 사용 시 ADC 필요)
GOOGLE_GENAI_USE_VERTEXAI=TRUE GOOGLE_CLOUD_PROJECT=<프로젝트> \
  uv run uvicorn databridge.server.app:app --port 8080
# → http://localhost:8080
```

품질 게이트: `uv run pytest -q` (36 tests) / `uv run ruff check .` / `uv run mypy`

## 평가 (미니 골든셋)

자작 데모 코퍼스 5문항 — 라이브 Gemini 기준 **keyword_hit 1.000 / source_hit 5/5**:

```bash
GOOGLE_CLOUD_PROJECT=<프로젝트> uv run python scripts/run_golden.py
```

## GCP 스택

| 구성 | 서비스 |
|---|---|
| LLM / 임베딩 | **Vertex AI** — Gemini 2.5 Flash / gemini-embedding-001 |
| 에이전트 프레임워크 | **ADK** (Agent Development Kit) — root + sub-agents |
| 벡터 저장소 | **Cloud SQL for PostgreSQL + pgvector** (plain 프로파일 — **AlloyDB** 호환) |
| 정형 데이터 | **BigQuery** (공개 데이터셋 `thelook_ecommerce`) |
| 배포 | **Cloud Run** (서비스 + ingest job, scale-to-zero) |

## 데모 데이터

전부 자작 가상 시나리오(Aurora Insights / Atlas Migration)와 BigQuery 공개
데이터셋만 사용합니다. 실기업 데이터는 포함되지 않습니다 (설계 D-10,
[CONTRIBUTING.md](CONTRIBUTING.md) 참조).

## 설계 문서

의사결정·기각 대안·리뷰 이력: [docs/design/architecture.md](docs/design/architecture.md)
