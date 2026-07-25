#!/usr/bin/env bash
# One-time CI/CD infrastructure for the cloudbuild.yaml pipeline — kept in the repo
# so the trigger and build identity are reproducible, not console-only state
# (review: Codex #2 / Antigravity #3). Safe to re-run; every step is idempotent or
# guarded. Run as a project owner:
#
#   PROJECT=genaiacademy-ph bash scripts/setup_cicd.sh
#
# Prereq once per project: the Cloud Build P4SA needs Secret Manager admin to store
# the GitHub connection token:
#   gcloud projects add-iam-policy-binding "$PROJECT" \
#     --member="serviceAccount:service-$(gcloud projects describe "$PROJECT" \
#       --format='value(projectNumber)')@gcp-sa-cloudbuild.iam.gserviceaccount.com" \
#     --role=roles/secretmanager.admin --condition=None
set -euo pipefail

PROJECT=${PROJECT:-genaiacademy-ph}
REGION=us-central1
BUILD_SA=databridge-build@${PROJECT}.iam.gserviceaccount.com
RUNTIME_SA=databridge-run@${PROJECT}.iam.gserviceaccount.com
SCHEDULER_SA=databridge-scheduler@${PROJECT}.iam.gserviceaccount.com
REPO_URI=https://github.com/ParkHwan/Data-Bridge.git

gcloud services enable run.googleapis.com secretmanager.googleapis.com \
  cloudscheduler.googleapis.com --project "$PROJECT"

# 1) Dedicated least-privilege build SA — never the default compute SA, which may
#    hold roles/editor (review: Codex #2).
gcloud iam service-accounts describe "$BUILD_SA" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud iam service-accounts create databridge-build --project "$PROJECT" \
    --display-name "Data-Bridge CI/CD (Cloud Build)"

# 2) Minimal grants: push images, deploy the service + run the migrate job,
#    write logs, act as the runtime SA. No roles/editor, no run.admin.
for role in roles/artifactregistry.writer roles/run.developer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:$BUILD_SA" --role "$role" \
    --condition=None --format=none
done
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --project "$PROJECT" \
  --member "serviceAccount:$BUILD_SA" --role roles/iam.serviceAccountUser --format=none

# 3) Migration Cloud Run job (schema-before-code; executed by the pipeline's
#    `migrate` step). Reuses the service's image, Cloud SQL wiring, and DSN.
if ! gcloud run jobs describe databridge-migrate --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
  IMAGE=$(gcloud run services describe databridge --project "$PROJECT" --region "$REGION" \
    --format='value(spec.template.spec.containers[0].image)')
  DSN=$(gcloud run services describe databridge --project "$PROJECT" --region "$REGION" \
    --format=export | grep -A1 'name: DATABRIDGE_DSN' | tail -1 | sed 's/.*value: //')
  gcloud run jobs create databridge-migrate --project "$PROJECT" --region "$REGION" \
    --image "$IMAGE" \
    --command python --args scripts/migrate.py \
    --set-cloudsql-instances "${PROJECT}:${REGION}:databridge-demo" \
    --set-env-vars "DATABRIDGE_DSN=${DSN}" \
    --service-account "$RUNTIME_SA" \
    --max-retries 0
fi

# 4) Scheduled Confluence ingestion. The self-authored Confluence corpus must use a
#    dedicated space key (for example CONF_DEMO) because a successful full run garbage
#    collects sources absent from the configured folder.
if ! gcloud secrets describe CONFLUENCE_API_TOKEN --project "$PROJECT" >/dev/null 2>&1; then
  echo "Create Secret Manager secret CONFLUENCE_API_TOKEN before provisioning the ingest job." >&2
  exit 1
fi
: "${CONFLUENCE_BASE_URL:?Set CONFLUENCE_BASE_URL for the Confluence Cloud site}"
: "${CONFLUENCE_EMAIL:=}"
: "${FOLDER_ID:?Set FOLDER_ID for the full-folder ingest scope}"
: "${SPACE_KEY:?Set SPACE_KEY to a dedicated key such as CONF_DEMO}"

gcloud secrets add-iam-policy-binding CONFLUENCE_API_TOKEN --project "$PROJECT" \
  --member "serviceAccount:$RUNTIME_SA" --role roles/secretmanager.secretAccessor \
  --condition=None --format=none

