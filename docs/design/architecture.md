# Data Bridge — Architecture Design (v0.1 Draft)

> Status: Draft for 3-party review (2026-07-06).
> Target: Google Cloud Gen AI Academy APAC — hackathon prototype submission.
> Origin: architectural sibling of a private RAG assistant project (same author). Purpose
> and core logic are shared; **every runtime component is Google Cloud native**.

## 1. Vision (from repo description)

Data Bridge is an **AI team** that connects scattered enterprise documents and data to
support real-world decision making. It ingests Confluence, Google Drive, PDF, meeting
notes, and BigQuery data, and produces proposal drafts, meeting action items, and
cost/progress risk reports — **every output cites its source documents and data**.

Key product principles:

1. **Grounded or nothing** — answers and generated reports must carry citations
   (document id + heading, or SQL + table). No citation, no claim.
2. **An AI team, not a chatbot** — specialized agents (knowledge, data, report) that
   collaborate, orchestrated by a root agent.
3. **Preprocessing quality drives answer quality** — document hierarchy
   (breadcrumbs), section-aware chunking, and per-source isolation are first-class
   (empirically validated in the sibling project: hierarchy injection improved
   keyword-hit quality by +13.3pt on a 15-question golden set).

## 2. Track alignment (Gen AI Academy APAC)

| Track (1st edition naming) | How Data Bridge aligns |
|---|---|
| **1. AI agents — Gemini + ADK + Cloud Run** | Root orchestrator + specialist agents built on **ADK**, Gemini on **Vertex AI**, deployed on **Cloud Run** |
| **2. MCP — agents ↔ real-world data/tools** | Retrieval & BigQuery tools exposed as **MCP servers** so any MCP client can reuse them (stretch goal, Phase 3) |
| **3. AI-ready databases — AlloyDB** | Embeddings + chunks + metadata live in **AlloyDB for PostgreSQL (pgvector)**; hybrid search (vector + keyword + metadata filter) |

> 2nd-edition track naming may differ (Data / AI-ML / Serverless …) — the same stack
> maps onto Data (BigQuery, AlloyDB), AI/ML (Vertex AI, Gemini), Serverless (Cloud Run).

## 3. Architecture

```
                ┌─────────────────────────  Google Cloud  ─────────────────────────┐
 Confluence ──▶ │ Ingest (Cloud Run job)                                            │
 Drive/PDF  ──▶ │   parse → Markdown + frontmatter (hierarchy breadcrumb)           │
                │   → GCS (raw + markdown)                                          │
                │   → chunk + embed (Vertex AI text-embedding)                      │
                │   → AlloyDB (pgvector: chunks, embeddings, metadata)              │
                │                                                                   │
 BigQuery  ───▶ │ (queried live by Data Agent — NL→SQL with schema grounding)       │
                │                                                                   │
                │ Agent service (Cloud Run, ADK + Gemini on Vertex AI)              │
                │   Root Orchestrator                                               │
                │    ├─ Knowledge Agent … hybrid search on AlloyDB, cite doc+section │
                │    ├─ Data Agent      … BigQuery NL2SQL, cite SQL + rows          │
                │    └─ Report Agents   … proposal draft / action items / risk      │
                │         (compose from Knowledge+Data outputs, citations carry over)│
                │                                                                   │
                │ Web UI (Cloud Run) — chat + report view with citation panel       │
                └───────────────────────────────────────────────────────────────────┘
```

