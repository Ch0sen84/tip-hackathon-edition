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


# ---------------------------------------------------------------------------
# #35 (2026-10-15 round 2): the synthetic placeholder must never lead the
# headline/current_read/dominant_label, even when it co-occurs with
# exactly ONE real evidence family (so categories() < 2 and the
# MULTI-SOURCE/UW-FLOW multi-family bypasses don't fire, but the old code
# still let the synthetic family win via fixed iteration order / fam[0]).
# Covers: real options-flow-only, real dark-pool-only, synthetic-only, and
# real+synthetic combined, per the task's required fixture matrix.
# ---------------------------------------------------------------------------

def _options_item(**over):
    base = _item(gates_passed=["options_flow_anomaly"],
                 options={"date": "2026-10-15", "call_vol": 50000, "put_vol": 5000,
                          "call_vs7": 180.0, "put_vs7": 10.0, "bull": 4_000_000, "bear": 500_000,
                          "skew": 0.78})
    base.update(over)
    return base


def test_real_options_only_leads_headline_and_dominant_label():
    item = _options_item()
    assert "synthetic" not in te.headline(item).lower()
    assert te.headline(item).lower().startswith(("calls", "puts"))
    assert te.dominant_label(item) == "OPTIONS"
    read = te.current_read(item)
    assert "unusual options volume" in read.lower() or "options" in read.lower()
    assert "synthetic" not in read.lower()
    # exactly one real category -> must honestly disclose no corroboration
    assert "no second independent category corroborates" in read.lower()


def test_real_dark_pool_only_leads_headline_and_dominant_label():
    item = _item(gates_passed=["dark_pool_anomaly"], dp={"ratio": 6.0, "hist": None, "prints": None})
    assert te.headline(item).lower().startswith("dark pool")
    assert te.dominant_label(item) == "DARK POOL"
    read = te.current_read(item)
    assert read.lower().startswith("dark-pool activity well above normal")
    assert "no second independent category corroborates" in read.lower()


def test_synthetic_only_never_leads_because_nothing_real_exists_to_lead_with():
    item = _item(gates_passed=["historical_percentile"],
                 gates_metrics={"historical_percentile": {"percentile": 0.97}})
    headline = te.headline(item)
    assert "synthetic" in headline.lower()
    assert te.dominant_label(item) == "SYNTHETIC BASELINE"
    read = te.current_read(item)
    assert "synthetic" in read.lower()
    # with NO real evidence at all, the "no second category" disclosure is
    # meaningless and must not be appended (nothing real to corroborate)
    assert "no second independent category corroborates" not in read.lower()


def test_real_options_plus_synthetic_combined_leads_with_real_finding():
    """The exact #35 bug fixture: ONE real family (options) plus the
    synthetic placeholder. families() appends synthetic_baseline BEFORE
    options in its own iteration order -- headline/current_read/
    dominant_label must still never let the placeholder lead or get
    counted as a second real category."""
    item = _options_item(gates_passed=["options_flow_anomaly", "historical_percentile"],
                         gates_metrics={"historical_percentile": {"percentile": 0.95}})
    fam = te.families(item)
    assert fam[0] == "synthetic_baseline"  # confirms the fixed-order trap is actually exercised
    headline = te.headline(item)
    assert not headline.lower().startswith("synthetic")
    assert headline.lower().startswith(("calls", "puts"))
    assert te.dominant_label(item) != "SYNTHETIC BASELINE"
    assert te.dominant_label(item) == "OPTIONS"
    read = te.current_read(item)
    assert not read.lower().startswith("a synthetic baseline")
    assert "unusual options volume" in read.lower()
    assert "no decision weight" in read.lower()  # synthetic disclosed, but subordinate
    assert "no second independent category corroborates" in read.lower()  # still only 1 real category
    ev = te.evidence(item)
    syn_bullets = [e for e in ev if e["family"] == "synthetic_baseline"]
    assert syn_bullets and syn_bullets[0]["key"] is False  # never "key"/qualifying evidence


