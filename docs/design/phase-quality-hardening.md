# Phase 2.5 — Quality Hardening: RRF Hybrid Search + Claim-Level Citation Verification

> Status: Draft for 3-party review (Claude / Antigravity / Codex), 2026-07-21.
> Scope: the two highest-priority items from the post-submission "2차 개발" idea
> exchange, on the **quality axis** (as opposed to the coverage axis — Drive/PDF
> ingest, extra Report Agents — which is out of scope here).
> Builds on [architecture.md](architecture.md) D-11 (hybrid search deferred) and
> D-12 (citation contract).

## 1. Why these two, together

Both features tighten the same product rule — **grounded or nothing** — from two
different angles:

- **RRF hybrid search** improves what evidence the agent *can* find (recall/precision
  of `search_knowledge`).
- **Claim-level citation verification** improves what the agent is *allowed to keep*
  after it answers (no more free-riding uncited sentences under a single trailing
  citation block).

They are independent enough to implement and test separately, but both touch the
knowledge/report answer path, so the acceptance step re-runs the full test suite (and
ideally the golden set) with both changes applied together, not just each in isolation.

## 2. Feature A — RRF Hybrid Search

### 2.1 Current state (`src/databridge/store/pg.py`)

`PgVectorStore.search()` is pure cosine-distance ANN over `chunks.embedding`, filtered
by `space_key`. No keyword/FTS signal exists. `schema.sql` has no `tsvector` column or
GIN index.

### 2.2 Schema change (additive, idempotent — must not break `ensure_schema()` on an
existing populated database)

```sql
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx ON chunks USING GIN (content_tsv);
```

`'english'` config is a known limitation for the Korean half of a KR/EN corpus (no
Korean stemming in stock Postgres) — call this out explicitly in the module docstring
as a scoped limitation, not a silent gap. Out of scope to fix here (see §4).

### 2.3 New method: `PgVectorStore.search_hybrid(...)`

Signature:

```python
def search_hybrid(
    self,
    query_embedding: list[float],
    query_text: str,
    *,
    space_key: str | None = None,
    top_k: int = 5,
    candidate_k: int = 20,
    rrf_k: int = 60,
) -> list[SearchHit]:
```

Algorithm:

1. **Vector candidates**: same query as `search()`, `LIMIT candidate_k`, ordered by
   cosine distance ascending → rank 1..N.
2. **FTS candidates**: `WHERE content_tsv @@ websearch_to_tsquery('english', %(q)s)`
   (plus the same space filter), ordered by `ts_rank_cd(content_tsv, query) DESC`,
   `LIMIT candidate_k` → rank 1..M. `websearch_to_tsquery` on empty/stopword-only input
   returns an empty tsquery that matches nothing — this is the natural, silent
   degrade-to-vector-only path; do not special-case it.
3. **RRF fusion**: for each chunk_id appearing in either list,
   `score = 1/(rrf_k + vector_rank) + 1/(rrf_k + fts_rank)` (a term is omitted, not
   zero, if the chunk is absent from that list — this is standard RRF and is what
   makes chunks found by *both* signals rank above chunks found by only one).
4. Sort by `score` desc, take `top_k`.
5. `SearchHit` gets two new **optional, default-`None`** fields: `rrf_score: float |
   None` and `fts_rank: int | None`. `distance` keeps its current meaning (vector
   distance) when available. This is purely additive — no existing field changes
   shape, so `test_store_search.py`'s existing assertions and every caller that reads
   `h.source_id` / `h.content` / etc. keep working unmodified.
6. Validate inputs same as `search()` (embedding dim, `top_k >= 1`), plus
   `candidate_k >= top_k` and `rrf_k > 0`.

`search()` itself is **not removed or changed** — zero regression risk to existing
callers/tests. `src/databridge/agents/tools.py::search_knowledge` switches to call
`search_hybrid(query_embedding, query, ...)` (it already has both the embedding and the
raw query text in scope, so this is a one-line call-site change).

### 2.4 Test plan (integration marker, `HashedEmbedder`, no live GCP needed)

- Fusion actually combines both signals: a chunk present in both the vector-top and
  FTS-top candidate lists outranks a chunk present in only one.
- Space isolation still holds through the hybrid path.
- Graceful degradation: a query with no FTS match (symbols-only / all-stopword text)
  still returns vector-ranked results, no exception.
