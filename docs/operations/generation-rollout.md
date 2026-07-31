# Generation rollout runbook

Replaces `embedding-provenance-rollout.md`, which is a tombstone. That procedure predates the
activation gate and is unsafe; do not follow it.

This runbook cuts a space over from legacy `generation_id IS NULL` chunks to a validated,
sealed, activated generation. It is written for the **first MFS cutover**, where production is
serving the pre-provenance image and no generation exists yet.

**Read the whole runbook before starting.** Steps 3 to 10 are not individually reversible, and
recovery after activation is forward-only.

---

## Every generation command goes through the wrapper

```text
scripts/run_generation_job.sh --project PROJECT --region REGION SUBCOMMAND [args…]
```

(Synopsis, not a command to paste. Every runnable block below is copy-pasteable as written
once the variables in "Before you start" are set.)

**Never call `gcloud run jobs execute` for a generation command.** Two measured facts make the
wrapper the only usable path:

- `gcloud run jobs execute --wait` returns **1 for every failure**. A container that exited 3
  and a job that does not exist are indistinguishable by exit code. The CLI's own 0–5 taxonomy
  is invisible without the wrapper.
- The wrapper reads the Task result and the CLI's result marker, and exits with either the CLI's
  code (0–5) or a wrapper code (80–87) that tells you whether a re-run is safe. For `activate`
  and `delete-legacy` that is the only signal saying whether the operation already ran.

The wrapper runs under **your** credentials, not the job's service account.

---

## What you are trading

**Steps 3–8 are a deliberate maintenance window: `/ask` returns no evidence for the space.**

The serving image reads only the active generation. Until one is activated, MFS has no readable
chunks, so answers become "no supporting evidence". This is chosen over the alternative: the
pre-provenance reader filters on `space_key` alone, so if it were left serving while a
generation is being built it would return legacy and new chunks together — duplicated, mixed
answers, which is worse than an honest refusal.

Budget the window. A clean ingest of the MFS corpus plus validation is the dominant cost.

---

## Before you start

Set the variables and helpers first — the checks below use them.

Set these once:

```bash
PROJECT=genaiacademy-ph
REGION=us-central1
SPACE=MFS
FOLDER_ID_EXPECTED=98380      # the Confluence folder that holds $SPACE

# The digest you intend to roll out. Take it from the build that produced it — the Cloud
# Build result for the merge — not from what is currently deployed. Reading it back from
# the service would make the check compare the deployment against itself.
EXPECTED_IMAGE='us-central1-docker.pkg.dev/genaiacademy-ph/cloud-run-source-deploy/databridge@sha256:CHANGE_ME'

SERVICE_URL=$(gcloud run services describe databridge --project "$PROJECT" --region "$REGION" \
  --format='value(status.url)')
set -o pipefail    # required: without it a failing wrapper is masked by tee

# The digest you intend to roll out. Take it from the build that produced it — the Cloud
# Build result for the merge — not from what is currently deployed. Reading it back from
# the service would make the check compare the deployment against itself.

run_generation() { scripts/run_generation_job.sh --project "$PROJECT" --region "$REGION" "$@"; }
preflight() { uv run python scripts/rollout_preflight.py "$@" --project "$PROJECT" --region "$REGION"; }

# The wrapper reports an outcome, not the command's own output. inventory, report and list
# write their body to the job's log; read it back for the execution that just ran.
show_output() {
  local result_file="${1:?usage: show_output <wrapper-result-file>}" op exec_name
  op=$(preflight operation-id --input "$result_file") || return 1
  exec_name=$(preflight correlate --operation-id "$op") || return 1
  gcloud logging read \
    "logName:\"run.googleapis.com%2Fstdout\" labels.\"run.googleapis.com/execution_name\"=\"$exec_name\" labels.\"run.googleapis.com/task_index\"=\"0\" labels.\"run.googleapis.com/task_attempt\"=\"0\"" \
    --project "$PROJECT" --limit 1000 --order desc --format='value(textPayload)' \
    | python3 -c 'import sys; sys.stdout.writelines(reversed(sys.stdin.readlines()))'
}
```


---

### Preconditions

Most of this is checkable. Run the function; the checklist below it covers only what a
command cannot decide.

```bash
preconditions_ok() { preflight preconditions; }

preconditions_ok
```

**Every later gate chains onto this**, so a failed precondition stops the rollout rather than
scrolling past. It only reads, and is cheap to re-run.

