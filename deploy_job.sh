#!/bin/bash
# Template deployment: local Docker build -> Artifact Registry -> Job -> Scheduler.
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-ggn-nmfs-swfscerddap-dev-1}"
REGION="${REGION:-us-east4}"
REPO_NAME="${REPO_NAME:-ncep-cloud-run}"
IMAGE_NAME="ncep-gdas"
TAG="${TAG:-v0.1.0}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-swfscerddap-run-sa1@${PROJECT_ID}.iam.gserviceaccount.com}"
JOB_MODE="${JOB_MODE:-staging}"
ENABLE_SCHEDULE="${ENABLE_SCHEDULE:-false}"
ERDDAP_BASE_URL="${ERDDAP_BASE_URL:-https://155.206.244.135/erddap}"
ERDDAP_INSECURE="${ERDDAP_INSECURE:-true}"
FLAG_SECRET="${FLAG_SECRET:-ncep-erddap-flag-keys}"
case "$JOB_MODE" in
  staging) JOB_NAME="ncep-gdas-staging"; PREFIX="_staging/ncep-gdas" ;;
  production) JOB_NAME="ncep-gdas-processor"; PREFIX="satellite/ncep_gdas" ;;
  *) echo "JOB_MODE must be staging or production" >&2; exit 1 ;;
esac
SCHEDULER_NAME="ncep-gdas-daily"
IMAGE_PATH="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${IMAGE_NAME}:${TAG}"
for tool in gcloud docker; do command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; exit 1; }; done
cd "$(dirname "$0")"
if [[ "$JOB_MODE" == production ]]; then
  gcloud secrets versions describe latest --secret "$FLAG_SECRET" --location "$REGION" --project "$PROJECT_ID" >/dev/null
fi
docker buildx build --platform linux/amd64 -t "$IMAGE_PATH" --load .
if ! gcloud artifacts repositories describe "$REPO_NAME" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPO_NAME" --repository-format docker --location "$REGION" --project "$PROJECT_ID"
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker push "$IMAGE_PATH"
envs="JOB_MODE=${JOB_MODE},BUCKET=erddap-swfsc-erd,PREFIX=${PREFIX},SEED_PREFIX=satellite/ncep_gdas"
secrets=(--clear-secrets)
if [[ "$JOB_MODE" == production ]]; then
  envs+=",ERDDAP_BASE_URL=${ERDDAP_BASE_URL},ERDDAP_INSECURE=${ERDDAP_INSECURE},ERDDAP_FLAG_SECRET=projects/${PROJECT_ID}/locations/${REGION}/secrets/${FLAG_SECRET}/versions/latest"
  secrets=(--clear-secrets)
fi
gcloud run jobs deploy "$JOB_NAME" --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE_PATH" --service-account "$SERVICE_ACCOUNT" --tasks 1 --parallelism 1 --args= \
  --max-retries 1 --task-timeout 3600s --memory 2Gi --cpu 1 \
  --set-env-vars "$envs" ${secrets[@]+"${secrets[@]}"}
if [[ "$JOB_MODE" == production ]]; then
  number="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  scheduler_args=(--project "$PROJECT_ID" --location "$REGION" --schedule '37 9 * * *' \
    --time-zone America/Los_Angeles \
    --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${number}/jobs/${JOB_NAME}:run" \
    --http-method POST --oauth-service-account-email "$SERVICE_ACCOUNT" \
    --oauth-token-scope https://www.googleapis.com/auth/cloud-platform)
  if gcloud scheduler jobs describe "$SCHEDULER_NAME" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
    # Pause an existing trigger before updating it.
    gcloud scheduler jobs pause "$SCHEDULER_NAME" --project "$PROJECT_ID" --location "$REGION"
    gcloud scheduler jobs update http "$SCHEDULER_NAME" "${scheduler_args[@]}"
  else
    gcloud scheduler jobs create http "$SCHEDULER_NAME" "${scheduler_args[@]}"
    gcloud scheduler jobs pause "$SCHEDULER_NAME" --project "$PROJECT_ID" --location "$REGION"
  fi
  if [[ "$ENABLE_SCHEDULE" == true ]]; then
    gcloud scheduler jobs resume "$SCHEDULER_NAME" --project "$PROJECT_ID" --location "$REGION"
  fi
fi
echo "Deployed ${JOB_NAME}. Schedule enabled: ${ENABLE_SCHEDULE}."