- Input validation: `candidate_k < top_k` and `rrf_k <= 0` both raise `ValueError`,
  matching the existing validation style in `search()`.
- `top_k` is still honored on the fused, deduplicated result.

## 3. Feature B — Claim-Level Citation Verification

### 3.1 Current state (`src/databridge/agents/runtime.py`)

`_to_grounded_answer` treats the **entire** `final_text` as one atomic claim: if *any*
valid `SOURCES: [n]` marker exists anywhere in the text, the whole text (minus the
marker line) becomes the answer, cited by whatever refs appeared in that one trailing
block. A model can pad the real, cited claim with additional uncited sentences and they
ride along unchallenged — this is the gap the 3-party idea exchange flagged.

### 3.2 Design decision: **line-level granularity, not sentence-level**

Sentence-boundary regexes are fragile across the Korean/English mix this project
targets (`team.py` explicitly instructs "answer in the user's language"; Korean
sentence enders like `습니다.`/`요.` don't reliably match a period-based English
sentence splitter, and README.ko confirms Korean is a first-class output language).
Splitting on `\n` instead is deterministic regardless of language and pushes the
"one claim, one line" discipline into the prompt instead of into a parser — the model
is asked to structure its own output, which is a prompt-engineering problem that
degrades gracefully (worst case: a marker-less line gets dropped), rather than a
parsing problem that can silently mis-segment.

### 3.3 Marker convention (replaces, does not extend, the old trailing-block format)

- **Knowledge Agent**: each line stating a factual claim ends with its own inline
  ref marker, e.g. `Production deploys happen every Tuesday and Thursday. [1]`.
  Blank lines and lines with no factual content need no marker.
- **Report Agent** (markdown action-item table): the existing `Source` column *is*
  the per-row marker — change its content from free text to the same `[n]` ref
  syntax, e.g. `| Alice | Fix retry logic | 2026-08-01 | [2] |`. Header/separator
  rows (`---`) are structural, not claims, and are never dropped.
- Prompts in `team.py` (`_KNOWLEDGE_INSTRUCTION`, `_REPORT_INSTRUCTION`) are rewritten
  to state this convention explicitly and drop the old "add a SOURCES: line at the
  end" rule.

### 3.4 Runtime behavior

New core primitive in `runtime.py`, e.g. `_bind_claims(text: str, doc_evidence) ->
tuple[str, tuple[Citation, ...], tuple[str, ...]]` (kept answer text, citations, dropped
claim lines):

1. Split `text` on `\n`.
2. For each non-blank line: if it is a report-table data row, extract refs from its
   last `|`-delimited cell; otherwise extract trailing `[n]` markers from end of line
   (reuse the existing `_REF_RE` pattern).
3. A line with ≥1 ref that resolves in `doc_evidence` is **kept** (marker stripped from
   the visible text) and contributes its ref(s) to the citation set. A line with zero
   resolving refs is **dropped** from the answer and recorded in `dropped_claims` —
   this is the "auto-remove weak claims" behavior, a redaction rather than a full
   refusal.
