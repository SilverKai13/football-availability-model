AvailabilityIQ
==============

Estimates the probability that a football player becomes unavailable for
selection in the next 7 days. Premier League, Bundesliga and La Liga,
August 2022 to September 2025.


Read this first
---------------

I had no GPS data and no training load data. The only exposure signal I have
is match minutes. That misses all training, and it also misses Champions
League, Europa League and international fixtures, because those games are not
in my dataset. So workload is understated for players at the bigger clubs,
which are exactly the players you would most want to get right.

The target is also not "injury" in a medical sense. It is whether an absence
was recorded on Transfermarkt. That is a report about a player, not a
diagnosis of one.


Data
----

171,938 player-weeks, 3,297 injury onsets, base rate 1.92%.

Two sources:

- Labels: a Transfermarkt injuries dataset, 143,195 absence spells.
- Exposure: a Sportmonks match database I already had from an earlier project,
  750,641 player-match appearances with minutes played.

The two use different player IDs, so I had to build a crosswalk. Names alone
were not enough (13,528 name strings for 11,175 players, formats like
"L. Cattermole" mixed with "Sebastian Larsson", 1,325 shared surnames). Adding
date of birth fixed it. 83.7% of players matched, 92.0% when weighted by
minutes played.

One row per player per Monday. Players who were already unavailable on that
Monday are dropped, not labelled zero. They cannot become unavailable if they
already are.

All features are computed as of the Monday using only earlier data. 27
features: rolling minutes and matches over 7/14/28 days, acute:chronic
workload, days since last match, short turnarounds, fixture congestion, away
travel, absence history, age, position.


Results
-------

Trained on seasons up to T, tested on season T+1.

                                PR-AUC   lift   prec@5     ECE   Brier skill
  base rate                     0.0204   1.00x  0.0237   0.0047   -0.0013
  rank by minutes last 28d      0.0252   1.24x  0.0237   0.0054    0.0004
  rank by injuries last 365d    0.0296   1.45x  0.0301   0.0051    0.0024
  logistic regression           0.0390   1.91x  0.0305   0.0041    0.0077
  gradient boosting             0.0359   1.76x  0.0288   0.0044    0.0068

The model does not really work, and I think it is worth being straight about
that. On precision@5, which is the number that matches how a shortlist is
actually used, logistic regression scores 0.0305 against 0.0301 for simply
counting how many injuries a player had in the last year. That is a rounding
error, not a win.

In practical terms: flag the 5 highest risk players in a squad each week and
you would catch about 2 more injuries per club per season than picking 5 names
at random.

The calibration is the part I would defend. Across 55,725 scored player-weeks
the mean prediction is 2.10% against an actual rate of 2.10%, and ECE is
0.0041. When it says 4% it means roughly 4%. A small number that is honest is
still usable; an overconfident one is worse than no model.


The thing I found interesting
-----------------------------

I built a naive random split and an honest forward-chained split expecting the
random one to look much better, which is the usual leakage demo. The gap was
only about 2.5%.

The reason turned out to be the risk set. Because already-absent players are
dropped, a player who gets injured leaves the data for the length of the
spell, so the near-identical repeated rows that a random split feeds on are
not there. I rebuilt the panel the wrong way (kept those weeks, labelled them
zero) and measured again:

  already-absent excluded:      +1.4% (logreg)   +2.5% (gbm)
  already-absent labelled zero: +6.2% (logreg)  +12.2% (gbm)

Five times the leakage, and the wrong version also scores better in absolute
terms, which is what makes the mistake tempting. So the leak is really in how
you build the risk set, not in how you split the data.


Limitations
-----------

1. Where the Sportmonks absence list overlaps the Transfermarkt data, the two
   sources only agree on 53.4% of spells. Neither is ground truth, and that
   caps how accurate anything here can be.

2. Clubs playing in Europe are 30.4% of the panel. Their average minutes over
   28 days is 142.1 against 141.2 for everyone else, so they look identical on
   the only load measure I have, but their injury rate is 2.53% against 1.65%.
   The gap holds inside every minutes band. I cannot prove it is the extra
   fixtures, because bigger clubs also get more press attention and an absence
   is more likely to be written down. Either way the model is worse on them.