The checks themselves live in `src/databridge/rollout_checks.py` and are unit-tested against the
shapes that have actually caused defects — an omitted `maxRetries` (which Cloud Run defaults to
3, not 0), a boolean where an integer belongs, a job whose image was never read. They were inline
shell here through nine review rounds, and every round found another one that no test could
catch.

When no execution exists yet, task read access cannot be proven and the check says so rather
than passing: run one read-only command through the wrapper, then re-run it.

What no command can decide, and you must judge yourself. The job configuration, the DSN secret
reference, task read access and the recovery window are **asserted by the function above** — they
are not repeated here, because a checklist item that duplicates an assertion invites ticking the
box instead of running the check.

- [ ] You can author the validation query file. It does not exist in the repository yet; it is
      written from `inventory` output between steps 4 and 5 (see step 4a).
- [ ] The retention window is long enough for **your** rollout. The function proves a window
      exists; only you know how long these steps will take and whether that fits inside it:

```bash
gcloud sql instances describe databridge-demo --project "$PROJECT" \
  --format='value(settings.backupConfiguration.transactionLogRetentionDays)'
```

- [ ] The cost of recovery is acceptable to whoever owns this data, and the RTO below is one
      you have agreed **before** starting.

These two are what authorise the irreversible deletion in step 8, so they get a token rather
than a checkbox. Record the retention you saw and accepted:

```bash
RECOVERY_ACCEPTED_DAYS=7      # the retention you read above, and accepted

recovery_accepted_ok() {
  local have
  have=$(gcloud sql instances describe databridge-demo --project "$PROJECT" \
    --format='value(settings.backupConfiguration.transactionLogRetentionDays)') || return 1
  [ -n "${RECOVERY_ACCEPTED_DAYS:-}" ] || {
    echo "STOP: set RECOVERY_ACCEPTED_DAYS after judging the retention window" >&2; return 1; }
  [ "$have" = "$RECOVERY_ACCEPTED_DAYS" ] || {
    echo "retention is now $have days, not the $RECOVERY_ACCEPTED_DAYS you accepted" >&2; return 1; }
  echo "recovery window accepted: $have days"
}
```

Binding the value rather than a yes/no means a retention change between your judgement and the
rollout invalidates the approval instead of silently keeping it.

**PITR is not a rollback button.** Understand the whole cost before relying on it:

- It **restores into a new Cloud SQL instance**. The original is untouched.
- Using it means: stop every writer → verify the restored instance → repoint `DATABRIDGE_DSN`
  → redeploy the service and jobs against it.
- **Everything else written after the recovery point is lost too**, not just this rollout.
- Decide the **acceptable RTO before starting**. If that number is smaller than the sequence
  above takes, PITR is not your recovery plan and you should not start the rollout relying on it.

`$GENERATION` appears from step 3 onward. It does not exist before then — do not guess it.

---

## Reading the wrapper's exit code

The wrapper prints one line to stdout and exits with the code below.

```
DATABRIDGE_WRAPPER_RESULT {"operation_id":…,"wrapper_exit":…,"wrapper_reason":…,"cli_exit":…,"cli_reason":…,"cli_generation_id":…}
```

| Exit | Meaning | Re-run? |
|---|---|---|
| **0** | Succeeded. | — |
| **1–5** | The command failed for a data or contract reason; `cli_reason` names it. | Only after fixing the cause. |
| **80** `wrapper_timeout` | The job may still be running. | **No.** Wait, then check `gcloud run jobs executions list`. |
| **81** `result_marker_missing` | The job ran; its result line never arrived. | **No.** Read the execution's logs. |
| **82** `result_marker_mismatch` | The result line broke its contract. | **No.** |
| **83** `task_result_unavailable` | The Task result could not be read, or did not pass the success gate. Cancelled and timed-out tasks land here. | **No.** |
| **84** `task_terminated_by_signal` | The Task reported a termination signal. | **No.** |
| **85** `wrapper_transport_error` · `correlation_indeterminate` | A read failed, or we could not tell whether an execution was created. | **No.** |
| **85** `creation_confirmed_absent` | The wrapper failed **before** the request was sent. | **Yes** — the only reason that permits a manual re-run. |
| **86** `unexpected_task_exit_code` | The container exited outside 0–5. | **No.** |
| **87** `duplicate_execution` | Two executions carry the same operation id. | **No.** Check the result of **both**. |

Three things worth knowing before you need them:

- **80–87 are not command failures.** They do not mean the data is bad. They mean the wrapper
  could not establish what happened.
