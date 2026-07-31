"""Static contracts for the generation management job and deployment ordering."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_cloudbuild_updates_all_jobs_between_migration_and_deploy() -> None:
    config = yaml.safe_load((ROOT / "cloudbuild.yaml").read_text("utf-8"))
    steps = config["steps"]
    ids = [step["id"] for step in steps]
    assert ids.index("migrate") < ids.index("update-jobs") < ids.index("deploy")
    assert "update-confluence-job" not in ids

    update = next(step for step in steps if step["id"] == "update-jobs")
    script = update["args"][-1]
    for job in (
        "databridge-confluence-ingest",
        "databridge-generation",
        "databridge-ingest",
    ):
        assert job in script
    assert "is not provisioned; run scripts/setup_cicd.sh" in script
    assert "@$${DIGEST}" in script


def test_setup_defines_generation_identity_bucket_and_read_only_job() -> None:
    script = (ROOT / "scripts" / "setup_cicd.sh").read_text("utf-8")
    assert "GENERATION_SA=databridge-generation@" in script
    assert "role in roles/cloudsql.client roles/aiplatform.user" in script
    assert 'add-iam-policy-binding "$GENERATION_SA"' in script
    assert 'serviceAccount:$BUILD_SA" --role roles/iam.serviceAccountUser' in script
    assert 'serviceAccount:$GENERATION_SA" --role roles/secretmanager.secretAccessor' in script
    assert 'serviceAccount:$GENERATION_SA" --role roles/storage.objectViewer' in script
    assert 'serviceAccount:$GENERATION_SA" --role roles/logging.logWriter' not in script
    assert 'serviceAccount:$GENERATION_SA" --role roles/bigquery.jobUser' not in script
    assert (
        'CONFLUENCE_API_TOKEN --project "$PROJECT" \\\n'
        '  --member "serviceAccount:$GENERATION_SA"' not in script
    )

    assert "--uniform-bucket-level-access --public-access-prevention" in script
    assert "--versioning" in script
    assert "gcloud run jobs create databridge-generation" in script
    assert "--command python,scripts/generation.py" in script
    assert '--command python,scripts/generation.py --args=""' in script
    assert "--clear-args" not in script
    assert "DATABRIDGE_EMBEDDER=vertex,DATABRIDGE_PROFILE_MODE=observe" in script
    assert "GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION}" in script
    assert "name=queries,type=cloud-storage,bucket=${QUERY_BUCKET},readonly=true" in script
    assert "volume=queries,mount-path=/queries" in script
    assert "--max-retries 0 --tasks 1 --parallelism 1 --task-timeout 3600s" in script


def test_generation_wrapper_is_the_observable_standard_path() -> None:
    wrapper = (ROOT / "scripts" / "run_generation_job.sh").read_text("utf-8")
    assert "set -euo pipefail" in wrapper
    assert 'exec uv run python -m databridge.generation_job "$@"' in wrapper


def test_obsolete_rollout_document_contains_no_executable_fallback() -> None:
    document = (ROOT / "docs" / "operations" / "embedding-provenance-rollout.md").read_text("utf-8")
    assert "obsolete" in document.lower()
    assert "PR-A (#24)" in document
    assert "forward-only" in document
    assert "raw SQL" in document
    assert "```" not in document
    assert "gcloud " not in document
    assert "UPDATE space_generation" not in document