4. Table header/separator rows and structural lines (headings, blank lines) pass
   through unmodified, contribute no citations, and are never counted as
   claims-to-drop. **This structural-line protection also covers fenced code blocks
   (everything between a pair of ` ``` ` lines) — a multi-line atomic unit that must
   be kept or dropped as a whole, never line-by-line, so it is exempted from marker
   checking entirely (moved in-scope per Antigravity's pre-implementation review, §6).
   Blockquote (`>`) and list-marker (`-`/`*`/`1.`) lines are `**not**` given this
   blanket exemption — corrected after a Claude review pass, §6: unlike a code fence,
   each blockquote/list line is an independently complete markdown unit, and in
   practice this is the most common shape a model uses to enumerate multiple grounded
   claims (`- Deploys happen Tuesdays and Thursdays. [1]`). Exempting them wholesale
   would let unmarked bulleted claims through unchecked — precisely the loophole
   Feature B exists to close. They go through the same trailing-`[n]`-marker check as
   plain prose lines (the list/blockquote prefix is irrelevant to that check, which
   only looks at the line's end) and can be individually dropped without corrupting
   markdown structure, since removing one bullet from a list is syntactically fine.**
5. If every line is dropped (or the input had no lines with content at all), raise
   `NoEvidenceError` exactly as today — the top-level strict contract is unchanged,
   only the grain at which it is enforced gets finer.
6. BigQuery evidence attachment is untouched — it is deterministic (the SQL that ran
   *is* the citation) and was never text-marker-based; confirm no coupling was
   introduced between the two paths. **Explicit regression guard (Antigravity §6):
   a response produced by the Data Agent alone (no `search_knowledge` call, no
   document evidence) must never have its prose lines run through the document
   marker-drop logic — `_bind_claims` must be applied only to text that is expected to
   carry document markers, or must short-circuit to "keep as-is" when `doc_evidence`
   is empty and the only evidence is BigQuery, otherwise a normal BigQuery-only
   answer would have every line dropped for "missing a doc marker" and incorrectly
   raise `NoEvidenceError`.**

`TeamResult` (the runtime-internal return wrapper, **not** the framework-agnostic
`GroundedAnswer`/`Citation` contract, which stays frozen per design §4.1) gains one new
field: `dropped_claims: tuple[str, ...]`. This is for observability/debugging/UI trace
— never surfaced to the end user as answer content, but visible to the 3-party review
and any future collaboration-trace UI work.

### 3.5 On existing tests (`tests/test_runtime_grounding.py`)

The existing fixtures use single-line answers with a trailing `SOURCES: [n]` block —
that format goes away entirely with this change (per the no-backwards-compat-shim
convention this project already follows elsewhere: `replace_source`'s atomic
delete+insert, space-scoped mutations). **Rewrite the fixtures to the new per-line
marker format rather than keeping a legacy fallback path** — a dual-format parser
would be strictly more code and more ambiguity for no product benefit, since nothing
depends on the old format outside this repo.

### 3.6 Test plan (deterministic, pure-function — no live LLM required, same style as
today's `test_runtime_grounding.py`)

- A multi-line answer where one line carries a valid marker and another does not:
  assert the final answer contains only the marked line, the unmarked line appears in
  `dropped_claims`, and citations reflect only the marked line's ref.
- All lines unmarked → `NoEvidenceError` (same as today's "no markers" test, rewritten
  to the new format).
- Report-table row without a resolving ref in its `Source` cell is dropped; header/
  separator rows survive untouched; other valid rows are kept.
- BigQuery-only citations (no document markers involved at all) are unaffected by the
  refactor — existing `test_bigquery_evidence_cites_sql_automatically` semantics hold.
- Mixed document + BigQuery evidence still combines correctly (existing
  `test_mixed_evidence_combines_citations` intent, updated to the new marker format).

## 4. Explicitly out of scope for this iteration (do not creep into this PR)

- True semantic support-verification (does the cited chunk *actually* substantiate the
  specific claim, beyond "a marker naming a real ref exists") — this would need an
  LLM-judge pass or embedding-overlap threshold and is a separate, larger piece of
  work. Flagging honestly: this iteration raises the grain of enforcement from
  "whole answer" to "one line," it does not add semantic fact-checking.
- Korean-aware FTS (no stock Postgres Korean text search config) — English-only `'english'`
  config is accepted as a known limitation.
- HNSW/ANN index tuning on `embedding` — corpus is still small (unchanged from
  `schema.sql`'s existing comment).
- Golden-set regression via `scripts/run_golden.py` requires live Vertex AI/GCP
  credentials that are not available in every dev environment (including Codex's and
  Claude's sandboxes here) — this must be run manually by the repo owner before
  merging as the final acceptance gate. Automated tests in this doc's §2.4/§3.6 are the
  offline-verifiable substitute, not a replacement for that live check.

## 5. Acceptance criteria (for the 3-party review pass)

1. `uv run pytest -q` passes, including new tests, with zero regressions to existing
   tests outside the ones intentionally rewritten in §3.5.
2. `uv run ruff check .` and `uv run mypy` clean.
3. `search_hybrid` behavior matches §2.3/§2.4; `search()` is provably untouched (diff
   review, not just tests — a subtle refactor-while-adding-a-method could still change
   shared helper behavior).
4. Claim-binding behavior matches §3.4/§3.6; specifically confirm the BigQuery-only
   path (§3.4.6) is genuinely untouched, not just untested.
5. Owner runs `scripts/run_golden.py` live and confirms `keyword_hit`/`source_hit`
   do not regress from the current 1.000 / 5-5 baseline (README.md).

## 6. Review log

**Antigravity — pre-implementation design review (2026-07-21):** Overall verdict
"proceed with minor prompt/markdown structural guard considerations." Findings applied
above:
- [상] Structural-block protection (code fences, blockquotes, list markers) moved from
  "nice to have" into the in-scope spec, §3.4 step 4 — a bare `\n`-split would drop
  markdown structural lines that carry no marker, corrupting the answer's formatting,
  not just its prose.
- [상] BigQuery-only responses must bypass the document-marker drop logic entirely —
  added as an explicit guard in §3.4 step 6. Without it, a Data-Agent-only answer (no
  `search_knowledge` call) would have every line dropped for lacking a doc marker and
  incorrectly raise `NoEvidenceError`.
- [중] RRF tie-breaking must be deterministic (`score DESC, distance ASC, chunk_id ASC`)
  — confirmed already satisfied by the implementation's sort key (see Codex log below).
- [중] Prompt must warn strongly that an unmarked factual line is silently deleted, to
  reduce prompt-non-adherence risk with Gemini 2.5 Flash — carried to Codex as an
  implementation instruction (§3.3 prompt rewrite).
- [하] `schema.sql`'s base `CREATE TABLE` not itself containing `content_tsv` was
  raised, then resolved as a non-issue: `ensure_schema()` executes the whole file
  top-to-bottom every time, so `CREATE TABLE IF NOT EXISTS` followed immediately by
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` is correct and idempotent for both a fresh
  install and an existing database in the same script run.
- Markdown table risks noted (escaped `\|` inside a cell breaking `split('|')`, a model
  reordering/renaming the `Source` column) — carried to Codex as implementation-time
  edge cases to guard against explicitly in `_bind_claims`' table-row handling.

**Codex — implementation (2026-07-21):** complete. `uv run pytest -q` 41 passed (incl.
live docker-compose pgvector integration tests), `ruff check .` clean, `mypy` clean.
`search()` left untouched; `search_hybrid()` added additively with `rrf_score`/
`fts_rank` as new optional `SearchHit` fields. `_bind_claims` implements line- and
table-row-level marker binding as specced. Codex's own note: "BigQuery-only/mixed
responses' unmarked lines keep the existing automatic SQL-citation behavior" — see
Claude's finding below, this is narrower than intended.

**Claude — post-implementation code review (2026-07-21):** two findings from reading
the actual diff (not just re-reading the design):

- **[상, confirmed] `keep_unmarked` guard is keyed on the wrong condition.**
  `runtime.py`'s `_ground_answer` calls `_bind_claims(text, doc_evidence,
  keep_unmarked=bool(bq_evidence))` — this bypasses per-line dropping for *every*
  response that has any BigQuery evidence at all, not only BigQuery-only responses.
  In a **mixed** document+BigQuery answer, an unmarked, ungrounded document-style claim
  now survives uncaught, because `bq_evidence` being non-empty is enough to disable
  line-dropping for the whole answer — this reopens exactly the loophole Feature B was
  built to close. The rewritten `test_mixed_evidence_combines_citations` in
  `tests/test_runtime_grounding.py` currently *asserts* this behavior
  (`assert "The query returned one row." in ga.answer` — an unmarked line, kept only
  because `bq` is non-empty), so the test suite passing does not catch this. Fix:
  change the guard to `keep_unmarked=not doc_evidence` — true exactly when there is no
  document evidence to bind claims against (the real BigQuery-only case), false
  whenever `doc_evidence` is non-empty so per-line dropping still applies to mixed
  answers. The existing BigQuery-only test (`test_bigquery_evidence_cites_sql_
  automatically`, `doc_evidence={}`) is unaffected by this fix; the mixed test needs
  its unmarked line either removed from the fixture or given a marker, since with the
  corrected guard an unmarked document-style line in a mixed answer should now drop.
- **[상, confirmed] Structural-block protection (code fences / blockquotes / list
  markers) from this doc's §3.4 step 4 is not implemented.** `_structural_line_indexes`
  in `runtime.py` only recognizes headings, horizontal rules, and table separator/
  header rows — there is no fenced-code-block, blockquote (`>`), or list-marker
  (`-`/`*`/`1.`) handling. This gap exists because the design doc was amended with this
  requirement (from Antigravity's pre-implementation review) *after* Codex had already
  started implementing, so Codex's pass predates it. Needs a follow-up: track
  fenced-code-block state (toggle on lines matching a ``` fence, treat every line
  inside as structural) and treat blockquote/list-marker lines as structural the same
  way heading lines are today.
