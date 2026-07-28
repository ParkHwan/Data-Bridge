# Embedding provenance rollout (v0.2.8)

This runbook moves one production space from unlabelled or old vectors to a verified
generation before enforcement becomes strict. Replace shell placeholders deliberately;
do not copy a generation id between spaces.

## Safety order

1. Refuse a deployment where `DATABRIDGE_PROFILE_MODE` is missing. Set the service and
   both ingest jobs to `observe` before deploying the provenance-aware image:

   ```sh
   gcloud run services update databridge --region us-central1 \
     --update-env-vars DATABRIDGE_PROFILE_MODE=observe
   gcloud run jobs update databridge-ingest --region us-central1 \
     --update-env-vars DATABRIDGE_PROFILE_MODE=observe
   gcloud run jobs update databridge-confluence-ingest --region us-central1 \
     --update-env-vars DATABRIDGE_PROFILE_MODE=observe
   ```

2. Pause the scheduler before creating a rebuild target. This prevents a normal active
   generation batch from racing the clean build:

   ```sh
   gcloud scheduler jobs pause databridge-confluence-ingest \
     --location us-central1
   ```

3. Create a `building` generation with the same environment as the ingest job. Record
   the printed id as `BUILDING_GENERATION_ID`:

   ```sh
   uv run python - <<'PY'
   import os
   from databridge.embed import resolve_embedder
   from databridge.store import PgVectorStore, resolve_profile_mode

   embedder = resolve_embedder()
   store = PgVectorStore(
       os.environ["DATABRIDGE_DSN"],
       profile=embedder.profile,
       mode=resolve_profile_mode(),
   )
   generation = store.create_building_generation(space_key=os.environ["SPACE_KEY"])
   print(generation.generation_id)
   PY
   ```

4. Point only the stopped scheduler's job at that generation, then run one clean full
   ingest manually. Do not resume the scheduler yet:

   ```sh
   gcloud run jobs update databridge-confluence-ingest --region us-central1 \
     --update-env-vars DATABRIDGE_GENERATION_ID="$BUILDING_GENERATION_ID"
   gcloud run jobs execute databridge-confluence-ingest --region us-central1 --wait
   ```

5. Verify all gates before activation. The batch must have succeeded and its completion
   log must name `BUILDING_GENERATION_ID`. First require one profile for the target,
   a stored fingerprint equal to the runtime fingerprint reported by the batch, and the
   expected chunk population:

   ```sql
   SELECT g.generation_id, g.state, count(c.id) AS chunks,
          count(DISTINCT g.profile_id) AS distinct_profiles,
          p.config_fingerprint
   FROM space_generation g
   JOIN embedding_profile p ON p.profile_id = g.profile_id
   LEFT JOIN chunks c
     ON c.space_key = g.space_key AND c.generation_id = g.generation_id
   WHERE g.space_key = :'space_key' AND g.generation_id = :'generation_id'
   GROUP BY g.generation_id, g.state, p.config_fingerprint;
   ```

   Run the MFS golden corpus with the same runtime embedder configuration. Activation is
   blocked unless the batch, SQL checks, fingerprint comparison, and golden run all pass:

   ```sh
   env -u PYTHONPATH uv run python scripts/run_golden.py
   ```

   A clean generation does not relabel or overwrite legacy `NULL` rows. Only after the
   clean batch and golden checks pass, remove those unprovable rows under the space lock,
   then require the count to be zero:

   ```sql
   BEGIN;
   SELECT pg_advisory_xact_lock(
     hashtextextended('databridge:embedding-profile:' || :'space_key', 0)
   );
   DELETE FROM chunks
   WHERE space_key = :'space_key' AND generation_id IS NULL;
   COMMIT;

   SELECT count(*) AS null_generation_chunks
   FROM chunks
   WHERE space_key = :'space_key' AND generation_id IS NULL;
   ```

6. Activate exactly the verified building generation. The store retires the prior active
   generation and activates the target atomically under the space advisory lock:

   ```sh
   uv run python - <<'PY'
   import os
   from databridge.embed import resolve_embedder
   from databridge.store import PgVectorStore, resolve_profile_mode

   embedder = resolve_embedder()
   store = PgVectorStore(
       os.environ["DATABRIDGE_DSN"],
       profile=embedder.profile,
       mode=resolve_profile_mode(),
   )
   store.activate_generation(
       space_key=os.environ["SPACE_KEY"],
       generation_id=int(os.environ["BUILDING_GENERATION_ID"]),
   )
   PY
   ```

7. Switch the service to `strict`; the environment update creates a new revision. Confirm
   startup preflight succeeds before restoring scheduled writes:

   ```sh
   gcloud run services update databridge --region us-central1 \
     --update-env-vars DATABRIDGE_PROFILE_MODE=strict
   gcloud run jobs update databridge-confluence-ingest --region us-central1 \
     --remove-env-vars DATABRIDGE_GENERATION_ID \
     --update-env-vars DATABRIDGE_PROFILE_MODE=strict
   gcloud scheduler jobs resume databridge-confluence-ingest \
     --location us-central1
   ```

## Rollback boundary

Do not delete the previous generation. Roll back if strict startup fails, `/ask` returns
the sanitized provenance 503, the post-activation golden run regresses, or the active
generation report differs from the verified build. Pause the scheduler first, return the
service to `observe`, and atomically restore the preserved generation while holding the
same space lock used by the application:

```sql
BEGIN;
SELECT pg_advisory_xact_lock(
  hashtextextended('databridge:embedding-profile:' || :'space_key', 0)
);
UPDATE space_generation
SET state = 'retired'
WHERE space_key = :'space_key' AND state = 'active';
UPDATE space_generation
SET state = 'active', activated_at = now()
WHERE space_key = :'space_key'
  AND generation_id = :'previous_generation_id'
  AND state = 'retired';
COMMIT;
```

Require the final `UPDATE` to affect exactly one row. Re-run the report and golden checks
before resuming the scheduler. Generation and profile rows are audit/rollback state and
must remain even when their chunk count reaches zero.
