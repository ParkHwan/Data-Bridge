# 3-party review log — v0.2.1 retrieval changes + CI/CD pipeline (2026-07-23)

Independent reviews of PR [#3](https://github.com/ParkHwan/Data-Bridge/pull/3) (pg_trgm
trigram RRF source, merged), PR [#4](https://github.com/ParkHwan/Data-Bridge/pull/4)
(Korean golden items, merged), and PR [#5](https://github.com/ParkHwan/Data-Bridge/pull/5)
(Cloud Build CI/CD, open at review time) by three agents: **Claude** (Claude Code),
**Antigravity**, **Codex**. Continues the project's review-log practice
(see [architecture.md](architecture.md) §3.1 review notes).

Verdict: PR #3/#4 — no merge-blocking defects (all three parties). PR #5 — merge after
the high-severity items below.

## Findings and dispositions

| # | Source | Sev | Finding | Verification | Disposition |
|---|---|---|---|---|---|
| 1 | Claude | High | `gates` step: `sh -c` without `set -e` swallows ruff/mypy failures (last-command exit code) | Confirmed; reproduced sh semantics | **Fixed** (`d9ad4f3`) |
| 2 | Claude | High | First automated deploy ships trigram-querying code against a prod DB that predates `pg_trgm`; nothing applies schema on deploy → every hybrid search fails | Confirmed: no `ensure_schema()` in server startup path; prod revision predates v0.2.1 | **Fixed in PR #5**: `databridge-migrate` Cloud Run job + pre-deploy `migrate` step (see #3) |
| 3 | Antigravity | High | Don't fix #2 by calling `ensure_schema()` at FastAPI startup — concurrent Cloud Run instances would race on `CREATE EXTENSION`/`CREATE INDEX`; run schema migration as a one-shot pre-deploy task | Premise about existing startup call inaccurate (no such call exists), but the mitigation design is correct and adopted | **Adopted**: schema-before-code via `scripts/migrate.py` job, executed by the pipeline before `deploy` |
| 4 | Codex | High | Gate tests the `uv.lock` graph but the Dockerfile re-resolves deps from `pyproject.toml` (lock not copied) — tested versions ≠ shipped versions, non-reproducible images | Confirmed: `Dockerfile` copied only `pyproject.toml` | **Fixed in PR #5**: `COPY uv.lock` + `uv sync --locked --no-dev` |
| 5 | Antigravity + Codex | High/Med | No `serviceAccount:` in cloudbuild.yaml → default build SA; project's default compute SA holds `roles/editor`; trigger exists only as console state | Confirmed (no triggers existed as of 2026-07-23; compute SA has editor) | **Fixed in PR #5**: dedicated `databridge-build@` SA (`artifactregistry.writer`, `run.developer`, `logging.logWriter`, scoped `iam.serviceAccountUser`), `serviceAccount:` pinned in YAML, trigger + SA defined in `scripts/setup_cicd.sh`. Removing `roles/editor` from the compute SA: **owner decision, open** |
| 6 | Codex | Med | CI never exercises the new SQL paths (no DB → integration tests all skip); pg_trgm/schema/RRF/josa regressions would auto-deploy | Confirmed | **Fixed in PR #5**: throwaway `pgvector/pgvector:pg16` on the `cloudbuild` network; gates run the 8 store integration tests |
| 7 | Codex | Med | Mutable tags everywhere; deploy trusts the just-pushed tag rather than a digest | Confirmed | **Partially fixed in PR #5**: deploy resolves and uses the `@sha256` digest. Builder/base image digest pinning + Artifact Registry immutable-tag mode: **backlog** |
| 8 | Codex | Med | FTS/trigram candidate queries sort by a single score; tie order is planner-dependent, and the physical row order becomes the RRF rank → candidates/citations can vary run-to-run at the `candidate_k` boundary | Confirmed (single-key `ORDER BY` in all three candidate queries) | **Fixed** ([PR #6](https://github.com/ParkHwan/Data-Bridge/pull/6)): deterministic secondary sort keys `space_key, chunk_id` |
| 9 | Antigravity | Med | FTS/trigram queries compute `embedding <=> query` for every text-matched row — CPU/I/O cost at corpus scale; two-stage fetch would defer distance to fused top-k | Confirmed (SELECT-list distance in both queries) | **Backlog** — negligible at demo corpus size; revisit when the corpus grows |
| 10 | Antigravity | Low | Ref-marker removal can leave a stray space before a Korean josa (`"단어 [1]는"` → `"단어 는"`); current tidy rules only cover space runs and punctuation | Confirmed as an edge case; markers usually precede punctuation, which is handled | **Backlog** — targeted fix via removal-span adjacency |
| 11 | Codex | Low | DG-006/007 golden items verify answer/citation, not that the trigram source specifically fired; vector-only recall would also pass | Confirmed; unit test `test_hybrid_korean_josa_recall_via_trigram` covers attribution but only runs with a DB | **Absorbed by #6** (integration tests now run in CI) |

## Cleared by review (no defects found)

- **SQL injection** (Codex): all inputs bind-parameterized; dynamic SQL limited to the
  fixed `space_filter` string; hostile inputs (`'`, `;`, `%(space)s`, `%`) executed safely.
- **Index usage** (Codex): `query <% content` plans as a Bitmap Index Scan on
  `chunks_content_trgm_idx`.
- **RRF fusion math and (space_key, chunk_id) dedup** (Codex): correct.
- **D-3 portability** (Codex): `pg_trgm` officially supported on Cloud SQL and AlloyDB.
- **Citation contract** (Codex): Korean chunks preserve `source_id`/`heading`/`breadcrumb`;
  the trigram path reuses the same `SearchHit` shape.
- **Live verification** (Claude): golden gate `keyword_hit=1.000, source_hit=7/7`
  including Korean items; store-level pure trigram recovery
  (`fts_rank=None, trgm_rank=1` for a bare-form query against josa-suffixed content).

## Closure (2026-07-23)

1. **Done** — `roles/editor` removed from the default compute SA (Codex #5); impact
   analysis first confirmed nothing uses it (all Cloud Run workloads run as
   `databridge-run@`, no GCE/Functions).
2. **Done** — Artifact Registry immutable-tag mode enabled on
   `cloud-run-source-deploy` (Codex #7). Consequence: re-pushing the same
   `:$COMMIT_SHA` tag fails, so a manual re-run of an already-built commit stops at
   `push` — the normal per-commit flow is unaffected.
3. **Done** — josa-space tidy rule (#10): ref-marker removal now absorbs the space
   before a marker glued to a following Hangul josa (`"단어 [1]는"` → `"단어는"`),
   implemented via merged-span adjacency in `_remove_ref_markers` with 3 new unit
   tests.
4. **Done** — builder/base images pinned to `@sha256` digests in `Dockerfile` and
   `cloudbuild.yaml` (#7 residual). Refreshing a pin is a deliberate, reviewable diff.
5. **Deferred by design** — two-stage distance computation (#9): at the demo corpus
   size the per-row cost is negligible and the extra round trip + complexity isn't
   warranted; revisit when the corpus grows past the point where the text-candidate
   scans dominate (`EXPLAIN ANALYZE` on `search_hybrid` is the trigger).