- **86 covers signal kills.** A container killed by SIGKILL reports `exitCode: 137` (128 + 9),
  not a termination signal — measured on this platform. `128 + n` conventionally indicates a
  signal, but the API does not confirm it, so the wrapper does not claim it. Read the raw code
  in the log line if you need the cause.
- **Never re-run `jobs execute` after a lost response.** The execution may already exist.
  Running `activate` or `delete-legacy` twice is the failure this design exists to prevent.

---

## Step 1 — Pause the scheduler

```bash
scheduler_paused_ok() {
  gcloud scheduler jobs pause databridge-confluence-ingest \
    --project "$PROJECT" --location "$REGION" || return 1
  local state
  state=$(gcloud scheduler jobs describe databridge-confluence-ingest \
    --project "$PROJECT" --location "$REGION" --format='value(state)') || return 1
  [ "$state" = "PAUSED" ] || { echo "scheduler state is $state, expected PAUSED" >&2; return 1; }
  echo "scheduler PAUSED"
}

preconditions_ok && scheduler_paused_ok
```

A batch starting mid-rollout writes into the wrong generation. The function asserts the state
rather than printing it; step 2 chains onto it.

## Step 2 — Update every job image, then verify each

`update-jobs` runs before `deploy`, but when a deploy fails the job update can be skipped, so
job images drift silently. Check digests rather than assuming.

```bash
images_aligned_ok() { preflight images --expected-image "${EXPECTED_IMAGE:?}"; }

preconditions_ok && recovery_accepted_ok && scheduler_paused_ok && images_aligned_ok && \
gcloud run services update-traffic databridge --project "$PROJECT" --region "$REGION" --to-latest
```

A job left on the old image writes with old schema assumptions and fails against the migrated
schema, so the traffic move is chained onto the digest check. `update-jobs` runs before `deploy`
in the build, but a failed deploy can skip the job update — which is exactly how digests drift
without anyone noticing.


**From here `/ask` returns no evidence for this space until step 8.** That is specified
behaviour. Do not roll back on seeing it — rolling the image back once building rows exist is
worse, because the old reader returns legacy and building rows together.

## Step 3 — Create the building generation

```bash
run_generation create-building --space "$SPACE" | tee /tmp/create.out
unset GENERATION
GENERATION=$(preflight generation-id --input /tmp/create.out)
echo "GENERATION=${GENERATION:?STOP: no usable generation id — do not continue}"
```

`preflight generation-id` yields the id only when there is exactly one wrapper result line,
the wrapper exited 0, the CLI exited 0, and `cli_generation_id` is a positive integer that is not
a boolean. Otherwise it prints why and leaves `GENERATION` unset. Those rules are unit-tested in
`tests/test_rollout_checks.py`, not written out here.

**The `:?` is the gate, not the prose.** An interactive shell does not stop just because a
command failed, so every later step references `${GENERATION:?}`; with the variable unset those
commands refuse to run instead of acting on the wrong generation.

`create-building` is idempotent: an existing `building` generation with the same embedding
profile is reused and the output says so. A `sealed` one blocks with exit 4. Use
`--discard-inflight` only if you intend to destroy that build.

> **Rollback is still possible here.** This writes one `space_generation` row and no chunks, so
> the old revision would still read correctly.

## Step 4 — Clean ingest into that generation

**Verify the job's scope first.** `setup_cicd.sh` tells the provisioner to use a dedicated
corpus key, so the configured space is not necessarily the one you are migrating. Parse the env
structurally — `env` is a list of `{name, value}` objects, so splitting on commas separates a
name from its own value:

```bash
verify_ingest_scope() {
  preflight ingest-scope --space "$SPACE" --folder-id "${FOLDER_ID_EXPECTED:?}"
}

# The ingest runs only if the scope check passes. Do not split these two.
verify_ingest_scope && \
gcloud run jobs execute databridge-confluence-ingest --project "$PROJECT" --region "$REGION" \
  --update-env-vars "DATABRIDGE_GENERATION_ID=${GENERATION:?}" --wait
```

> This is the one `jobs execute` in the runbook. The ingest job is **not** a generation command
> and has no wrapper, so its exit code only distinguishes "worked" from "did not". Verify the
> outcome with `inventory` below rather than trusting it.

Use the **execution-time override**. Do not put `DATABRIDGE_GENERATION_ID` in the job's
configuration — a permanent value would make every future scheduled run target a generation that
is by then retired. (Measured: an execution-time override is preserved in the execution spec and
does not leak back into the job.)