### 3.1 Component decisions

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D-1 | LLM | **Gemini (2.x) on Vertex AI** | Hackathon requirement. Agent/tool-calling quality sufficient; sibling project's provider abstraction proves the agent loop is model-portable. |
| D-2 | Agent framework | **ADK (Agent Development Kit)** | Flagship track technology; native multi-agent (root + sub-agents) matches the "AI team" vision. Rejected: porting the sibling's custom agent loop (no track alignment, no multi-agent primitives). |
| D-3 | Vector store | **AlloyDB for PostgreSQL + pgvector** | Named track technology. Replaces the sibling's Milvus + source-prefix isolation with plain `WHERE space_key = …`. Rejected: Vertex AI Vector Search (no relational metadata joins, higher idle cost for a hackathon). **Portability profile (Codex P1)**: the app owns embedding generation (calls Vertex AI itself) and uses only **plain pgvector columns/operators** — no AlloyDB-side AI functions (`embedding()`), no ScaNN-specific features. This keeps Cloud SQL pg a true one-connection-string fallback; AlloyDB-enhanced features are opt-in later, never load-bearing. |
| D-4 | Embeddings | **Vertex AI `gemini-embedding-001`** (multilingual) | Korean+English corpus; single-vendor requirement. Dimension pinned in schema migration. |
| D-5 | Document storage | **GCS** (raw JSON + processed Markdown) | Same Bronze/Silver layering as the sibling project — already GCP-native there. |
| D-6 | Ingest sources (MVP order) | Confluence → PDF/Drive → meeting notes | Confluence pipeline is proven (incl. **hierarchy breadcrumb injection** — port of the validated design). Drive/PDF reuse the same Markdown+frontmatter contract. |
| D-7 | Structured data | **BigQuery** queried live (no copy into AlloyDB) | Data Agent does schema-grounded NL2SQL. **Guardrails (Codex P2)**: allowlisted datasets/tables, dry-run cost estimate + `maximum_bytes_billed`, enforced `LIMIT`, read-only service account, validate-before-execute loop. Results cited as SQL + result sample. |
| D-11 | Search scope (MVP) | **Vector similarity + metadata filter** only | Hybrid FTS+vector fusion (RRF) is a tuning time-sink for a hackathon (Antigravity P1). MVP ships vector+filter; an RRF SQL template is kept in the repo as a Phase-3 option, not on the critical path. |
| D-12 | Citation contract | **Normalized citation object, defined in Phase 1** | One schema across doc chunks (`source_id`+heading), BigQuery results (SQL+sample), and report sections (carried-over citations) — shared by all agents and the UI (Codex P2: citation is a contract, not a UI feature). |
| D-8 | Deploy | **Cloud Run** ×3 (ingest job / agent API / web UI) | Serverless track alignment; scale-to-zero fits demo budget. |
| D-9 | Language | **All repo content & demo in English** | Submission requirement. |
| D-10 | Demo corpus | Public/sample content only (e.g., a demo Atlassian space + public sample PDFs + BigQuery public datasets) | **No internal/employer data in this repo — hard rule.** The sibling project's internal corpus (customer names, org structure) must never be ingested here. |

### 3.2 What is ported vs newly built

| Ported from sibling (logic proven) | Newly built (hackathon scope) |
|---|---|
| Confluence extract → Markdown + frontmatter (incl. ancestors breadcrumb) | ADK multi-agent orchestration (root + 3 specialists) |
| Section-aware chunking policy + per-source isolation concept | AlloyDB schema + hybrid search function |
| Grounded-answer contract (citations mandatory) | BigQuery Data Agent (NL2SQL + cost guard) |
| Golden-set evaluation method (questions.yaml → keyword-hit / source-overlap) | Report Agents (proposal / action items / risk) |
| | Web UI + citation panel |

Porting is **re-implementation against the same contracts**, not code copy — the sibling
repo stays private and its code is not vendored here.

## 4. Phased scope (fits an unknown deadline)

- **Phase 1 — Grounded Knowledge MVP (must-have)**: Confluence ingest → GCS → AlloyDB
  → Knowledge Agent with citations → Cloud Run API + minimal UI. Even in Phase 1 the
  agent runs **under a minimal ADK Root Orchestrator shell** (one sub-agent registered)
  so the multi-agent architecture is real from day one — a Phase-1-only demo still
  shows an orchestrated team, not a bare RAG chatbot (Codex P1 / Antigravity P2
  convergent). Includes: **citation contract (D-12)** + **5-question mini golden set**
  (regression proof from the first demo; Codex P2).