3. Transfermarkt only records absences that got reported, so short knocks are
   probably missing. 18.9% of spells in the source say only "unknown injury".
   I re-ran everything with those dropped from the positive class and nothing
   changed except the absolute numbers.

4. Injury rates by league are 2.46% (Bundesliga), 2.10% (Premier League),
   1.33% (La Liga), 0.74% (Championship) and 0.07% (Indian Super League). Real
   injury rates do not vary that much, so this is reporting depth. I dropped
   the last two.

5. Crosswalk match rate goes from 72.7% in the lowest minutes decile to 97.3%
   in the highest, so lesser known players are underrepresented.

6. About 0.78% of appearance rows in the source database carry a teammate's
   name. I drop the ones I can detect, which leaves roughly 0.17% of minutes
   attributed to the wrong player.

7. Only three seasons of usable data, which gives two forward-chained folds.
   The player-grouped split is worse still, because squad continuity is around
   73%, so holding out the test season's players leaves very little to train
   on. Its poor scores mean "trained on almost nothing", not "does not
   generalise".

8. The dataset I originally planned to use was taken down from Figshare part
   way through. The mirror I used instead does not carry a licence.


Running it
----------

Everything after the export step runs offline. The data/ and artifacts/
folders are not in the repo, the pipeline rebuilds them.

  python -m aiq.cli export
  python -m aiq.cli xwalk --audit
  python -m aiq.cli categories
  python -m aiq.cli panel
  python -m aiq.cli features
  python -m aiq.cli train
  python -m aiq.cli report
  python -m aiq.cli predict
  streamlit run app/streamlit_app.py

Checks:

  python -m ruff check src tests
  python -m ruff format --check src tests
  python -m mypy src tests
  python -m pytest

97 tests. The ones I care about are the leakage tests: moving the as-of date
forward changes every rolling feature, no feature reads anything after its
cutoff, no already-absent row carries a label, and the calibrator never sees
a test season.


Layout
------

  src/aiq/export.py       pull the match data out of the source database
  src/aiq/identity.py     name cleaning and the Transfermarkt crosswalk
  src/aiq/absences.py     absence spells and category mapping
  src/aiq/panel.py        risk set, labels, censoring
  src/aiq/features.py     as-of feature building
  src/aiq/splits.py       the three train/test splitters
  src/aiq/metrics.py      PR-AUC, Brier, ECE, precision@5
  src/aiq/models.py       baselines, models, isotonic calibration
  src/aiq/sensitivity.py  the two sensitivity checks
  src/aiq/cli.py          command line entry points
  app/streamlit_app.py    squad risk page

Running it in production
------------------------

The model is a weekly batch, so the production setup serves a weekly table:
a scheduled job runs the pipeline and publishes the predictions file with a
manifest; an API and the squad page read it; Prometheus alerts when the file
stops arriving. Everything is under deploy/:

- deploy/README.md       how the pieces fit, the API, the publish hand-off,
                         what CI verifies on a real (kind) cluster and what
                         it cannot
- deploy/chart/          the Helm chart, alert rules with unit tests, the
                         Grafana dashboard
- deploy/terraform/aws/  the AWS footprint (EKS, EFS, S3, Pod Identity),
                         validated and linted but deliberately not applied
- deploy/RUNBOOK.md      one entry per alert
- deploy/SLO.md          what is promised and what is not
- deploy/DECISIONS.md    the choices and their costs

Quick look, with synthetic data and no corpus:

    aiq smoke-data --out artifacts/smoke
    aiq publish --source artifacts/smoke --destination artifacts/published
    cp artifacts/smoke/teams.parquet artifacts/published/
    AIQ_ARTIFACT_DIR=artifacts/published AIQ_TEAMS_PATH=artifacts/published/teams.parquet aiq-api

Then http://localhost:8000/v1/squads/1000/risk and /v1/model.