> **After this step you cannot roll back to the old revision.** The boundary is the first
> `replace_source` commit, early in the job. If the job aborted and you need to know whether
> anything was written, confirm the space holds zero non-null-generation chunks before
> considering a traffic rollback.

If the job fails partway the manifest stays `in_progress`, and validation will refuse the
generation. Discarding creates a **new** generation, so re-derive the id and run the ingest
again before doing anything else:

```bash
run_generation create-building --space "$SPACE" --discard-inflight | tee /tmp/create.out
unset GENERATION
GENERATION=$(preflight generation-id --input /tmp/create.out)
echo "GENERATION=${GENERATION:?STOP: no usable generation id — do not continue}"    # a new id

verify_ingest_scope && \
gcloud run jobs execute databridge-confluence-ingest --project "$PROJECT" --region "$REGION" \
  --update-env-vars "DATABRIDGE_GENERATION_ID=${GENERATION:?}" --wait
```

The retry path takes the same scope gate as the first run — an unguarded retry was how the
original draft let a wrong corpus through.

Running `inventory` against the old id after a discard reports on a generation that no longer
exists. Always re-read `$GENERATION` first.

Then confirm what landed:

```bash
run_generation inventory --space "$SPACE" --generation-id "${GENERATION:?}" | tee /tmp/inv.out && show_output /tmp/inv.out
```

Check the source set and per-source chunk counts against what you expect from Confluence. The
manifest records **what the batch believed it wrote** — it catches storage-side divergence, not
an incomplete fetch. If a page never arrived from Confluence, the manifest agrees with itself,
and no command can tell you otherwise: there is no independent list of what should be there.

That makes this a human judgement, so it gets the same treatment as step 7 — a token bound to
the generation, which `validate` is chained onto:

```bash
INVENTORY_VERIFIED="${GENERATION:?}"    # set this only after you have read the inventory

inventory_verified_ok() {
  [ "${INVENTORY_VERIFIED:-}" = "${GENERATION:?}" ] || {
    echo "STOP: the inventory for generation ${GENERATION} was not reviewed" >&2; return 1; }
}
```

A token left over from a discarded generation will not match the new id, so a retry cannot
inherit the previous review.

## Step 4a — Author, upload and pin the validation query file

The file does not exist in the repository. Write it from the `inventory` output above. The
contract `validate` enforces is:

- **at least five queries in total**, with unique ids;
- **at least one query per source** in the generation;
- **two queries with different `heading_intent`** for any source that has two or more headings;
- the required categories present, including an English paraphrase, an English keyword query,
  and Korean morphology.

A file that misses any of these is rejected with exit 2 before validation runs.

**Do not commit it to `main` during the rollout** — that triggers Cloud Build and changes the
image mid-procedure. Upload to GCS only; commit separately, before or after.

The object path carries the generation id and the file's own SHA-256, so **an upload can never
overwrite a previously validated file**. Compute the SHA first, then build the path from it:

```bash
QUERY_BUCKET="${PROJECT}-databridge-validation-queries"
LOCAL=./evals/mfs_validation_queries.yaml     # authored locally, not committed yet

SHA=$(shasum -a 256 "$LOCAL" | cut -d' ' -f1)   # shasum: present on macOS and Linux
OBJECT="validation-queries/${SPACE}/${GENERATION:?}/${SHA}.yaml"
QUERIES="/queries/${OBJECT}"

gcloud storage cp "$LOCAL" "gs://${QUERY_BUCKET}/${OBJECT}" --project "$PROJECT"

# Record all three in the rollout log
echo "sha256=$SHA"
gcloud storage objects describe "gs://${QUERY_BUCKET}/${OBJECT}" \
  --project "$PROJECT" --format='value(generation,md5Hash,size)'
```

The object generation and checksum are your audit record of exactly which bytes were validated.
`validate` recomputes the SHA-256 of the **mounted** bytes and compares it to the value you pass,
so a wrong file fails closed rather than being silently accepted — but only this step gets the
right file to the right place. **Upload as the operator**, not as a deployment service account.

## Step 5 — Validate, which seals on success

```bash
inventory_verified_ok && \
run_generation validate --space "$SPACE" --generation-id "${GENERATION:?}" \
  --queries "$QUERIES" --expected-queries-sha256 "$SHA"
```

`--expected-queries-sha256` is required and has no opt-out. It fails closed when the mounted
file is not the one you reviewed.

