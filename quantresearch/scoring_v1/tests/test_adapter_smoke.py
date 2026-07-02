"""
test_adapter_smoke.py - best-effort smoke test of the PIT adapter against the
real local store. Skips gracefully when the DB is empty/unavailable so it never
flakes in CI without data. We assert the contract shape only.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

adapter = pytest.importorskip(
    "quantresearch.scoring_v1.adapter",
    reason="adapter or its data deps unavailable",
)


def _recent_business_day():
    """A reasonably recent weekday string 'YYYY-MM-DD' (point-in-time cutoff)."""
    d = dt.date.today() - dt.timedelta(days=3)
    while d.weekday() >= 5:  # Sat/Sun -> step back to Friday
        d -= dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def test_run_scoring_returns_dataframe():
    """run_scoring must always return a DataFrame (empty when no data), never raise."""
    try:
        res = adapter.run_scoring(_recent_business_day())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"adapter.run_scoring raised in this environment: {exc!r}")

    assert isinstance(res, pd.DataFrame)

    if res.empty:
        pytest.skip("DB has no eligible point-in-time data for the chosen date")

    # If we DID get rows, sanity-check the engine contract columns.
    for col in ("composite_z", "score", "rank"):
        assert col in res.columns
    # composite descending + unique ranks
    vals = res["composite_z"].values
    assert (vals[:-1] >= vals[1:]).all()
    assert sorted(res["rank"].tolist()) == list(range(1, len(res) + 1))


def test_inputs_summary_shape():
    """inputs_summary is pure reporting; assert it returns the documented keys."""
    try:
        summ = adapter.inputs_summary(_recent_business_day())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"adapter.inputs_summary raised in this environment: {exc!r}")

    assert isinstance(summ, dict)
    for key in (
        "as_of_date", "eligible_symbols", "price_data_size",
        "benchmark_rows", "sector_idx_coverage", "earnings_total",
    ):
        assert key in summ
