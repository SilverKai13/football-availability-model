# Service levels

What the service promises, how it is measured, and what is deliberately not
promised. Numbers are starting points for a service with no traffic history;
the first month of real use resets them from what the dashboard shows.

## Who depends on it

A physio or an analyst looking at a squad on a Monday morning, through the
page or a dashboard that calls the API. The numbers are read once a week,
discussed in a meeting, and acted on over the following days. Nothing is
automated downstream and nothing is real-time. That shapes everything below:
freshness matters more than latency, and a short outage on a Wednesday costs
nothing.

## Objectives

| Objective | Target | Measured by | Window |
|---|---|---|---|
| **Freshness.** Predictions served are for the current week | Published by 06:00 UK time on Monday, 50 of 52 weeks a year | `aiq_predictions_age_seconds` under 7 days at 06:00 Monday | rolling 52 weeks |
| **Availability.** The API answers | 99.5% of minutes with a ready replica | `aiq_predictions_loaded >= 1` and `up == 1` | rolling 30 days |
| **Correctness of what is served.** The file served is the file published | 100%: every loaded file passed the contract and matches its manifest's hash | `aiq_predictions_reloads_total{result="failed"} == 0`; publish validates before install | always |
| **Latency.** A squad question answers quickly | p95 under 250 ms from the service, 500 ms through the ingress | `aiq_http_request_seconds` | rolling 30 days |
| **Errors.** Requests do not fail | under 0.5% 5xx | `aiq_http_requests_total{status=~"5.."}` | rolling 30 days |

The freshness objective is the one that matters and the one the alerts are
built around. Two misses a year allows for a Monday when the upstream corpus
is late; a third is a conversation about the pipeline, not the platform.

## Error budgets

- Availability at 99.5% over 30 days is 3.6 hours of no ready replica. A
  node drain with two replicas and a disruption budget of one should cost
  none of it; a bad publish that fails the contract costs none of it either,
  because the previous file keeps serving.
- Freshness has no budget to spend on purpose: a missed Monday is an incident
  with a runbook entry (`AiqPredictionsStale`), because the people reading
  the page cannot tell a stale number from a fresh one.

## What is not promised

- **Model quality.** The README is explicit that the model does not beat
  counting a player's injuries over the last year on the metric that
  matters. The platform guarantees that what was published is what is
  served; it does not make the numbers better.
- **Real-time scoring.** There is no on-request scoring and no streaming.
  The unit of work is the week.
- **Multi-region.** One region, one cluster. A regional outage on a Tuesday
  is a page nobody is reading.
- **Historical API stability.** `/v1` will not change shape without a
  version bump, but the set of weeks served is whatever the last run
  published; older weeks are in `history/` on the volume, not in the API.

## Alerts and the objectives they protect

| Alert | Objective | Pages? |
|---|---|---|
| AiqPredictionsStale, AiqScoringNotRunning | Freshness | yes |
| AiqApiDown, AiqApiNoTargets, AiqApiNotReady | Availability | yes |
| AiqPredictionsReloadFailed | Correctness | no, next working day |
| AiqApiErrorRateHigh, AiqApiLatencyHigh | Errors, latency | no, next working day |
| AiqScoringJobFailed, AiqScoringRunningTooLong | Freshness, early warning | no, same day |

"Pages" here means the on-call phone; for a weekly product that is Sunday
night to Monday morning, and the rest of the week is a ticket.