Validation runs structural checks, then a validation-only search path, then re-checks
concurrency. **Every query must pass; there is no averaging.** On success the generation is
sealed and a receipt carrying a checksum is written. Nothing can write to it afterwards.

- exit 3 `structural_validation_failed` — the manifest and the stored rows disagree.
- exit 3 `search_validation_failed` — retrieval did not answer a required query; the report
  names which. Fix the corpus or the query file, then re-ingest into a **new** generation. A
  sealed generation cannot be amended.
- exit 5 `concurrent_change` — something else touched the space. Stop and find out what.

## Step 6 — Activate

```bash
run_generation activate --space "$SPACE" --generation-id "${GENERATION:?}" --yes
```

Activation accepts only a sealed generation, re-verifies the receipt, checksum, manifest
revision, and profile under the space lock, then switches atomically. `--yes` is required and is
checked before any database access.

**Legacy rows still exist, deliberately.** They are the fallback until step 7 proves the new
generation serves correctly.

## Step 7 — Verify serving before deleting anything

```bash
curl -sf -X POST "$SERVICE_URL/ask" -H 'Content-Type: application/json' \
  -d '{"question":"<a question you know this corpus answers>"}' | head -40
```

`-f` matters: without it an HTTP error is not a shell failure and the block would look fine.

Then ask something this corpus does not cover, and confirm it is refused rather than answered:

```bash
curl -sf -X POST "$SERVICE_URL/ask" -H 'Content-Type: application/json' \
  -d '{"question":"What is the approved catering menu for the Neptune office holiday party?"}' \
  | head -20
```

Check by hand that the first answer carries citations resolving into this corpus, and that the
second is a refusal. Both halves matter: citations prove the generation serves, and the refusal
proves it is not answering from something else. **This judgement cannot be automated for a space that has no
golden file** — the repository's golden set targets `DEMO`, and the runner refuses a space
mismatch, so there is no command that can decide this for `$SPACE`.

Because the judgement is human, the gate is an explicit token. Record what you verified:

```bash
SERVING_VERIFIED="${GENERATION:?}"     # set this only after you have read the answers
```

**If anything is wrong, stop. Do not proceed to step 8.** Recovery is forward-only: build,
validate, and activate a new generation. Do not reactivate a retired generation, and do not
change lifecycle state with SQL.

## Step 8 — Delete legacy rows

Only after step 7 passed. First-cutover-only, and irreversible.

```bash
serving_verified_ok() {
  [ "${SERVING_VERIFIED:-}" = "${GENERATION:?}" ] || {
    echo "STOP: step 7 was not completed for generation ${GENERATION}" >&2; return 1; }
}

recovery_accepted_ok && serving_verified_ok && \
run_generation delete-legacy --space "$SPACE" --generation-id "${GENERATION:?}" --yes
```

The deletion runs only as a consequence of that check. Setting `SERVING_VERIFIED` without having
read step 7's answers defeats the gate — it exists so the irreversible command cannot be reached
by scrolling past a failed verification, not to certify that you looked.

The command re-checks activation integrity every time: the target must be the active
generation, its receipt must match the current profile and chunk count, and its manifest must be
complete. While legacy rows remain it additionally checks that the receipt's checksum, chunk
count, and manifest revision have not drifted; drift means the window has closed and it refuses
with `legacy_cleanup_window_closed`.

It is idempotent — with zero legacy rows it still runs the integrity checks and exits 0. Output
carries `space_key`, `generation_id`, `legacy_count_before`, `deleted_count`.

Confirm:

```bash
run_generation report --space "$SPACE" | tee /tmp/report.out && show_output /tmp/report.out
```

Legacy count is 0, and the active generation's count matches step 4.

## Step 9 — Strict mode, then resume the scheduler

`strict` rejects a profile mismatch at read time instead of logging it. Switch only once the
space serves from its generation — under `observe` a mismatch degrades answers silently, which
is the failure this whole mechanism exists to prevent.

**The service and every job must move together.** A job left on `observe` keeps writing under
the condition strict exists to reject.

```bash
gcloud run services update databridge --project "$PROJECT" --region "$REGION" \
  --update-env-vars DATABRIDGE_PROFILE_MODE=strict

for JOB in databridge-migrate databridge-ingest databridge-confluence-ingest databridge-generation; do
  gcloud run jobs update "$JOB" --project "$PROJECT" --region "$REGION" \
    --update-env-vars DATABRIDGE_PROFILE_MODE=strict
done
```