- **Phase 2 — The AI team (core differentiator)**: Data Agent (BigQuery) + routing +
  one Report Agent (action items). Full golden-set eval ported (quality numbers in the
  demo video are a differentiator).
- **Phase 3 — Stretch (post-demo unless deadline allows)**: remaining Report Agents,
  Drive/PDF ingest, hierarchy-aware re-ranking, RRF hybrid search. **MCP exposure is
  a post-demo extension** — it must not compete with the core demo (Codex P2).

Each phase leaves the system demoable; the 3-minute video is recorded at whatever
phase the deadline allows.

### 4.1 Development practice notes

- **Fixture-driven UI dev mode** (Antigravity P1, scoped): the UI may be developed
  against fixture responses for parallelism. **The submission demo video must show the
  real system end-to-end** — a mocked demo would misrepresent a "functional prototype"
  and is ruled out (integrity + hackathon rule compliance). Fixture mode is a dev
  tool, clearly flagged in the UI, never enabled in the recorded demo.
- **ADK contracts stay framework-agnostic** (Codex P2): citations-mandatory,
  tool-result schemas, and golden eval are implemented as plain tests independent of
  ADK, so the proven behavior survives any framework swap.
- **ADK bail-out timebox** (Antigravity P2): if ADK multi-agent wiring is not working
  within ~2 focused dev days, fall back to a thin custom orchestrator over the Gemini
  SDK (contracts above make the swap cheap). Track alignment prefers ADK; shipping
  beats alignment.

## 5. Risks

- **R-1 AlloyDB cost/quota** — hackathon credits may not cover an AlloyDB cluster
  long-term. Mitigation: develop against Cloud SQL pg/pgvector locally-priced tier or
  AlloyDB free trial; identical pgvector API keeps the swap one connection string.
- **R-2 ADK learning curve** — new framework. Mitigation: Phase 1 uses a single agent
  (smallest ADK surface); multi-agent only in Phase 2.
- **R-3 NL2SQL correctness** — mitigated by schema-grounded prompting, dry-run cost
  guard, and mandatory SQL citation (judge can verify).
- **R-4 Demo data licensing** — use BigQuery public datasets and self-authored demo
  docs only (D-10). **Physical guard (Antigravity P1)**: repo `.gitignore` blocks
  `.env`, `*questions*.yaml`, `*snapshots*`, `*.crowdy*` patterns from day one, and
  `CONTRIBUTING.md` states the two-project isolation rule (no file copied from the
  sibling repo's data/corpus paths) — the rule is enforced by tooling, not memory.
- **R-5 Submission rules unknowns** — repo-public obligation, pre-existing-project
  policy, 2nd-edition track names: **owner to confirm on the event FAQ before
  submission** (design unaffected; positioning text may need adjustment).

## 6. Review log (3-party)

**Pre-review 2026-07-06 — applied:**
- Codex P1×2: D-3 fallback needs a portability profile (plain pgvector only, app-owned
  embeddings) / Phase 1 alone reads as a RAG chatbot → minimal ADK orchestrator shell
  moved into Phase 1. P2 applied: citation contract to Phase 1 (D-12) / mini golden set
  in Phase 1 / BigQuery guardrail list (D-7) / MCP demoted to post-demo /
  framework-agnostic contracts (§4.1).
- Antigravity P1: hybrid search scope cut to vector+filter MVP (D-11) / `.gitignore` +
  CONTRIBUTING physical guard (R-4). P2: ADK bail-out timebox (§4.1).
- **Rejected with reason**: Antigravity's "Scenario Mock Mode for the demo video" —
  a mocked demo would misrepresent a functional prototype (hackathon rules require the
  project to operate as described). Reframed as a dev-only fixture mode, never in the
  recorded demo (§4.1).
