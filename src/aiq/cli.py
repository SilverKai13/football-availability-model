"""Command line entry points.

Each stage writes its output to disk, so you can rerun one without redoing the
ones before it. Only "export" touches the database or the network.
"""

import os
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import numpy as np
import pandas as pd
import typer

from aiq import absences as absences_mod
from aiq import export as export_mod
from aiq.identity import attach_dob, build_aliases, build_appearances, build_players_dim
from aiq.metrics import evaluate, reliability_table
from aiq.models import FEATURE_COLUMNS, CalibratedScorer, build_models
from aiq.panel import build_panel, median_durations, with_assumed_end
from aiq.splits import (
    Fold,
    forward_chain_folds,
    forward_chain_grouped_folds,
    naive_random_folds,
)

app = typer.Typer(add_completion=False, help="AvailabilityIQ pipeline.")

RAW = Path("data/raw")
INTERIM = Path("data/interim")
PANEL = Path("data/panel")
FEATURES = Path("data/features")
ARTIFACTS = Path("artifacts")
#: Where `publish` installs predictions and the API reads them; a shared volume on a cluster.
PUBLISHED = Path("artifacts/published")
#: Recorded in the manifest; the image build sets it to the git sha.
DEFAULT_VERSION = os.environ.get("AIQ_VERSION", "dev")

# Leagues carried into modelling. Championship and ISL excluded on
# Transfermarkt reporting depth, not on anything about the football.
MODELLED_LEAGUES = [8, 82, 564]

# Modelling window start. Minutes coverage is ~95% from 2022 and ~99.9% from 2023.
WINDOW_START = "2022-07-01"


def _require(path: Path) -> Path:
    """Fail with something a human can act on, not a stack trace."""
    if not path.exists():
        typer.echo(f"missing {path}\nrun the earlier pipeline stages first.", err=True)
        raise typer.Exit(code=1)
    return path


@app.command()
def export() -> None:
    """Copy the Sportmonks corpus into data/raw/ and build the L1 tables."""
    written = export_mod.export_all()
    for name, rows in written.items():
        typer.echo(f"{name:20s} {rows:>10,}")

    player_match = pd.read_parquet(RAW / "player_match.parquet")
    dim = build_players_dim(player_match)
    dob_path = INTERIM / "players_dob.parquet"
    if dob_path.exists():
        dim = attach_dob(dim, pd.read_parquet(dob_path))
    INTERIM.mkdir(parents=True, exist_ok=True)
    dim.to_parquet(INTERIM / "players_dim.parquet", index=False)
    build_appearances(player_match, dim).to_parquet(INTERIM / "appearances.parquet", index=False)
    typer.echo(f"players_dim {len(dim):,}  appearances written")


@app.command()
def xwalk(audit: bool = typer.Option(False, help="Print match rate by minutes decile.")) -> None:
    """Build the Transfermarkt to Sportmonks crosswalk."""
    from aiq.identity import build_crosswalk

    dim = pd.read_parquet(_require(INTERIM / "players_dim.parquet"))
    profiles = pd.read_csv(_require(RAW / "tm_player_profiles.csv"), low_memory=False)
    aliases = build_aliases(dim)
    aliases.to_parquet(INTERIM / "aliases.parquet", index=False)
    crosswalk = build_crosswalk(aliases, dim, profiles)
    crosswalk.to_parquet(INTERIM / "xwalk.parquet", index=False)
    typer.echo(f"crosswalk {len(crosswalk):,} pairs")
    typer.echo(crosswalk.method.value_counts().to_string())

    if audit:
        appearances = pd.read_parquet(INTERIM / "appearances.parquet")
        appearances["match_date"] = pd.to_datetime(appearances.match_date)
        recent = appearances[appearances.match_date >= WINDOW_START]
        minutes = recent.groupby("player_id").minutes.sum()
        matched = minutes.index.isin(set(crosswalk.player_id))
        typer.echo(f"players matched      {matched.mean():.1%}")
        typer.echo(f"minutes-weighted     {minutes[matched].sum() / minutes.sum():.1%}")
        frame = minutes.rename("mins").reset_index()
        frame["matched"] = frame.player_id.isin(set(crosswalk.player_id))
        frame["decile"] = pd.qcut(frame.mins.rank(method="first"), 10, labels=False) + 1
        typer.echo("match rate by minutes decile (flat = no fame bias):")
        for decile, group in frame.groupby("decile"):
            typer.echo(f"  {int(decile):>2}: {group.matched.mean():6.1%}")