Verify everything mechanically, in **one function that returns non-zero on any failure** — a
`for` loop over jobs reports the status of its *last* iteration, so an early failure would be
hidden by a later success.

```bash
strict_preflight_ok() {
  # Service and all four jobs on strict, with no permanent generation override anywhere.
  preflight strict || return 1

  # The service must actually answer under strict — a preflight failure shows at startup.
  curl -sf -X POST "$SERVICE_URL/ask" -H 'Content-Type: application/json' \
    -d '{"question":"<a question you know this corpus answers>"}' >/dev/null || return 1

  # The active generation must be the one this rollout activated, with no legacy left.
  run_generation report --space "$SPACE" >/tmp/report.out || return 1
  show_output /tmp/report.out > /tmp/report.body || return 1
  preflight report --input /tmp/report.body --expected-generation "${GENERATION:?}" || return 1
  echo "strict preflight OK"
}
```

`strict_preflight_ok` already includes the active-generation and legacy-count checks, so a wrong
active id or leftover legacy rows block the resume rather than merely printing.

The field names come from the code, not from guesswork: `report` prints
`legacy_null_generation_chunks` at the top level and a `generations` list whose rows carry
`generation_id`, `state` and `chunk_count` (`scripts/generation.py` → `_reference_report`,
`PgVectorStore.generation_report`). 

The permanent-override check is a cheap guard against operational drift — a stray `jobs update`,
a leftover setting, a runbook that disagreed with what was actually run. The step-4 override is
execution-scoped and does not persist (measured), so these assertions should always pass.

Resume **only as a consequence of the check**, in one chained command:

```bash
strict_preflight_ok && \
gcloud scheduler jobs resume databridge-confluence-ingest --project "$PROJECT" --location "$REGION"

[ "$(gcloud scheduler jobs describe databridge-confluence-ingest \
  --project "$PROJECT" --location "$REGION" --format='value(state)')" = "ENABLED" ] \
  && echo "scheduler ENABLED" || echo "STOP: scheduler did not resume"
```

Resuming earlier would let a batch change active chunks and the manifest while step 7's golden
verification is still running.

---

## If you are interrupted

Safe to pause between any two steps. **Not** safe to leave paused between steps 2 and 6 for
long: the space serves no evidence in that window.

- **Before step 4 starts** — the building generation is inert: one `space_generation` row and
  no chunks. Moving traffic back to the old revision is safe, and legacy rows serve again.
- **After step 4 starts, before step 6** — **do not roll traffic back.** Once the first
  `replace_source` commits, the space holds non-null-generation chunks and the old
  space-only reader would return legacy and building rows together. The only exception is
  when you have confirmed the space holds **zero** non-null-generation chunks:

  Establishing that requires quiescence, not a single report. Do all of it, in order:

  ```bash
  # 1. Stop every writer first, or the answer is stale before you read it.
  gcloud scheduler jobs pause databridge-confluence-ingest \
    --project "$PROJECT" --location "$REGION"

  # 2. Terminate the ingest execution that failed, if it is still running.
  gcloud run jobs executions list --job databridge-confluence-ingest \
    --project "$PROJECT" --region "$REGION" --limit 5 \
    --format='table(metadata.name, status.runningCount, status.completionTime)'
  # cancel any still-running execution before continuing:
  #   gcloud run jobs executions cancel <NAME> --project "$PROJECT" --region "$REGION"

  # 3. Only now read the state, and read both views.
  run_generation inventory --space "$SPACE" --generation-id "${GENERATION:?}" \
    | tee /tmp/inv.out && show_output /tmp/inv.out      # total_chunks must be 0
  run_generation report --space "$SPACE" \
    | tee /tmp/report.out && show_output /tmp/report.out # every generation's count
  ```

  Roll traffic back only if `total_chunks` is 0 **and** no generation holds chunks, and only
  while writers stay stopped — the state you read is only true as long as nothing writes. If a
  batch runs between the read and the rollback, the old reader will see building rows again.

  Otherwise leave the generation in place, or discard it on the next attempt with
  `--discard-inflight`.
- **After step 6, before step 8** — the new generation serves and legacy rows remain as
  evidence. Nothing is lost. Resume at step 7.
- **After step 8** — legacy rows are gone. Recovery is a new generation, or PITR.

## Not covered here

- Rolling back the **schema**. An image rollback is not a schema rollback: old writers fail
  against the migrated schema.
- A second cutover. `delete-legacy` is first-cutover-only; later rebuilds replace one generation
  with another and never touch legacy rows.