def test_real_dark_pool_plus_synthetic_combined_still_leads_with_dark_pool():
    item = _item(gates_passed=["dark_pool_anomaly", "historical_percentile"],
                 dp={"ratio": 4.5, "hist": None, "prints": None},
                 gates_metrics={"historical_percentile": {"percentile": 0.92}})
    assert te.headline(item).lower().startswith("dark pool")
    assert te.dominant_label(item) == "DARK POOL"
    read = te.current_read(item)
    assert read.lower().startswith("dark-pool activity well above normal")
    assert "no decision weight" in read.lower()


# ---------------------------------------------------------------------------
# #33 (2026-10-15 round 2): CATX's real fixture (options_flow_anomaly +
# catalyst_identified -- genuinely CONFIRMED/"high"/"multi-signal" per
# signal_engine.INDEPENDENT_SIGNAL_GATES) used to only ever surface 1
# tip_evidence category ("options") because catalyst_identified had no
# family/category mapping at all -- directly contradicting its own engine-
# computed multi-signal state. Fixed by giving catalyst_identified its own
# "catalyst" family/category, matching INDEPENDENT_SIGNAL_GATES exactly.
# ---------------------------------------------------------------------------

def test_catalyst_identified_is_its_own_family_and_category():
    """catalyst_identified is read from its own dedicated item field, NOT
    gates_passed -- scan_service.evaluate_ticker_scan deliberately excludes
    catalyst_identified from gates_passed (see app/main.py's
    _to_evidence_item), so a realistic fixture must set the field directly,
    exactly like the real pipeline does."""
    item = _options_item(gates_passed=["options_flow_anomaly"], catalyst_identified=True)
    fam = te.families(item)
    assert "catalyst" in fam
    cats = te.categories(item)
    assert "catalyst" in cats
    assert len(cats) == 2  # options + catalyst -> genuinely 2 independent categories


def test_catalyst_identified_in_gates_passed_alone_is_not_enough():
    """Negative check for the real root cause: if only gates_passed (not
    the dedicated field) carries catalyst_identified, it must NOT be
    recognized -- confirms this isn't accidentally reading gates_passed."""
    item = _options_item(gates_passed=["options_flow_anomaly", "catalyst_identified"])
    assert "catalyst" not in te.families(item)


def test_catx_style_fixture_is_now_multi_source_consistent_with_the_engine():
    """Matches CATX's real fixture shape (bullish call sweep + manually
    curated catalyst_identified=true, no dark pool) exactly as it flows
    through the real pipeline (app/main.py._to_evidence_item sets
    catalyst_identified from scan["gates"], NOT from the filtered
    gates_passed list). Before the fix this produced
    dominant_label=="OPTIONS" (single-source) despite the engine computing
    CONFIRMED/high/multi-signal for the exact same gates -- a visible
    self-contradiction on the same page."""
    item = _options_item(gates_passed=["options_flow_anomaly"], catalyst_identified=True,
                         state="CONFIRMED")
    assert te.dominant_label(item) == "MULTI-SOURCE"
    ev = te.evidence(item)
    catalyst_bullets = [e for e in ev if e["family"] == "catalyst"]
    assert catalyst_bullets
    assert "manually curated" in catalyst_bullets[0]["text"].lower()
    assert catalyst_bullets[0]["key"] is True  # genuinely part of why this qualified


def test_catalyst_alone_with_no_other_real_evidence_leads_current_read_honestly():
    item = _item(catalyst_identified=True)
    read = te.current_read(item)
    assert "catalyst" in read.lower()
    assert te.dominant_label(item) == "CATALYST"


def test_two_real_categories_plus_synthetic_is_multi_source_not_synthetic():
    """Two genuinely independent real categories (dark_pool + options) plus
    the synthetic placeholder must be MULTI-SOURCE, with the "no second
    category" disclosure absent (there IS a second real category)."""
    item = _options_item(gates_passed=["dark_pool_anomaly", "options_flow_anomaly", "historical_percentile"],
                         dp={"ratio": 5.0, "hist": None, "prints": None},
                         gates_metrics={"historical_percentile": {"percentile": 0.9}})
    assert te.dominant_label(item) == "MULTI-SOURCE"
    read = te.current_read(item)
    assert "no second independent category corroborates" not in read.lower()
