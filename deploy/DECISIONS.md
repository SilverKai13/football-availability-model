# Deployment decisions

The choices behind the production setup, with their costs. Numbered P- to
keep them apart from the modelling decisions in the project's own log.

## P-001 — The product is a weekly table, so the platform serves a table

**Decision.** No on-request scoring. The pipeline runs once a week as a
CronJob and publishes `predictions.parquet` plus a manifest; the API and the
Streamlit page read that file and nothing else.

**Why.** The model scores player-weeks as of a Monday from data that is
complete by then. Nothing changes between Monday and the next Monday that
the model could see. An online scoring service would add a model server, a
feature store and a failure mode for every request, to produce the same
number a file already holds.

**Cost.** A question about a week the job never scored gets a 404. The set
of weeks served is whatever the last run published.

## P-002 — One volume, one manifest, atomic rename

**Decision.** The job and the readers share a volume. `publish` validates the
file against the contract, writes it under a temporary name, renames it into
place, then writes the manifest last; readers poll the manifest's
modification time and reload on change, keeping the previous frame if the
new file fails to load. Previous versions go to `history/` for a one-command
rollback.

**Why.** It is the simplest hand-off that cannot serve a half-written file.
The alternative, an object store with the readers pulling on a timer, is
what the Terraform also provides (the S3 bucket) and is where this goes if
the readers ever live in more than one cluster. For one cluster, a
`ReadWriteMany` volume is less to operate.

**Cost.** `ReadWriteMany` storage: EFS on EKS, Filestore on GKE, or a
single-node `ReadWriteOnce` claim for a demonstration. The CI cluster uses
the last of those.

## P-003 — Readiness means "a valid file is loaded"

**Decision.** `/ready` answers 503 until the API has loaded a predictions
file that passes the contract. A pod with an empty volume takes no traffic.

**Why.** A ready API with nothing to serve would answer every squad question
with a 503 anyway; it is better that the load balancer never sends the
question. It also makes a first deployment honest: the service is not up
until the job has run.

**Cost.** `helm install --wait` cannot be used before the first publish; CI
installs, runs the job, then waits for the rollout.

## P-004 — Verification is a kind cluster in CI, not a cloud account

**Decision.** Every push builds the image, installs the chart on a kind
cluster with the Prometheus Operator, runs the scoring job, checks the API
through its service, checks that Prometheus scrapes it and has the rules
loaded, and runs the chart's own tests. The AWS Terraform is validated,
linted and scanned, not applied.

**Why.** The project was built without spending money, and a cluster that
exists for the duration of a CI job is free on GitHub's runners. What a kind
cluster cannot show (real load balancers, `ReadWriteMany` storage, IAM) is
listed in the README rather than implied.

**Cost.** The NetworkPolicies are inert on kind's default CNI, so they are
validated by schema, not by behaviour. The Terraform has never produced a
plan against a real account.

## P-005 — Prometheus rules as a plain file with unit tests

**Decision.** The alert rules live in `files/alerts.yaml` inside the chart;
the `PrometheusRule` template wraps that file verbatim. `promtool check
rules` and `promtool test rules` run on the plain file in CI.

**Why.** Rules in a template cannot be unit-tested without rendering the
chart first, and an alert that fires on the wrong condition is worse than no
alert. Five unit tests cover the two that page (stale predictions, API down)
and the three most likely to be mis-specified.

## P-006 — Metrics that answer the on-call question

**Decision.** Beyond the usual request counts and latencies, the API exports
`aiq_predictions_age_seconds`, `aiq_predictions_loaded`, `aiq_predictions_rows`,
`aiq_predictions_reloads_total{result}` and `aiq_model_info{version}`.

**Why.** For a weekly product the question that matters is "is what people
are reading this week's?", and no request metric answers it. Age and
version are the two numbers the dashboard puts first.

## P-007 — Hardened by default, in the chart's values not in a policy engine

**Decision.** Non-root, read-only root filesystem, all capabilities dropped,
seccomp runtime default, no service-account token mounted, a disruption
budget, topology spread, and a network policy that lets the readers talk
only to DNS. All in `values.yaml`, all overridable.

**Why.** These are the settings a cluster admission policy would demand;
shipping them in the chart means the chart installs under such a policy
without a values file full of exceptions.

**Cost.** Streamlit wants a writable home directory and the pipeline writes
under its working directory, so both get `emptyDir` mounts. The read-only
root filesystem is what found that, which is the point of it.

## P-008 — Trivy's configuration scan is reported, not gating

**Decision.** The misconfiguration scan over the Terraform and the chart
runs on every push and prints its findings; it does not fail the build.

**Why.** Its HIGH findings on this configuration are judgement calls, each
made here: the S3 bucket uses SSE-S3 rather than a customer-managed KMS key
(nothing in the bucket is sensitive and a key is one more thing to rotate);
the cluster endpoint is public (there is no VPN or office CIDR yet, and
`endpoint_public_access_cidrs` is the one-line change when there is); the
EFS security group allows all egress (mount targets initiate nothing). A
gate that fails on decisions already taken teaches people to ignore gates.

## P-009 — No model artefact in the image

**Decision.** The image carries code and configuration, never predictions.
The weekly job produces them inside the cluster; CI produces synthetic ones
with `aiq smoke-data` for the deployment checks.

**Why.** The predictions are derived from licensed data and carry real
players' names; they belong on a volume with access control, not in a
registry anyone can pull from. It also keeps the image small and the
deployment honest about where the numbers come from.
