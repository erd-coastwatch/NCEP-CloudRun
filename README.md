# NCEP-CloudRun

Updates ERDDAP dataset `ncep_gdas_global` from NOAA PSL’s NCEP GDAS daily surface data.

Processes eight variables: `uwnd.sfc`, `vwnd.sfc`, `slp`, `air.sfc`, `pr_wtr`, `rhum.sfc`, `srfp`, and `trpp`.

Annual files are stored at:

```text
gs://erddap-swfsc-erd/satellite/ncep_gdas/ncep_gdas_surface_YEAR.nc
```

## Repository

```text
config/requirements.txt       Python dependencies
scripts/process.py            Data retrieval, processing, upload, and ERDDAP flagging
templates/ncep_winds_slp.cdl   Output NetCDF schema
Dockerfile                    Container build
deploy_job.sh                 Image, job, and scheduler deployment
```

Docker generates an empty NetCDF template from the CDL during the build. The template preserves the existing 73 × 144 grid, metadata, six packed short variables, and two float variables.

## Processing

Each execution updates the current and previous UTC years. It downloads the existing annual files, retrieves missing days, and refreshes the final two existing days.

The processor checks coordinates, timestamps, units, and packing before uploading. Source values are explicitly decoded and repacked into the existing output schema. Invalid source timestamps are repaired only when the remaining timestamps agree with the expected daily calendar.

Uploads use GCS generation checks to prevent concurrent executions from overwriting each other’s changes. Avoid overlapping runs.

After processing, the production job requests an ERDDAP reload. A failed update or rejected flag request causes the execution to fail. Flagging is retried on subsequent executions, including runs with no data changes.

## Production deployment

Deployment follows the existing Cloud Run template: local Docker buildx for `linux/amd64`, push to Artifact Registry, then deploy the job and scheduler.

From the repository root:

```bash
JOB_MODE=production ENABLE_SCHEDULE=false bash deploy_job.sh
```

Run the production job:

```bash
gcloud run jobs execute ncep-gdas-processor \
  --project ggn-nmfs-swfscerddap-dev-1 \
  --region us-east4 \
  --wait
```

Deployment settings:

| Setting | Value |
| --- | --- |
| Project | `ggn-nmfs-swfscerddap-dev-1` |
| Region | `us-east4` |
| Artifact Registry repository | `ncep-cloud-run` |
| Image | `ncep-gdas:v0.1.0` |
| Production job | `ncep-gdas-processor` |
| Runtime account | `swfscerddap-run-sa1@ggn-nmfs-swfscerddap-dev-1.iam.gserviceaccount.com` |
| Scheduler | `ncep-gdas-daily` |

The regional secret `ncep-erddap-flag-keys` must exist in `us-east4`, contain a JSON mapping from `ncep_gdas_global` to its flag key, and grant the runtime account Secret Manager access.

The ERDDAP endpoint defaults to `https://155.206.244.135/erddap`, with `ERDDAP_INSECURE=true` to support its certificate settings. Override these environment variables when using a hostname with a valid certificate.

## Daily schedule

The scheduler runs daily at **09:00 America/Los_Angeles**. Production deployment leaves it paused unless `ENABLE_SCHEDULE=true`.

After verifying the production execution and ERDDAP data:

```bash
gcloud scheduler jobs resume ncep-gdas-daily \
  --project ggn-nmfs-swfscerddap-dev-1 \
  --location us-east4
```

The scheduler’s service account requires permission to invoke the production job.

For later deployments that should leave scheduling enabled:

```bash
JOB_MODE=production ENABLE_SCHEDULE=true bash deploy_job.sh
```

## Historical refresh

To refresh all available records for a specific year:

```bash
gcloud run jobs execute ncep-gdas-processor \
  --project ggn-nmfs-swfscerddap-dev-1 \
  --region us-east4 \
  --args='--start-year=2026,--end-year=2026,--refresh-days=366' \
  --wait
```

The argument override applies only to that execution. Regular executions continue updating the current and previous years with a two-day refresh.

Older records outside the refresh window are retained. The processor does not delete historical annual files.

## Staging

The deployment script defaults to staging when `JOB_MODE` is omitted:

```bash
JOB_MODE=staging bash deploy_job.sh
```

Staging uses job `ncep-gdas-staging` and writes to:

```text
gs://erddap-swfsc-erd/_staging/ncep-gdas/
```

Existing production files seed staging when no staged annual file exists. Staging does not flag production ERDDAP or create a scheduler.