- Everything else reviewed clean: `search()` is provably untouched (diff-only change is
  the new `search_hybrid` method + two new optional `SearchHit` fields), RRF tie-
  breaking is deterministic (`-score, distance, chunk_id`), table-row marker extraction
  correctly isolates the last `|`-delimited cell, schema migration is idempotent for
  both fresh and existing databases confirmed via the live docker-compose integration
  run.

Both findings sent back to Codex as a corrective follow-up; Antigravity asked to
independently review the actual diff (not just the design) as a second code-review
pass.

**Codex — correction round (2026-07-21):** both findings fixed exactly as requested —
`keep_unmarked=not doc_evidence`, and `_structural_line_indexes` now tracks fenced
code-block state (toggle on a ``` fence line, mark every interior line structural)
plus blockquote/list-marker recognition. `uv run pytest -q` 42 passed, ruff/mypy clean.

**Claude — verification of correction round (2026-07-21):** traced the actual diff.
The `keep_unmarked` fix is correct. But the structural-line fix over-corrected: it
added blockquote (`>`) and list-marker (`-`/`*`/`1.`) lines to the *same wholesale
exemption* as headings/code-fences — i.e. these lines now bypass marker-checking
unconditionally, `continue`-ing before `_bind_claims` ever looks for a `[n]` marker on
them. **This reopens the Feature B loophole for the single most common way a model
enumerates multiple grounded facts (a bulleted list)** — an unmarked bullet like
`"- Some invented fact."` would now survive untouched, uncited. This is a spec bug I
introduced myself (my §3.4 step 4 amendment said blockquote/list lines should be
protected "the same way heading lines are," which was too broad) — corrected in §3.4
above: only fenced code blocks need whole-block, all-or-nothing exemption (removing
part of a multi-line code block would corrupt it); a blockquote or list-item line is
independently complete markdown, so it should go through the *same* trailing-`[n]`
marker check as plain prose (dropping one unmarked bullet doesn't break markdown
syntax, unlike removing an interior code-fence line would).

**Antigravity — code-diff review, round 2 (2026-07-21):** independently found the same
bullet-list regression (converges with Claude's finding above — high confidence this
is real) plus two additional issues Claude had not caught:
- [상, new] `_TRAILING_REFS_RE` anchors markers strictly at end-of-line
  (`(?:...)+\s*$`) — a line ending `"...twice weekly [1]."` (period *after* the
  bracket, which is the more conventional citation-style punctuation placement) fails
  to match, since a literal `.` sits between the marker and end-of-string. Such a line
  would be wrongly treated as unmarked and dropped despite carrying a valid ref. Fix:
  the marker regex must tolerate a single optional trailing sentence-ending
  punctuation mark (`.`/`!`/`?`) after the marker group, and stripping must preserve
  that punctuation in the kept output text (drop only the bracket markers themselves).
- [상, new, accepted as a documented risk rather than fixed now] a line containing more
  than one factual claim (partial prompt non-adherence — the model doesn't fully
  follow the "one claim per line" instruction) only has its line-final marker group
  recognized; an earlier mid-line marker is not separately bound. Not fixing this now:
  building a mid-line multi-claim splitter adds real parser complexity for a case the
  prompt is already explicitly designed to prevent (§3.3). Recorded here as a known,
  accepted residual risk rather than in-scope work — re-open only if live usage shows
  this actually happens often.
- [중, new, accepted as a documented risk] a table with a multi-line or captioned
  header (more than one line between the last non-table content and the `|---|`
  separator) only protects the single line immediately above the separator; an outer
  caption line could be misidentified as a droppable claim line. Edge case, low
  likelihood given `team.py`'s prompt specifies a plain single-header-row table;
  accepted as a known limitation, not fixed now.
- [중] `team.py` prompt wording itself assessed as clear and well-suited to Gemini
  2.5 Flash's instruction-following; recommends the prompt example show both
  punctuation placements (or, equivalently, that the backend regex fix above makes the
  prompt-side fix unnecessary — Claude's fix covers this).
- Feature A (RRF/schema): no correctness or regression issues found; explicitly praised
  the FTS candidate query's inclusion of vector distance for safe tie-breaking even on
  FTS-only-matched rows.

**Round 3 fix sent to Codex:** (a) narrow structural-line exemption to fenced code
blocks only — blockquote/list-marker lines go through the normal per-line marker check;
update the test that currently asserts unmarked bullets/blockquotes survive
unconditionally. (b) `_TRAILING_REFS_RE` tolerates one optional trailing `.`/`!`/`?`
after the marker group, preserving that punctuation in the stripped output text; add a
regression test for `"...twice weekly [1]."`. The two accepted-risk items (multi-claim
lines, multi-line table headers) are recorded above, not sent as fix requests.

**Codex — round 3 correction (2026-07-21):** both fixes applied exactly as requested.
`_structural_line_indexes` no longer references blockquote/list patterns at all —
only heading, horizontal rule, table separator, and code-fence-toggle state remain
wholesale-exempt. `_TRAILING_REFS_RE` gained a `(?P<punctuation>[.!?])?` group and
`_strip_line_markers` now slices out only the `markers` named-group span, leaving any
trailing punctuation in place. New tests cover marked/unmarked bullets, blockquotes,
and ordered-list items side by side, plus the `"...weekly [1]."` punctuation case.
`uv run pytest -q`: 43 passed. `ruff`/`mypy`: clean.

**Claude — final independent verification (2026-07-21):** re-read the `_structural_
line_indexes`/`_strip_line_markers` diff directly — both round-3 fixes are correct.
Independently re-ran `uv run pytest -q` (43 passed), `uv run ruff check .` (all
checks passed), and `uv run mypy` (no issues, 19 source files) myself rather than
trusting Codex's self-reported terminal output. **Verdict: ready for the owner's
final gate** — `scripts/run_golden.py` against live Vertex AI (§4, this cannot be run
from any sandbox in this review loop and remains the one manual step before merge).
No outstanding code-level findings; two accepted risks remain documented above
(multi-claim-per-line, multi-line table captions) and are intentionally out of scope,
not oversights.

## 7. Live golden-set run (2026-07-21) — owner-run acceptance gate

Result: **PASS**. `keyword_hit=1.000`, `source_hit=5/5` against live Gemini 2.5 Flash +
real Vertex `gemini-embedding-001` embeddings — matches the README baseline exactly,
no regression. This closes acceptance criterion §5 item 5, the only gate that could
not be exercised from any sandbox in this review loop.

Root cause of the initial auth failure (unrelated to this feature or the GCP credit
itself): the shell environment had `GOOGLE_APPLICATION_CREDENTIALS` pointing at an
unrelated project's service-account key, which takes priority over `gcloud auth
application-default login`'s ADC and silently authenticated every call as that
unrelated, unauthorized identity. Fixed by unsetting the variable per-invocation
(`env -u GOOGLE_APPLICATION_CREDENTIALS ...`); the `genaiacademy-ph` project's IAM
(Owner role) and billing (credit-linked) were correct from the start.

### 7.1 New finding from the live run — upgrading a previously "accepted risk"

The §3.6/§6 "multi-claim-per-line" item was accepted as a low-priority, rarely-occurring
risk in round 2. **The live run shows it is not rare — it reproduced in 3/3 repeated
calls to the same golden question and in 2 of the first 2 golden answers overall.**
Confirmed root cause via direct instrumentation of the raw pre-`_bind_claims` text:

```
RAW: 'Production deploys occur every Tuesday and Thursday at 14:00 UTC. [1] Hotfixes
can be deployed at any time with two approvals. [1]'
PROCESSED answer: 'Production deploys occur every Tuesday and Thursday at 14:00 UTC.
[1] Hotfixes can be deployed at any time with two approvals.'
```

The model puts multiple sentences on one physical line, each internally followed by
its own `[1]` marker. `_TRAILING_REFS_RE`/`_strip_line_markers` only recognize and
strip the **line-final** marker group — every earlier mid-line marker is left as
literal, visible `[1]` text in the user-facing answer. This is a real product-quality
defect (stray citation brackets leaking into prose), not merely cosmetic: it also means
that if a model ever uses *different* refs for different mid-line sentences, only the
trailing ref is captured as a citation and earlier refs are silently dropped from the
citation set even though their bracket text remains visible — a second latent
correctness gap layered under the visible one.

**Proposed fix direction (for review, not yet sent to Codex):** stop anchoring marker
detection to end-of-line. Scan the whole line for *all* `[n]` marker occurrences
(`_REF_RE.finditer`), collect every resolving ref as a citation for that line, and
strip *every* matched marker span from the visible text (removing spans in reverse
order to avoid index shift), not only the last one. The line is kept if it has *any*
resolving ref, dropped if it has none — same overall keep/drop semantics as today,
just no longer blind to non-trailing markers. This is being sent to Antigravity and
Codex for independent review before implementation, per the user's request to
re-run 3-party verification on this specific finding.

### 7.2 Review of the §7.1 fix direction

**Antigravity:** verdict "highly valid, recommended for immediate implementation."
Two additions: (a) after removing multiple mid-line marker spans, clean up leftover
double-spaces and space-before-punctuation artifacts (e.g. `"...UTC.  Hotfixes"` or
`"...UTC ."`) with a small whitespace/punctuation normalization pass; (b) strip *every*
matched `[n]` tag from the visible text regardless of whether it resolves — a stray
non-resolving `[99]` left dangling in prose is an equally ugly UX defect even though it
correctly contributes no citation.

**Codex:** verdict: the whole-line scan is "a realistic direction that solves the
observed problem." Independently raised the *same* two points as Antigravity
(whitespace/punctuation cleanup after span removal; strip all matched tags, not just
resolving ones) — convergent, high-confidence findings. Additionally confirmed table
rows must **keep** the existing last-cell-only scoping (scanning a whole table row
would risk mangling non-Source cell content, e.g. an Action cell that happens to
contain bracketed text) and code-fence bypass is unaffected. Noted residual accepted
risk (unchanged from round 2): a line mixing one resolving ref with one non-resolving
ref is still kept wholesale rather than split into separately-verified claims —
splitting on marker boundaries would materially raise parser complexity for a
partial-compliance case that is not the common failure mode observed live; not fixing
now.

**Final agreed direction (3-party consensus, sent to Codex for implementation):**
1. Non-table lines: `_REF_RE.finditer` over the *whole* line, not just a trailing
   anchor. Collect every ref that resolves in `doc_evidence` as a citation for the
   line (dedup, preserve first-seen order, matching current behavior). Keep the line
   if at least one ref resolves; drop it if none do (unchanged semantics).
2. When reconstructing the visible text: remove *every* matched `[n]` span (resolving
   or not), not only the trailing one, then normalize leftover whitespace (collapse
   runs of spaces, remove a stray space immediately before `.`/`,`/`!`/`?`).
3. Table-row Source-cell scoping is **unchanged** — still scoped to the last
   `|`-delimited cell only, never the whole row.
4. Code-fence wholesale exemption is **unchanged**.

### 7.3 Implementation and final verification (2026-07-21)

**Codex — round 4 implementation:** `_bind_claims` now scans the full line (or the
table's Source cell) via `_REF_RE.finditer`, collects every resolving ref, and removes
*all* matched marker spans plus marker-only comma separators via a new
`_remove_ref_markers` helper (whitespace-run collapse + space-before-punctuation
cleanup, leading indentation preserved for markdown compatibility). 6 new regression
tests added (multi-ref-per-line collection+removal, dedup/unknown-ref stripping,
whitespace/punctuation normalization). `uv run pytest -q`: 47 passed. `ruff`/`mypy`:
clean.

**Claude — final verification, both offline and live (2026-07-21):**
- Independently re-ran `pytest`/`ruff`/`mypy` myself: 47 passed, both clean.
- Re-ran the *exact* raw string captured from the live bug report
  (`'...UTC. [1] Hotfixes...approvals. [1]'`) directly against `_bind_claims`: output
  is now `'Production deploys occur every Tuesday and Thursday at 14:00 UTC. Hotfixes
  can be deployed at any time with two approvals.'` — clean, no stray brackets, single
  correctly-bound citation.
- **Re-ran the live golden set against real Gemini 2.5 Flash a second time**:
  `keyword_hit=1.000`, `source_hit=5/5` — no regression, and every answer's visible
  text is now free of leftover `[n]` markers (previously DG-001 showed a stray `[1]`
  mid-sentence; now clean). This closes the loop: the finding was reproduced live,
  fixed, and re-verified live, not just against synthetic unit fixtures.

**Status: this round's finding is resolved and verified end-to-end.** No further
action needed before commit.
