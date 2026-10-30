# Running AvailabilityIQ in production

The model is a weekly batch: every Monday it scores each player's chance of
becoming unavailable in the next seven days. The production setup is built
around that one fact. A scheduled job runs the pipeline and publishes a
predictions file; an API and the squad page read the file; the monitoring
says when the file stopped arriving. Nothing scores on request, because
nothing changes between Mondays that the model could see.

```
                        ┌─────────────────────────────┐
   Mondays 05:00  ──▶   │  CronJob: aiq weekly         │
                        │  export → panel → features   │
                        │  → predict → publish         │
                        └──────────────┬──────────────┘
                                       │ predictions.parquet + manifest.json
                                       ▼
                        ┌─────────────────────────────┐
                        │  artefact volume (RWX)       │◀──── history/ for rollback
                        └───────┬─────────────┬───────┘
                        read-only│             │read-only
                                 ▼             ▼
                      ┌──────────────┐  ┌──────────────┐
   physio / dashboard │  API         │  │  squad page  │
        ──────────▶   │  FastAPI     │  │  Streamlit   │
                      │  /v1/...     │  │              │
                      └──────┬───────┘  └──────────────┘
                             │ /metrics
                             ▼
                      Prometheus → alert rules → runbook
```

## The pieces

| Piece | Where | What it does |
|---|---|---|
| Scoring job | `CronJob`, `aiq weekly` | Runs the pipeline stages in order and ends with `aiq publish`, which validates the output against the file contract and installs it atomically |
| Artefact volume | `PersistentVolumeClaim` | The hand-off. `ReadWriteMany` in production (EFS), a single-node claim in CI |
| API | `Deployment` + `Service`, `aiq-api` | Loads the file, answers squad and player questions with the base rate and exact contributions, reloads when the manifest changes, reports its freshness |
| Squad page | `Deployment` + `Service`, Streamlit | The same file, for a person in a Monday meeting |
| Monitoring | `ServiceMonitor`, `PrometheusRule`, Grafana `ConfigMap` | Ten alert rules with a runbook entry each; a dashboard whose first panel is the predictions' age |
| Chart | `deploy/chart/availabilityiq` | All of the above, hardened by default, with `values-ci.yaml` and `values-prod.example.yaml` |
| Cloud | `deploy/terraform/aws` | VPC, EKS, EFS, S3, IAM through Pod Identity. Validated and linted in CI, not applied |

## The API

```
GET /health                       liveness: the process answers
GET /ready                        readiness: 503 until a valid predictions file is loaded
GET /metrics                      Prometheus
GET /v1/model                     version, hash, rows, weeks, honest metrics, what it cannot see
GET /v1/teams                     squads in the file
GET /v1/weeks                     Mondays in the file
GET /v1/squads/{team_id}/risk?week=YYYY-MM-DD&shortlist=5&top=5
GET /v1/squads/{team_id}/weeks
GET /v1/players/{player_id}/risk?week=YYYY-MM-DD
```

A squad answer is ranked, every risk comes with `vs_base_rate`, the top
`shortlist` players are flagged, and each player carries the largest
contributions to their log-odds, labelled in words. A week that is not a
Monday is a 422; a squad or week the file does not hold is a 404.

## The hand-off, exactly

`aiq publish --source artifacts --destination /srv/artifacts` does, in order:

1. Reads `predictions.parquet` and checks the contract: every required
   column, risks in `[0, 1]`, labels in `{0, 1}`, every `monday` a Monday,
   no duplicate player-weeks, integer ids. Any problem is refused with the
   full list, and nothing is touched.
2. Copies the current files to `history/<published_at>-<sha>/`, keeping
   three.
3. Writes the new file under a temporary name and renames it into place.
4. Writes `manifest.json` last: rows, week range, base rate, mean risk,
   SHA-256 of the file, the version of the code that produced it.

The API polls the manifest's modification time (`api.reloadSeconds`). A new
manifest triggers a reload; a file that fails to load leaves the previous
frame in place and increments `aiq_predictions_reloads_total{result="failed"}`,
which is an alert. `aiq rollback` reinstates the newest history entry.

## Running it

**Locally, with synthetic data** (no corpus needed):

```bash
pip install -e ".[dev]"
aiq smoke-data --out artifacts/smoke
aiq publish --source artifacts/smoke --destination artifacts/published
cp artifacts/smoke/teams.parquet artifacts/published/
AIQ_ARTIFACT_DIR=artifacts/published AIQ_TEAMS_PATH=artifacts/published/teams.parquet aiq-api
```

**Locally, with real predictions:** run the pipeline as the project README
describes, then `aiq publish` and `make api`.

**On a cluster:**

```bash
helm install aiq deploy/chart/availabilityiq -n aiq --create-namespace -f my-values.yaml
kubectl -n aiq create job --from=cronjob/aiq-availabilityiq-scoring first-run   # or wait for Monday
helm test aiq -n aiq
```

The API is not ready until the first publish; that is deliberate (P-003).

## What CI verifies, and where

| Check | Job | Real? |
|---|---|---|
| ruff, mypy strict, the test suite | `checks` | yes |
| `helm lint`; rendered manifests against the Kubernetes and operator CRD schemas; `promtool check rules` and the rule unit tests; the dashboard JSON | `chart` | yes |
| `terraform validate` against pinned modules; `tflint`; Trivy configuration scan (reported) | `terraform` | validation only, no plan against an account |
| The image builds, starts with no data, answers `/health` and refuses `/ready` | `image` | yes |
| On a kind cluster with the Prometheus Operator: the chart installs, the job runs and publishes, the API and the page become ready, `helm test` passes, the API answers through its service, Prometheus scrapes the target and loads both rule groups, a second publish is picked up without a restart | `e2e` | yes, on a one-node cluster |

What a kind cluster cannot show: real load balancers and TLS, `ReadWriteMany`
storage, IAM, multi-node scheduling, and NetworkPolicy enforcement (kind's
default CNI ignores policies; they are checked by schema only). Those are
the parts the Terraform and the production values describe and that the
first real environment will exercise.

## Security defaults

Every pod runs as uid 10001 with a read-only root filesystem, all
capabilities dropped, the runtime seccomp profile, and no service-account
token mounted. The readers may talk to DNS and nothing else; the job may
reach out, because the export stage fetches. The image has no shell
installer, no pip, no setuptools. Secrets for the export stage come from a
`Secret` referenced in `scoring.envFrom`, never from values.

## Documents

- [RUNBOOK.md](RUNBOOK.md): one entry per alert, what it means and what to do.
- [SLO.md](SLO.md): what is promised, how it is measured, what is not promised.
- [DECISIONS.md](DECISIONS.md): the choices behind this setup and their costs.
- [terraform/aws/README.md](terraform/aws/README.md): the cloud footprint and its cost.
- [chart/availabilityiq/values.yaml](chart/availabilityiq/values.yaml): every knob, commented.
