# Data Bridge — an AI team that cites its sources

> A **multi-agent AI team** that connects scattered enterprise documents and data to
> support real-world decision making. Every answer and every report carries its
> **evidence** — *grounded or nothing.*
>
> 🇰🇷 한국어: [README.ko.md](README.ko.md)
> 🔗 Live demo: https://databridge-227172390736.us-central1.run.app

## Why it's different

| Principle | Implementation |
|---|---|
| **Grounded or nothing** | An uncited claim is refused, not returned. Document answers cite only the evidence chunks the model actually used (`SOURCES` markers, verified against retrieved evidence); data answers carry the **exact SQL that ran**. |
| **An AI team, not a chatbot** | A Root Orchestrator classifies each request and delegates to specialists (Knowledge / Data / Report). The collaboration — which agent acted, which tool ran — is shown live in the UI. |
| **Preprocessing quality drives answer quality** | Chunking preserves document hierarchy (breadcrumbs) and section boundaries, so every citation's `title › section › path` is verifiable in the source document. |

## Architecture (Google Cloud native)

```
 Confluence/PDF ─▶ Ingest (Cloud Run job)
                     parse → Markdown + frontmatter (hierarchy breadcrumb)
                     → chunk → embed (Vertex AI gemini-embedding-001, 768d)
                     → Cloud SQL for PostgreSQL + pgvector   ※ plain-pgvector profile:
                                                               AlloyDB-compatible via a
                                                               connection-string swap
 BigQuery ────────▶ (queried live by the Data Agent — no copies)

 Agent service (Cloud Run, ADK + Gemini 2.5 Flash on Vertex AI)
   databridge_root ─┬─ knowledge_agent : pgvector search, document citations
                    ├─ data_agent      : BigQuery NL2SQL with guardrails, SQL citations
                    └─ report_agent    : action items & working docs, citations carry over

 Demo UI (same Cloud Run) — answer + citation panel + team activity feed
```

### Data Agent guardrails (all enforced in code, never left to the model)

- Single `SELECT` statements only (DML/DDL statically rejected)
- Dry-run first: referenced tables must be inside **allowlisted datasets**
- `maximum_bytes_billed` 200 MB cost cap + client-side row cap
- Read-only service account

## Quick start (local)

```bash
# 1) local pgvector + dependencies
docker compose up -d
uv pip install -e ".[server,gcp,dev]"

# 2) ingest the sample corpus (no GCP needed: hashed embedder / Vertex: DATABRIDGE_EMBEDDER=vertex)
uv run python scripts/ingest_samples.py

# 3) serve (Vertex AI requires ADC)
GOOGLE_GENAI_USE_VERTEXAI=TRUE GOOGLE_CLOUD_PROJECT=<project> \
  uv run uvicorn databridge.server.app:app --port 8080
# → http://localhost:8080
```

Quality gates: `uv run pytest -q` (49 tests) / `uv run ruff check .` / `uv run mypy`

## Evaluation (mini golden set)

Five questions over the self-authored demo corpus — live Gemini results:
**keyword_hit 1.000 / source_hit 5/5**.

```bash
GOOGLE_CLOUD_PROJECT=<project> uv run python scripts/run_golden.py
```

## GCP stack

| Component | Service |
|---|---|
| LLM / embeddings | **Vertex AI** — Gemini 2.5 Flash / gemini-embedding-001 |
| Agent framework | **ADK** (Agent Development Kit) — root + sub-agents |
| Vector store | **Cloud SQL for PostgreSQL + pgvector** (plain profile — **AlloyDB**-compatible) |
| Structured data | **BigQuery** (public dataset `thelook_ecommerce`) |
| Deploy | **Cloud Run** (service + ingest job, scale-to-zero) |

## Demo data

Everything is self-authored fiction (Aurora Insights / Atlas Migration) plus BigQuery
public datasets. No real company data is included (design D-10, see
[CONTRIBUTING.md](CONTRIBUTING.md)).

## Design doc

Decisions, rejected alternatives, and review history:
[docs/design/architecture.md](docs/design/architecture.md)