@app.command()
def categories() -> None:
    """Audit the absence-category mapping. Phase 0 needs a zero unknown residual."""
    injuries = pd.read_csv(_require(RAW / "tm_player_injuries.csv"))
    audited = absences_mod.audit_categories(injuries)
    total = audited.spells.sum()
    for category, group in audited.groupby("category"):
        share = group.spells.sum() / total
        typer.echo(
            f"  {category:10s} {group.spells.sum():>8,} ({share:6.2%})  {len(group):>3} strings"
        )
    unknown = audited[audited.category == "unknown"]
    typer.echo(f"unknown residual: {unknown.spells.sum():,} spells across {len(unknown)} strings")


@app.command()
def panel() -> None:
    """Build the absence spells and the labelled player-week panel."""
    injuries = pd.read_csv(_require(RAW / "tm_player_injuries.csv"))
    crosswalk = pd.read_parquet(_require(INTERIM / "xwalk.parquet"))
    spells = absences_mod.build_absences(injuries, crosswalk)
    spells = with_assumed_end(spells, median_durations(spells))
    spells.to_parquet(INTERIM / "absences.parquet", index=False)

    appearances = pd.read_parquet(_require(INTERIM / "appearances.parquet"))
    appearances["match_date"] = pd.to_datetime(appearances.match_date)
    appearances = appearances[
        (appearances.match_date >= WINDOW_START) & (appearances.league_id.isin(MODELLED_LEAGUES))
    ]
    built = build_panel(appearances, spells, label_end=spells.start_date.max())
    PANEL.mkdir(parents=True, exist_ok=True)
    built.to_parquet(PANEL / "player_week.parquet", index=False)
    typer.echo(f"absences {len(spells):,} spells")
    typer.echo(f"panel    {len(built):,} player-weeks")
    typer.echo(
        f"  label_injury {built.label_injury.mean():.2%} ({int(built.label_injury.sum()):,})"
    )
    typer.echo(f"  label_any    {built.label_any.mean():.2%} ({int(built.label_any.sum()):,})")


@app.command()
def features() -> None:
    """Build the as-of feature matrix."""
    from aiq.features import build_features

    built = pd.read_parquet(_require(PANEL / "player_week.parquet"))
    built["monday"] = pd.to_datetime(built.monday)
    appearances = pd.read_parquet(INTERIM / "appearances.parquet")
    appearances["match_date"] = pd.to_datetime(appearances.match_date)
    appearances = appearances[
        (appearances.match_date >= WINDOW_START) & (appearances.league_id.isin(MODELLED_LEAGUES))
    ]
    fixtures = pd.read_parquet(RAW / "fixtures.parquet")
    fixtures["starting_at"] = pd.to_datetime(fixtures.starting_at)
    fixtures = fixtures[fixtures.league_id.isin(MODELLED_LEAGUES)]
    spells = pd.read_parquet(INTERIM / "absences.parquet")
    dim = pd.read_parquet(INTERIM / "players_dim.parquet")

    matrix = build_features(built, appearances, fixtures, spells, dim)
    FEATURES.mkdir(parents=True, exist_ok=True)
    matrix.to_parquet(FEATURES / "features.parquet", index=False)
    typer.echo(f"features {len(matrix):,} rows x {len(matrix.columns)} columns")


@app.command()
def train(label: str = typer.Option("label_injury", help="label_injury or label_any")) -> None:
    """Fit every model on every splitter and write artifacts/metrics.parquet."""
    warnings.filterwarnings("ignore")
    built = pd.read_parquet(_require(PANEL / "player_week.parquet"))
    matrix = pd.read_parquet(_require(FEATURES / "features.parquet"))
    built["monday"] = pd.to_datetime(built.monday)
    matrix["monday"] = pd.to_datetime(matrix.monday)
    frame = matrix.merge(built[["player_id", "monday", label]], on=["player_id", "monday"])
    labels = frame[label].to_numpy(dtype=float)

    # All three are called with just the frame; their keyword defaults differ.
    splitters: dict[str, Callable[[pd.DataFrame], list[Fold]]] = {
        "naive_random": naive_random_folds,
        "honest_temporal": forward_chain_folds,
        "honest_grouped": forward_chain_grouped_folds,
    }
    rows: list[dict[str, object]] = []
    for splitter_name, make_folds in splitters.items():
        for fold in make_folds(frame):
            for model_name, scorer in build_models().items():
                model = CalibratedScorer(scorer)
                model.fit(frame.iloc[fold.train], labels[fold.train], frame.iloc[fold.train].monday)
                predictions = model.predict(frame.iloc[fold.test])
                scores = evaluate(frame.iloc[fold.test], labels[fold.test], predictions)
                record: dict[str, object] = dict(scores)
                record["splitter"] = splitter_name
                record["fold"] = fold.name
                record["model"] = model_name
                record["label"] = label
                record["train_n"] = len(fold.train)
                record["train_events"] = int(labels[fold.train].sum())
                rows.append(record)
            typer.echo(f"  {fold.name} done")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(ARTIFACTS / "metrics.parquet", index=False)
    typer.echo(f"metrics {len(rows)} rows -> artifacts/metrics.parquet")


