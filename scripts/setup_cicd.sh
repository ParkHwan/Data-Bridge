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
REPO_URI=https://github.com/ParkHwan/Data-Bridge.git

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

# 4) GitHub connection → repository link → trigger on main.
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
