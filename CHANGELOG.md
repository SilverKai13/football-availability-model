# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-10-31

The production setup. The model and the pipeline are unchanged; what is new
is everything needed to run the weekly scoring on a cluster and serve the
result.

### Added

- `aiq.serve`: a FastAPI service over the published predictions, with
  honest readiness (503 until a valid file is loaded), hot reload on a new
  manifest, Prometheus metrics including the predictions' age and version,
  structured JSON logs with request ids, and a model card at `/v1/model`.
- `aiq publish`, `aiq rollback`: the hand-off between the weekly job and the
  readers. A file contract that is checked before anything is installed, an
  atomic rename, a manifest written last, and `history/` for a one-command
  rollback.
- `aiq weekly`: the scheduled run, every stage in order and publish last.
- `aiq smoke-data`: synthetic artefacts that pass the contract, for
  deployment checks that must not carry real players.
- A Helm chart: API, page, CronJob, shared volume, HPA, disruption budget,
  topology spread, NetworkPolicy, ServiceMonitor, ten alert rules with
  promtool unit tests, a Grafana dashboard, a `helm test`, and hardened
  defaults.
- Terraform for AWS: VPC, EKS, EFS, S3, IAM through Pod Identity. Validated,
  linted and scanned in CI; not applied, by decision.
- A container image: multi-stage, digest-pinned base, non-root, no pip or
  setuptools at runtime, one image for the API, the page and the job.
- CI: the quality gate, chart checks, Terraform checks, an image build and
  start-up check, and an end-to-end deployment on a kind cluster with the
  Prometheus Operator, including a scrape and rule check.
- Documentation under `deploy/`: overview, runbook, service levels,
  decisions, cloud footprint.

### Changed

- The Streamlit page reads `AIQ_ARTIFACT_DIR` and `AIQ_TEAMS_PATH`, so it
  can share a volume with the API; the defaults are unchanged.
- Version 0.2.0; `aiq` and `aiq-api` are console scripts.

## [0.1.0] - 2026-08-18

The model: data audit, crosswalk, panel, features, honest forward-chained
evaluation, calibration, the squad page, and the README that says what the
result does and does not support.