@app.command()
def reliability() -> None:
    """Write the reliability diagram, predicted probability against observed rate.

    The chart the calibration claim rests on. Fitted on the honest temporal split, so
    the curve describes performance on a season the model never saw.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    warnings.filterwarnings("ignore")
    built = pd.read_parquet(_require(PANEL / "player_week.parquet"))
    matrix = pd.read_parquet(_require(FEATURES / "features.parquet"))
    built["monday"] = pd.to_datetime(built.monday)
    matrix["monday"] = pd.to_datetime(matrix.monday)
    frame = matrix.merge(built[["player_id", "monday", "label_injury"]], on=["player_id", "monday"])
    labels = frame.label_injury.to_numpy(dtype=float)

    fold = forward_chain_folds(frame)[-1]
    figure, axis = plt.subplots(figsize=(5.5, 5.5))
    for name in ("logistic_regression", "gradient_boosting"):
        model = CalibratedScorer(build_models()[name])
        model.fit(frame.iloc[fold.train], labels[fold.train], frame.iloc[fold.train].monday)
        table = reliability_table(labels[fold.test], model.predict(frame.iloc[fold.test]))
        axis.plot(table.predicted, table.observed, marker="o", label=name.replace("_", " "))

    base = float(labels[fold.test].mean())
    limit = max(0.08, float(labels[fold.test].mean()) * 4)
    axis.plot([0, limit], [0, limit], linestyle="--", color="grey", label="perfect")
    axis.axhline(base, color="grey", linewidth=0.6)
    axis.axvline(base, color="grey", linewidth=0.6)
    axis.set_xlim(0, limit)
    axis.set_ylim(0, limit)
    axis.set_xlabel("predicted probability")
    axis.set_ylabel("observed rate")
    axis.set_title(f"Reliability, {fold.name} (base rate {base:.2%})")
    axis.legend()
    figure.tight_layout()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = ARTIFACTS / f"reliability_{fold.name.replace('/', '-')}.png"
    figure.savefig(out, dpi=150)
    typer.echo(f"wrote {out}")


@app.command()
def predict() -> None:
    """Score the most recent season and write artifacts/predictions.parquet.

    The Streamlit page reads this and nothing else, no model is fitted at request
    time. Trained on every season before the one it scores, so
    the numbers on screen are out-of-sample.

    Logistic regression, deliberately. For a linear model the local explanation is
    exact and free: contribution_j = coefficient_j x standardised_value_j, and those
    contributions sum to the log-odds. A tree model would need shap to say anything
    as precise, and an approximate explanation of a model this weak would be worse than
    an exact one. Gradient boosting stays the better-calibrated model and both are in
    the metrics table.
    """
    warnings.filterwarnings("ignore")
    built = pd.read_parquet(_require(PANEL / "player_week.parquet"))
    matrix = pd.read_parquet(_require(FEATURES / "features.parquet"))
    built["monday"] = pd.to_datetime(built.monday)
    matrix["monday"] = pd.to_datetime(matrix.monday)
    frame = matrix.merge(built[["player_id", "monday", "label_injury"]], on=["player_id", "monday"])
    labels = frame.label_injury.to_numpy(dtype=float)

    fold = forward_chain_folds(frame)[-1]
    model = CalibratedScorer(build_models()["logistic_regression"])
    model.fit(frame.iloc[fold.train], labels[fold.train], frame.iloc[fold.train].monday)
    test = frame.iloc[fold.test].copy()
    test["risk"] = model.predict(test)

    pipeline = model.scorer.estimator  # type: ignore[attr-defined]
    design = pipeline[:-1].transform(test[FEATURE_COLUMNS])
    coefficients = pipeline[-1].coef_[0]
    # Prefixed: several feature names (league_id, position_id) are also keys here.
    contributions = pd.DataFrame(
        design * coefficients,
        columns=[f"contrib_{name}" for name in FEATURE_COLUMNS],
        index=test.index,
    )

    dim = pd.read_parquet(INTERIM / "players_dim.parquet")
    names = dim.get("display_name")
    dim["player_name"] = dim.name_raw if names is None else names.fillna(dim.name_raw)
    out = test[["player_id", "monday", "team_id", "league_id", "risk", "label_injury"]]
    out = out.merge(dim[["player_id", "player_name", "position_id"]], on="player_id", how="left")
    out = pd.concat([out.reset_index(drop=True), contributions.reset_index(drop=True)], axis=1)
    out.attrs["base_rate"] = float(labels[fold.test].mean())

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out.to_parquet(ARTIFACTS / "predictions.parquet", index=False)
    typer.echo(f"scored {fold.name}: {len(out):,} player-weeks")
    typer.echo(f"  base rate {labels[fold.test].mean():.2%}  mean predicted {out.risk.mean():.2%}")
    typer.echo(f"  weeks {out.monday.min().date()} -> {out.monday.max().date()}")


@app.command()
def sensitivity() -> None:
    """Both sensitivity analyses: the unknown injury call, and invisible European load."""
    from aiq.sensitivity import european_team_seasons, flag_european, rate_by_minutes_band

    warnings.filterwarnings("ignore")
    built = pd.read_parquet(_require(PANEL / "player_week.parquet"))
    matrix = pd.read_parquet(_require(FEATURES / "features.parquet"))
    built["monday"] = pd.to_datetime(built.monday)
    matrix["monday"] = pd.to_datetime(matrix.monday)
    fixtures = pd.read_parquet(RAW / "fixtures.parquet")
    fixtures["starting_at"] = pd.to_datetime(fixtures.starting_at)
    fixtures = fixtures[fixtures.league_id.isin(MODELLED_LEAGUES)]

    european = european_team_seasons(fixtures)
    is_european = flag_european(built, european)
    typer.echo(f"European team-seasons: {len(european)}")
    typer.echo(f"panel rows at those clubs: {is_european.sum():,} ({is_european.mean():.1%})")
    typer.echo(f"  base rate, European clubs : {built.label_injury[is_european].mean():.2%}")
    typer.echo(f"  base rate, domestic only  : {built.label_injury[~is_european].mean():.2%}")
    eu_minutes = matrix.minutes_28d[is_european.to_numpy()].mean()
    dom_minutes = matrix.minutes_28d[~is_european.to_numpy()].mean()
    typer.echo(f"  mean minutes_28d          : {eu_minutes:.1f} vs {dom_minutes:.1f}")
    typer.echo("injury rate within the same observed-minutes band:")
    table = rate_by_minutes_band(built, matrix, is_european)
    typer.echo(table.to_string(index=False, float_format=lambda x: f"{x:,.4f}"))


@app.command()
def report() -> None:
    """Print the results table the README is written from."""
    metrics = pd.read_parquet(_require(ARTIFACTS / "metrics.parquet"))
    order = [
        "base_rate",
        "recent_minutes",
        "prior_injuries",
        "logistic_regression",
        "gradient_boosting",
    ]
    grouped = (
        metrics.groupby(["splitter", "model"])
        .agg(
            pr_auc=("pr_auc", "mean"),
            brier=("brier", "mean"),
            ece=("ece", "mean"),
            bss=("brier_skill", "mean"),
            p5=("precision_at_5", "mean"),
            base=("base_rate", "mean"),
            train_n=("train_n", "mean"),
        )
        .reset_index()
    )
    for splitter_name in ["naive_random", "honest_temporal", "honest_grouped"]:
        table = grouped[grouped.splitter == splitter_name].set_index("model").reindex(order)
        if table.pr_auc.isna().all():
            continue
        typer.echo(
            f"\n=== {splitter_name}  base rate {table.base.iloc[0]:.2%}"
            f"  mean train n {table.train_n.iloc[0]:,.0f}"
        )
        header = f"{'model':22s} {'PR-AUC':>8s} {'lift':>6s} {'Brier':>8s}"
        typer.echo(f"{header} {'ECE':>7s} {'BSS':>8s} {'p@5':>7s}")
        for name, row in table.iterrows():
            typer.echo(
                f"{name:22s} {row.pr_auc:8.4f} {row.pr_auc / row.base:6.2f}x "
                f"{row.brier:8.5f} {row.ece:7.4f} {row.bss:8.4f} {row.p5:7.4f}"
            )

    honest = grouped[grouped.splitter == "honest_temporal"].set_index("model")
    naive = grouped[grouped.splitter == "naive_random"].set_index("model")
    typer.echo("\nnaive vs honest gap:")
    for name in ["logistic_regression", "gradient_boosting"]:
        if name in honest.index and name in naive.index:
            gap = (naive.loc[name].pr_auc - honest.loc[name].pr_auc) / honest.loc[name].pr_auc
            typer.echo(f"  {name:22s} {gap:+.1%}")


@app.command()
def publish(
    source: Annotated[
        Path, typer.Option(help="Directory holding predictions.parquet.")
    ] = ARTIFACTS,
    destination: Annotated[
        Path, typer.Option(help="Where the API reads; a shared volume on a cluster.")
    ] = PUBLISHED,
    version: Annotated[str, typer.Option(help="Recorded in the manifest.")] = DEFAULT_VERSION,
    keep: Annotated[int, typer.Option(help="Previous versions kept under history/.")] = 3,
) -> None:
    """Validate the scored predictions and install them where the API reads them."""
    from aiq.serve.publish import PublishError
    from aiq.serve.publish import publish as do_publish

    try:
        manifest = do_publish(source, destination, version=version, keep=keep)
    except PublishError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"published {manifest.rows:,} player-weeks ({manifest.weeks_from} -> {manifest.weeks_to}) "
        f"to {destination}  version {manifest.version}  sha256 {manifest.sha256[:12]}"
    )


@app.command()
def rollback(
    destination: Annotated[Path, typer.Option(help="The published directory.")] = PUBLISHED,
) -> None:
    """Reinstate the previous published predictions."""
    from aiq.serve.publish import PublishError
    from aiq.serve.publish import rollback as do_rollback

    try:
        manifest = do_rollback(destination)
    except PublishError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"rolled back to the predictions published {manifest.published_at}")


@app.command("smoke-data")
def smoke_data(
    out: Annotated[Path, typer.Option(help="Directory to write into.")] = Path("artifacts/smoke"),
    teams: Annotated[int, typer.Option()] = 3,
    weeks: Annotated[int, typer.Option()] = 4,
) -> None:
    """Write synthetic artefacts that pass the contract, for deployment smoke checks."""
    from aiq.serve.fixtures import write_synthetic_artifacts

    frame = write_synthetic_artifacts(out, teams=teams, weeks=weeks)
    typer.echo(f"wrote {len(frame):,} synthetic player-weeks to {out} (no real players)")


@app.command()
def weekly(
    destination: Annotated[
        Path, typer.Option(help="Where the API reads; a shared volume on a cluster.")
    ] = PUBLISHED,
    skip_export: Annotated[
        bool, typer.Option(help="Start from the panel; data/raw is already current.")
    ] = False,
    with_metrics: Annotated[
        bool, typer.Option(help="Also refit every model for metrics.parquet.")
    ] = False,
    version: Annotated[str, typer.Option(help="Recorded in the manifest.")] = DEFAULT_VERSION,
) -> None:
    """The scheduled run: export, crosswalk, panel, features, predict, publish.

    Each stage writes to disk, so a failure leaves the published predictions as
    they were: publish is the last step and refuses anything that fails the contract.
    """
    import time

    stages: list[tuple[str, Callable[[], None]]] = []
    if not skip_export:
        stages.append(("export", export))
        stages.append(("xwalk", lambda: xwalk(audit=False)))
        stages.append(("categories", categories))
    stages += [("panel", panel), ("features", features)]
    if with_metrics:
        stages.append(("train", lambda: train(label="label_injury")))
    stages.append(("predict", predict))
    for name, stage in stages:
        started = time.perf_counter()
        typer.echo(f"== {name}")
        stage()
        typer.echo(f"== {name} done in {time.perf_counter() - started:,.0f}s")
    publish(source=ARTIFACTS, destination=destination, version=version, keep=3)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    np.seterr(all="ignore")
    app()


if __name__ == "__main__":
    main()
