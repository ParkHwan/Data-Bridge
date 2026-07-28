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

The Confluence ingest path is real code as of
[v0.2.4](docs/releases/v0.2.4.md) — a batch Cloud Run job (`src/databridge/confluence/`) that pulls
a folder over the REST v2 API and converts ADF → Markdown. Its live demo runs against a
self-authored space only (D-10); the sample corpus above still drives the local quick start.
[v0.2.5](docs/releases/v0.2.5.md) hardened that path from real operation: an empty page no longer
fails the nightly run, the Cloud SQL DSN moved to Secret Manager, and the serving space now has a
declared default in `scripts/setup_cicd.sh` (the release note documents how to change it).
[v0.2.6](docs/releases/v0.2.6.md) closed four gaps in the quality gate itself — starting with a
target corpus that was implicit and could disagree with production — and
[v0.2.7](docs/releases/v0.2.7.md) made a refusal diagnosable.

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

Quality gates: `uv run pytest -q` / `uv run ruff check .` / `uv run mypy`
(the test count depends on whether the local database is up — integration tests skip without it)

## Evaluation (golden set)

Eleven questions over the self-authored demo corpus, covering all three specialists and the
refusal path: 7 knowledge (5 English + 2 Korean exercising trigram recall), 2 data (BigQuery
NL2SQL), 1 report, 1 refusal. The evaluator checks **observable contracts** — final agent,
tool subsequence, citation kind, exact values, keyword threshold, and refusal — and the gate is
green only when every item passes (`PASS`/`FAIL`/`REFUSAL_OK`/`ERROR`). Latest owner-run:
**10/11**; `DG-004` is unstable (4 of 9 isolated runs passed) and its refusals are diagnosed in
[v0.2.7](docs/releases/v0.2.7.md).

The golden file declares the space it targets, and `--space` asserts that value rather than
overriding it, so a space-key mismatch is blocked before the first question is asked.

```bash
GOOGLE_CLOUD_PROJECT=<project> uv run python scripts/run_golden.py
```

The evaluator itself (`src/databridge/evals/`) is ADK-independent and unit-tested offline; see
[v0.2.3](docs/releases/v0.2.3.md).

## GCP stack

| Component | Service |
|---|---|
| LLM / embeddings | **Vertex AI** — Gemini 2.5 Flash / gemini-embedding-001 |
| Agent framework | **ADK** (Agent Development Kit) — root + sub-agents |
| Vector store | **Cloud SQL for PostgreSQL + pgvector** (plain profile — **AlloyDB**-compatible) |
| Structured data | **BigQuery** (public dataset `thelook_ecommerce`) |
| Deploy | **Cloud Run** (service + migrate/ingest jobs, scale-to-zero) |
| CI/CD | **Cloud Build** trigger on `main` — gates (incl. store integration tests) → locked-deps build → schema migrate → digest deploy ([cloudbuild.yaml](cloudbuild.yaml), infra in [scripts/setup_cicd.sh](scripts/setup_cicd.sh)) |

## Demo data

Everything is self-authored fiction (Aurora Insights / Atlas Migration) plus BigQuery
public datasets. No real company data is included (design D-10, see
[CONTRIBUTING.md](CONTRIBUTING.md)).

## Design doc

Decisions, rejected alternatives, and review history:
[docs/design/architecture.md](docs/design/architecture.md)