IMAGE=$(gcloud run services describe databridge --project "$PROJECT" --region "$REGION" \
  --format='value(spec.template.spec.containers[0].image)')
DSN=$(gcloud run services describe databridge --project "$PROJECT" --region "$REGION" \
  --format=export | grep -A1 'name: DATABRIDGE_DSN' | tail -1 | sed 's/.*value: //')
if ! gcloud run jobs describe databridge-confluence-ingest \
  --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
  gcloud run jobs create databridge-confluence-ingest \
    --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
    --command python --args scripts/ingest_confluence.py \
    --set-cloudsql-instances "${PROJECT}:${REGION}:databridge-demo" \
    --set-env-vars "DATABRIDGE_DSN=${DSN},DATABRIDGE_EMBEDDER=vertex,CONFLUENCE_BASE_URL=${CONFLUENCE_BASE_URL},CONFLUENCE_EMAIL=${CONFLUENCE_EMAIL},FOLDER_ID=${FOLDER_ID},SPACE_KEY=${SPACE_KEY}" \
    --set-secrets "CONFLUENCE_API_TOKEN=CONFLUENCE_API_TOKEN:latest" \
    --service-account "$RUNTIME_SA" --max-retries 0 --task-timeout 3600s
else
  gcloud run jobs update databridge-confluence-ingest \
    --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
    --command python --args scripts/ingest_confluence.py \
    --set-cloudsql-instances "${PROJECT}:${REGION}:databridge-demo" \
    --set-env-vars "DATABRIDGE_DSN=${DSN},DATABRIDGE_EMBEDDER=vertex,CONFLUENCE_BASE_URL=${CONFLUENCE_BASE_URL},CONFLUENCE_EMAIL=${CONFLUENCE_EMAIL},FOLDER_ID=${FOLDER_ID},SPACE_KEY=${SPACE_KEY}" \
    --set-secrets "CONFLUENCE_API_TOKEN=CONFLUENCE_API_TOKEN:latest" \
    --service-account "$RUNTIME_SA" --max-retries 0 --task-timeout 3600s
fi

gcloud iam service-accounts describe "$SCHEDULER_SA" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud iam service-accounts create databridge-scheduler --project "$PROJECT" \
    --display-name "Data-Bridge Confluence scheduler"
gcloud run jobs add-iam-policy-binding databridge-confluence-ingest \
  --project "$PROJECT" --region "$REGION" \
  --member "serviceAccount:$SCHEDULER_SA" --role roles/run.invoker --format=none

JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/databridge-confluence-ingest:run"
if ! gcloud scheduler jobs describe databridge-confluence-ingest \
  --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs create http databridge-confluence-ingest \
    --project "$PROJECT" --location "$REGION" --schedule '0 6 * * *' \
    --time-zone Asia/Seoul --uri "$JOB_URI" --http-method POST \
    --oauth-service-account-email "$SCHEDULER_SA"
fi

# 5) GitHub connection → repository link → trigger on main.
#    First run prints an authorization URL — complete it in the browser (as the
#    GitHub repo owner), then re-run this script to finish the remaining steps.
gcloud builds connections describe databridge-github --project "$PROJECT" --region "$REGION" >/dev/null 2>&1 ||
  gcloud builds connections create github databridge-github --project "$PROJECT" --region "$REGION"

gcloud builds repositories describe Data-Bridge --project "$PROJECT" --region "$REGION" \
  --connection databridge-github >/dev/null 2>&1 ||
  gcloud builds repositories create Data-Bridge --project "$PROJECT" --region "$REGION" \
    --connection databridge-github --remote-uri "$REPO_URI"

gcloud builds triggers describe deploy-main --project "$PROJECT" --region "$REGION" >/dev/null 2>&1 ||
  gcloud builds triggers create github --name deploy-main --project "$PROJECT" --region "$REGION" \
    --repository "projects/$PROJECT/locations/$REGION/connections/databridge-github/repositories/Data-Bridge" \
    --branch-pattern '^main$' \
    --build-config cloudbuild.yaml \
    --service-account "projects/$PROJECT/serviceAccounts/$BUILD_SA"

echo "CI/CD infrastructure ready: SA=$BUILD_SA trigger=deploy-main (^main$)"
