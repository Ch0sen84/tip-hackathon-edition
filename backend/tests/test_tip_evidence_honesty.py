"""Regression tests for the 2026-10-15 Priority 1 presentation-honesty fix:
the SYNTHETIC `historical_percentile` placeholder gate (signal_engine.
synthetic_percentile_gate -- a static premium-size bucket estimator, not
real historical dark-pool data) must never be rendered as real dark-pool
evidence, and dark-pool language must only ever be written when backed by
a real, non-bootstrapped ratio.

Root cause this covers (Chad's Live-Mode diagnostic, 2026-10-15): all 5
watchlist tickers repeated "Dark-pool activity above normal"/"well above
normal" even when the REAL dark_pool_anomaly gate had failed/bootstrapped
(insufficient baseline -- structurally always true in Live Mode today,
since real UW darkpool trades carry no baseline field this build can use).
tip_evidence.families() used to fold historical_percentile into the same
"dark_pool" family as the real gate, so the synthetic gate passing alone
(easy: only needs ~$10M combined premium, see synthetic_percentile_gate's
buckets) produced exactly that dishonest text.
"""
from __future__ import annotations

from app.core import tip_evidence as te


def _item(**over):
    base = {
        "ticker": "NVDA", "state": "NEW", "gates_passed": [],
        "gates_metrics": {}, "dp": None, "options": None,
        "options_partial": False, "alerts": None, "context": None,
        "price": None, "next_earnings_date": None, "research": [],
    }
    base.update(over)
    return base


def test_synthetic_percentile_alone_never_labeled_dark_pool():
    """historical_percentile passing WITHOUT dark_pool_anomaly must produce
    the synthetic_baseline family, never dark_pool."""
    item = _item(gates_passed=["historical_percentile"],
                 gates_metrics={"historical_percentile": {"percentile": 0.97, "synthetic": True}})
    fam = te.families(item)
    assert "dark_pool" not in fam
    assert "synthetic_baseline" in fam


def test_synthetic_percentile_alone_never_produces_dark_pool_prose():
    item = _item(gates_passed=["historical_percentile"],
                 gates_metrics={"historical_percentile": {"percentile": 0.97, "synthetic": True}})
    read = te.current_read(item)
    assert "dark-pool" not in read.lower()
    assert "synthetic" in read.lower()
    headline = te.headline(item)
    assert "dark-pool" not in headline.lower() and "dark pool" not in headline.lower()
    ev = te.evidence(item)
    dark_pool_bullets = [e for e in ev if e["family"] == "dark_pool"]
    synthetic_bullets = [e for e in ev if e["family"] == "synthetic_baseline"]
    assert not dark_pool_bullets
    assert synthetic_bullets and "synthetic" in synthetic_bullets[0]["text"].lower()


def test_real_dark_pool_gate_still_produces_real_dark_pool_prose():
    """A genuine dark_pool_anomaly pass (real ratio) must still render as
    real dark-pool evidence -- this fix must not silence real signal."""
    item = _item(gates_passed=["dark_pool_anomaly"],
                 dp={"ratio": 5.2, "hist": None, "prints": None})
    fam = te.families(item)
    assert "dark_pool" in fam
    assert "synthetic_baseline" not in fam
    read = te.current_read(item)
    assert "dark-pool activity well above normal" in read.lower()
    ev = te.evidence(item)
    assert any(e["family"] == "dark_pool" and "5.2x" in e["text"] for e in ev)


def test_both_gates_passing_render_two_distinct_honest_bullets():
    """When BOTH the real gate and the synthetic gate pass, each must get
    its own correctly-labeled bullet -- never merged into one claim."""
    item = _item(gates_passed=["dark_pool_anomaly", "historical_percentile"],
                 dp={"ratio": 4.0, "hist": None, "prints": None},
                 gates_metrics={"historical_percentile": {"percentile": 0.95, "synthetic": True}})
    ev = te.evidence(item)
    families_seen = {e["family"] for e in ev}
    assert {"dark_pool", "synthetic_baseline"} <= families_seen
    syn = next(e for e in ev if e["family"] == "synthetic_baseline")
    assert "not a real" in syn["text"].lower() or "not real" in syn["text"].lower()


def test_dominant_label_discloses_synthetic_not_dark_pool():
    item = _item(gates_passed=["historical_percentile"],
                 gates_metrics={"historical_percentile": {"percentile": 0.99}})
    assert te.dominant_label(item) == "SYNTHETIC BASELINE"
