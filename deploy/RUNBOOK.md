# Runbook

One entry per alert in `deploy/chart/availabilityiq/files/alerts.yaml`. Each
alert's annotation links here by name.

Where things are, for a release called `aiq` in namespace `aiq`:

```bash
kubectl -n aiq get deploy,cronjob,job,pvc,pod
kubectl -n aiq logs deploy/aiq-availabilityiq-api --tail=200
kubectl -n aiq port-forward svc/aiq-availabilityiq-api 8000:80   # then http://localhost:8000/ready
```

The API's own view of its state: `/ready` (what is loaded and how old),
`/v1/model` (version, hash, rows, weeks), `/metrics`.

## AiqPredictionsStale

**Meaning.** The predictions being served were published more than eight
days ago. The weekly job has not delivered. Physios opening the page on
Monday are reading last week's numbers and nothing on the page says so
beyond the "week beginning" date.

**Do now.**

1. Confirm: `curl localhost:8000/v1/model` and read `published_at`.
2. Find the job: `kubectl -n aiq get jobs -l app.kubernetes.io/component=scoring`.
   A failed job has its own entry below (AiqScoringJobFailed); no job at all
   means the CronJob did not fire. `kubectl -n aiq describe cronjob` shows
   `Last Schedule Time` and whether it was suspended or missed its
   `startingDeadlineSeconds`.
3. If the job never ran, run it now:
   `kubectl -n aiq create job --from=cronjob/aiq-availabilityiq-scoring score-now`
   and follow it with `kubectl -n aiq logs -f job/score-now`.
4. If the job ran and published but the API did not pick it up, check the
   manifest on the volume against what the API reports:
   `kubectl -n aiq exec deploy/aiq-availabilityiq-api -- cat /srv/artifacts/manifest.json`.
   A manifest newer than the API's `published_at` with no reload is a bug in
   the reload path (the API polls the manifest's mtime every
   `api.reloadSeconds`); restart the deployment and open an issue.
5. Tell the people who read the page. The number on it is a week old.

## AiqScoringNotRunning

**Meaning.** kube-state-metrics has not seen a successful completion of the
scoring CronJob in over eight days. Same situation as AiqPredictionsStale
seen from the job's side; usually both fire.

**Do now.** As above, from step 2. If the job is suspended
(`kubectl -n aiq get cronjob -o jsonpath='{.items[0].spec.suspend}'`),
someone paused it; find out why before unpausing.

## AiqScoringJobFailed

**Meaning.** A run of the weekly job failed. The previously published
predictions are still being served, so nothing is wrong for readers yet;
if it is Sunday night, it will be by Monday.

**Do now.**

1. `kubectl -n aiq get jobs -l app.kubernetes.io/component=scoring` and
   `kubectl -n aiq logs job/<name>`. The stages print `== name` and
   `== name done in Ns`; the last `==` line says where it died.
2. Causes, by stage:
   - `export`: the corpus volume is missing or the upstream fetch failed.
     Check `scoring.corpus.existingClaim` is bound and the secret in
     `scoring.envFrom` exists. A token that expired fails here.
   - `panel` or `features`: a schema change upstream. The error names the
     column. This needs a code change, not a retry.
   - `predict`: out of memory is the usual one; the job's limit is in
     `scoring.resources`. `kubectl -n aiq describe pod <pod>` shows
     `OOMKilled`.
   - `publish`: the file failed the contract. The message lists every
     problem. This is the stage that protects readers; do not bypass it.
3. Re-run with `kubectl -n aiq create job --from=cronjob/... score-now` once
   the cause is fixed. `concurrencyPolicy: Forbid` means a stuck earlier run
   must be deleted first.

## AiqScoringRunningTooLong

**Meaning.** A run has been active for over two hours. A normal run finishes
inside the hour; the job's `activeDeadlineSeconds` will kill it at two.

**Do now.** `kubectl -n aiq logs -f job/<name>` to see which stage. If it
is `export`, the upstream is slow or hanging; if it is `predict`, the panel
grew (check `== features done` for the row count printed). Let the deadline
kill it, then raise `scoring.resources` or the deadline deliberately.

## AiqApiDown

**Meaning.** Prometheus scraped the API's target and it did not answer, for
two minutes. Readiness is separate (AiqApiNotReady); this is the process.

**Do now.**

1. `kubectl -n aiq get pods -l app.kubernetes.io/component=api` and
   `kubectl -n aiq describe pod <pod>`: crash loops show in `Last State`,
   with `OOMKilled` or an exit code.
2. `kubectl -n aiq logs <pod> --previous` for the last crash. The API logs
   JSON; the last line before the exit is usually the cause.
3. A bad image rollout: `kubectl -n aiq rollout history deploy/aiq-availabilityiq-api`
   and `rollout undo`. The rolling update keeps `maxUnavailable: 0`, so a new
   image that never becomes ready should not have taken the old pods down;
   if it did, the readiness probe is lying and that is the bug.

## AiqApiNoTargets

**Meaning.** The ServiceMonitor matches no endpoints at all. The service, the
deployment or the labels changed.

**Do now.** `kubectl -n aiq get svc,endpoints -l app.kubernetes.io/component=api`.
An empty endpoints list with a running pod means the selector labels
diverged, which happens after a chart upgrade that renamed something. Check
`helm -n aiq get manifest aiq | grep -A3 selector`.

## AiqApiNotReady

**Meaning.** Every replica reports `aiq_predictions_loaded == 0` for ten
minutes. The volume has no valid predictions file. No traffic is served:
readiness fails on purpose.

**Do now.**

1. Is this a first deployment? The API is ready only after the first
   publish. Run the job (`create job --from=cronjob/...`) or, for a
   demonstration, `aiq smoke-data` then `aiq publish` inside a pod with the
   volume mounted.
2. Otherwise the file was removed or the volume is not the one the job
   wrote to. `kubectl -n aiq get pvc` and compare `volumes` in the API pod
   and the last job pod; a chart upgrade that changed
   `artifacts.persistence.existingClaim` does this.
3. `kubectl -n aiq logs deploy/aiq-availabilityiq-api | grep -i "not loadable"`
   says exactly why the file was refused.

## AiqPredictionsReloadFailed

**Meaning.** A new manifest appeared and the file it describes failed the
contract or could not be read. The API kept the previous predictions. Since
`publish` validates before installing, this should not happen through the
job; it means the files on the volume were written by something else or
were corrupted.

**Do now.** The API log line `reload failed, keeping the previous
predictions: ...` names the problem. Compare `sha256` in the manifest with
`sha256sum /srv/artifacts/predictions.parquet` from inside a pod. If the
hashes differ, the file was altered after publish. `aiq rollback
--destination /srv/artifacts` reinstates the previous version from
`history/`.

## AiqApiErrorRateHigh

**Meaning.** More than 2% of requests returned 5xx for ten minutes. The API
has no downstream, so a 5xx is a bug or a resource problem in the API itself.

**Do now.** `kubectl -n aiq logs deploy/aiq-availabilityiq-api | grep '"level": "ERROR"'`.
The request id in the log line matches the `x-request-id` the caller got.
Memory pressure shows as restarts rather than 5xx, so check the dashboard's
"Requests by route": one route failing is a code path, every route failing is
the process.

## AiqApiLatencyHigh

**Meaning.** p95 over 500 ms for ten minutes. A squad question is a filter
over an in-memory frame and takes single-digit milliseconds; this means CPU
starvation or a very large predictions file.

**Do now.** `kubectl -n aiq top pods`; if CPU is at the request, the HPA
should be scaling (`kubectl -n aiq get hpa`). If the file grew (check `rows`
in `/v1/model`), raise `api.resources` and consider trimming the weeks the
job publishes.

## Routine

- **After every weekly run**, the dashboard's "Predictions age" resets and
  "Model version" shows the image that scored. If the version changed
  without a deployment, someone ran the job from a different image.
- **Rollback of predictions:** `aiq rollback --destination /srv/artifacts`
  inside any pod with the volume mounted read-write (a job pod, or a debug
  pod). Rollback of the service: `helm rollback`.
- **Upgrading the chart:** `helm upgrade` with the same values; the rolling
  update keeps one replica serving throughout. The CronJob's spec changes
  take effect at the next schedule.
