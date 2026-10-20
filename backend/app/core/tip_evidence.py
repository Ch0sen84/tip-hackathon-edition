#!/usr/bin/env python3
"""TIP shared evidence -> explanation logic (Market Radar / Ticker Analysis).

Ported VERBATIM from the production TIP app's `tip_evidence.py`. That file
has zero imports and zero DB/network/clock/environment access -- it is pure
by construction, so no adaptation was needed to run it standalone here. Only
this header comment and the module docstring below were lightly trimmed of
references to production-only views (EOD Brief / Morning Brief) that this
hackathon app does not include; all function bodies, thresholds, and logic
are identical to production.

PURE MODULE: no database, network, clock or environment access. Every function
takes plain dicts/numbers and returns plain dicts/strings, so the same logic can
be fed fixture data in tests and produce identical output to what the
production app would show for the same inputs.

What this module decides
  * which per-ticker evidence counts as "noteworthy" (entry criteria),
  * how surfaced names are ordered and split into HIGHEST ATTENTION / OTHER,
  * the human-readable reasons ("why TIP surfaced this"),
  * a short deterministic CURRENT READ,
  * an UNRESOLVED QUESTION only when a specific, checkable one exists.

What it deliberately does NOT do
  * no public score, probability, conviction, expected return or BUY/SELL;
    the internal ordering key is never displayed;
  * never converts one measurement into another (options volume is not
    premium, dark-pool notional is not direction, alert counts are not flow
    direction);
  * never asserts causation from a headline or event (context only);
  * research status is supporting context only (FloorTrade = provisional,
    not validated).

Evidence item schema (input to evidence()/current_read()/rank_key()):
  ticker            str
  state             lifecycle state from TIP's existing signal engine or None
  gates_passed      list of passed anomaly gate names this session
  dp                {"ratio": float|None, "hist": {...}|None, "prints": (n, observed_premium)} or None
  options           options_volume snapshot (see options_from_rows below) or None
  options_partial   True when the session is still in progress (today's partial
                    volume vs a FULL-day 7-day average -> understates activity)
  price             {"chg_pct": float, "last": float} or None
  alerts            alert_activity() row (see below) or None
  context           {"kind": CONFIRMED_EVENT|POSSIBLE_CONTEXT|NO_CLEAR_CATALYST|UNAVAILABLE, "text": str} or None
  next_earnings_date ISO date string or None
  research          list of short research-status strings or []

alerts row (counts/premiums of UW flow alerts for the ticker in this session,
up to the same ET time of day as the baseline):
  n, prem, call_prem, put_prem, ask_prem, bid_prem, max_vol_oi,
  floor_n, floor_rules (list), base_n, base_prem (averages over base_days
  prior sessions at the same time-of-day cutoff), base_days
"""

EVIDENCE_VERSION = "tip_evidence_v1"
RADAR_ENTRY_VERSION = "market_radar_entry_v2"
RADAR_CATEGORY_VERSION = "market_radar_categories_v1"
CARRY_RULE_VERSION = "carry_forward_v2_unresolved_question"

# ---- thresholds (structural/presentation choices, NOT fitted to returns) ----
OPTIONS_HOT_PCT = 50          # existing brief rule: a side >=50% above its 7-day average is "hot"
DP_STRONG_RATIO = 3.0         # existing carry-forward rule: dark pool >=3x recent average
ALERT_SURGE_MIN_N = 20        # UW alerts: at least 20 alerts so far this session ...
ALERT_SURGE_MULT = 4.0        # ... and >=4x the same-time-of-day average of recent sessions
ALERT_PREM_MIN = 2_000_000    # or: >= $2M alert premium so far ...
ALERT_PREM_MULT = 5.0         # ... and >=5x the same-time-of-day average premium
MIN_BASELINE_DAYS = 3         # fewer prior sessions -> no "x times normal" claim (thin history)
ONE_SIDED_SHARE = 0.70        # call/put or ask/bid share worth stating as a fact
PRICE_MOVE_PCT = 1.0          # existing brief rule: |move| >= 1% is a move, else "little changed"
HIGHEST_MAX = 5               # visual emphasis only; never a minimum
INDEX_CONTEXT_TICKERS = ("SPY", "QQQ")  # eligible under the same rules; labelled, never forced
FLOOR_TRADE_RULES = ("FloorTradeSmallCap", "FloorTradeMidCap", "FloorTradeLargeCap")  # frozen UW family
STATE_RANK = {"CONFIRMED": 3, "STRENGTHENING": 3, "BUILDING": 2, "NEW": 1}
# historical_percentile removed (signal-integrity remediation): the synthetic
# estimator can never make a ticker qualify for Market Radar.
ANOMALY_GATES = ("dark_pool_anomaly", "options_flow_anomaly")
FAMILY_LABELS = {"dark_pool": "Dark pool", "options": "Options", "alerts": "UW alerts", "floor": "FloorTrade",
                 "synthetic_baseline": "Synthetic baseline estimate"}


# ------------------------------------------------------------- formatting --
def fmt_money(x):
    if x is None:
        return "n/a"
    x = float(x)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}" if unit == "B" else f"${x / div:,.0f}{unit}"
    return f"${x:,.0f}"


def fmt_pct(x, d=0):
    if x is None:
        return "n/a"
    x = round(float(x), d) + 0.0  # avoid "-0.0%"
    return f"{x:+.{d}f}%"


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---------------------------------------------------- options / dark pool --
def hot_side(o, thresh=OPTIONS_HOT_PCT):
    if not o or o.get("call_vs7") is None:
        return None
    side = "calls" if o["call_vs7"] >= o["put_vs7"] else "puts"
    return side if max(o["call_vs7"], o["put_vs7"]) >= thresh else None


def premium_side(o):
    if not o or o.get("skew") is None or abs(o["skew"]) <= 0.1:
        return None
    return "calls" if o["skew"] > 0 else "puts"


def options_from_rows(rows, session_date_iso=None):
    """Parse UW per-ticker options-volume rows into the options dict used everywhere
    (same math TIP's briefs have always used). Only the row for exactly session_date
    counts; returns None when there is no such row (data unavailable, not a negative)."""
    rows = rows or []
    if session_date_iso is not None:
        rows = [r for r in rows if r.get("date") == session_date_iso]
    if not rows:
        return None
    r = rows[0]
    f = lambda k: float(r.get(k) or 0)  # noqa: E731
    cv, pv = f("call_volume"), f("put_volume")
    a7c, a7p = f("avg_7_day_call_volume"), f("avg_7_day_put_volume")
    bull, bear = f("bullish_premium"), f("bearish_premium")
    return {"date": r.get("date"), "call_vol": cv, "put_vol": pv,
            "call_vs7": (cv / a7c - 1) * 100 if a7c else None,
            "put_vs7": (pv / a7p - 1) * 100 if a7p else None,
            "bull": bull, "bear": bear,
            "skew": (bull - bear) / (bull + bear) if (bull + bear) else None}


def options_bits(o):
    """Factual options phrases; volume and premium are reported separately."""
    if not o:
        return []
    parts = []
    if o.get("call_vs7") is not None and o.get("put_vs7") is not None:
        parts.append(f"calls {fmt_pct(o['call_vs7'])}, puts {fmt_pct(o['put_vs7'])} vs 7-day avg")
    if o.get("skew") is not None:
        side = "bullish-tilted" if o["skew"] > 0.1 else "bearish-tilted" if o["skew"] < -0.1 else "balanced"
        parts.append(f"premium {side} ({fmt_money(o['bull'])} bull / {fmt_money(o['bear'])} bear)")
    return parts


def options_read(o):
    if not o or o["call_vs7"] is None:
        return None
    hot = max(o["call_vs7"], o["put_vs7"]) >= OPTIONS_HOT_PCT
    cold = max(o["call_vs7"], o["put_vs7"]) <= 0
    if cold:
        return "options activity did not confirm it (both sides at or below normal)"
    if hot and o["skew"] is not None:
        vol_side = "calls" if o["call_vs7"] > o["put_vs7"] else "puts"
        prem_side = "calls" if o["skew"] > 0 else "puts"
        if vol_side == prem_side:
            return f"options agree ({vol_side} lead volume and premium)"
        return f"options mixed ({vol_side} lead volume, {prem_side} lead premium)"
    return "options activity was only modestly above normal"


def dp_bits(ratio, hist, prints):
    """Dark-pool phrases. z-score withheld below n=10 history; capped sums are floors."""
    n, prem = prints or (0, 0.0)
    bits = []
    if ratio:
        bits.append(f"~{float(ratio):.1f}x its recent average")
    if hist and hist.get("window_n", 0) >= 10 and hist.get("z_score") is not None:
        bits.append(f"z={hist['z_score']:.1f} over {hist['window_n']} sessions")
    elif ratio:
        bits.append("short history")
    if prem:
        bits.append(f"observed \u2265{fmt_money(prem)} in {n} large prints (sample cap)")
    return bits


def move_phrase(price):
    if not price or price.get("chg_pct") is None:
        return None
    c = price["chg_pct"]
    if abs(c) >= PRICE_MOVE_PCT:
        return f"stock {'rose' if c > 0 else 'fell'} {abs(c):.1f}%"
    return "stock little changed"


# --------------------------------------------------------- carry / question --
def carry_reason(b, next_session):
    """Carry a name only for a specific unresolved analytical question that the
    next session can help answer. Agreement between dark pool and options is
    NOT required -- divergence is itself a question. Returns (code, text) or None.
    Never carried merely for appearing in a report or being on a watchlist."""
    o, px = b.get("options"), b.get("price") or {}
    chg = px.get("chg_pct")
    dp = float(b.get("dp_ratio") or 0)
    vol_side, prem_side = hot_side(o), premium_side(o)
    ned = b.get("next_earnings_date")
    if ned and (dp >= 3 or vol_side) and str(ned) == next_session.isoformat():
        return ("FLOW_PLUS_CATALYST", f"unusual flow into earnings {next_session.strftime('%a')}; "
                "next session shows how the positioning meets the event")
    if dp >= 3 and vol_side and prem_side and vol_side != prem_side:
        return ("DP_OPTIONS_DIVERGENCE", f"options split under heavy dark pool ({vol_side} led volume, "
                f"{prem_side} led premium); which side follows through?")
    if dp >= 3 and vol_side and prem_side == vol_side:
        return ("DP_PLUS_OPTIONS_CORROBORATE", f"dark pool and {vol_side}-led options both unusual"
                + ("; price hasn't responded yet" if chg is not None and abs(chg) < 1 else
                   "; checks whether the move extends"))
    if o and vol_side and max(o["call_vs7"], o["put_vs7"]) >= 100 and (chg is None or abs(chg) < 1):
        return ("OPTIONS_ANOMALY_UNRESOLVED", f"{vol_side} volume ~{max(o['call_vs7'], o['put_vs7']):.0f}% above "
                "normal with no price response yet")
    if dp >= 10 and chg is not None and abs(chg) < 0.5:
        return ("FLOW_NO_PRICE_RESPONSE", f"~{dp:.0f}x normal dark-pool notional with the stock flat; "
                "next session tests whether it was absorbed or is still building")
    return None


# ------------------------------------------------------------ UW alerts ----
def alert_multiples(a):
    """(count multiple, premium multiple) vs same-time baseline, or None when the
    baseline is thin. A ticker with no recent alerts at all has baseline 0 ->
    multiple reported as None and only absolute counts are used."""
    if not a or (a.get("base_days") or 0) < MIN_BASELINE_DAYS:
        return None, None
    bn, bp = a.get("base_n") or 0, a.get("base_prem") or 0
    return (a["n"] / bn if bn > 0 else None), (a["prem"] / bp if bp > 0 else None)


def alert_surge(a):
    """True when UW flow alerts are running well above this ticker's own recent pace."""
    if not a:
        return False
    thin = (a.get("base_days") or 0) < MIN_BASELINE_DAYS
    if thin:
        return False
    mn, mp = alert_multiples(a)
    no_base_n = (a.get("base_n") or 0) == 0
    no_base_p = (a.get("base_prem") or 0) == 0
    by_n = a["n"] >= ALERT_SURGE_MIN_N and (no_base_n or (mn is not None and mn >= ALERT_SURGE_MULT))
    by_p = (a["prem"] or 0) >= ALERT_PREM_MIN and (no_base_p or (mp is not None and mp >= ALERT_PREM_MULT))
    return by_n or by_p


def alert_premium_surge(a):
    """Size-based UW path: >= ALERT_PREM_MIN alert premium AND >= ALERT_PREM_MULT x the
    ticker's same-time-of-day baseline premium (or no baseline premium at all, with a
    sufficient baseline history). This is the only UW-alert route that can QUALIFY a name."""
    if not a or (a.get("base_days") or 0) < MIN_BASELINE_DAYS:
        return False
    if (a.get("prem") or 0) < ALERT_PREM_MIN:
        return False
    bp = a.get("base_prem") or 0
    return bp == 0 or a["prem"] / bp >= ALERT_PREM_MULT


def uw_flow_current_read(a, session_live):
    """Ticker Analysis: the Evidence Overview UW FLOW card's one-line
    read. Pure assembly over alert_bits() -- the exact same wording Market
    Radar uses -- never a second interpretation of what 'unusual' means."""
    if not a or not a.get("n"):
        return "No UW Flow history available."
    bits = alert_bits(a, session_live)
    return " \u00b7 ".join(bits) if bits else "UW flow is near its normal pace."


def _share(x, y):
    t = (x or 0) + (y or 0)
    return (x or 0) / t if t > 0 else None


ALERT_NORMAL_MIN_N = 5        # below this, a non-surging alert count is too small to mention


def alert_bits(a, session_live):
    """Full alert detail only when alerts are themselves unusual; otherwise a single
    'at their usual pace' line (useful disconfirmation) or nothing."""
    if not a or not a.get("n"):
        return []
    when = "so far today" if session_live else "this session"
    mn, mp = alert_multiples(a)
    by = "by this time" if session_live else "per session"
    if not alert_surge(a):
        if a["n"] >= ALERT_NORMAL_MIN_N and mn is not None:
            return [f"UW flow alerts at about their usual pace ({a['n']} {when} vs ~{a['base_n']:.0f} typical)"]
        return []
    bits = []
    if mn is not None:
        bits.append(f"{_plural(a['n'], 'UW flow alert')} {when} vs ~{a['base_n']:.1f} typical {by} ({mn:.1f}x)")
    elif (a.get("base_days") or 0) >= MIN_BASELINE_DAYS:
        bits.append(f"{_plural(a['n'], 'UW flow alert')} {when} (none {by} on recent sessions)" if session_live else f"{_plural(a['n'], 'UW flow alert')} {when} (none on recent sessions)")
    else:
        bits.append(f"{_plural(a['n'], 'UW flow alert')} {when} (short history, no baseline)")
    if a.get("prem"):
        s = f"{fmt_money(a['prem'])} alert premium"
        if mp is not None:
            s += f" vs ~{fmt_money(a['base_prem'])} typical ({mp:.1f}x)"
        bits.append(s)
    cs = _share(a.get("call_prem"), a.get("put_prem"))
    if cs is not None and (cs >= ONE_SIDED_SHARE or cs <= 1 - ONE_SIDED_SHARE):
        side, share = ("calls", cs) if cs >= 0.5 else ("puts", 1 - cs)
        bits.append(f"{side} {share * 100:.0f}% of alert premium")
    ask = _share(a.get("ask_prem"), a.get("bid_prem"))
    if ask is not None and (ask >= ONE_SIDED_SHARE or ask <= 1 - ONE_SIDED_SHARE):
        side, share = ("ask", ask) if ask >= 0.5 else ("bid", 1 - ask)
        bits.append(f"{share * 100:.0f}% of alert premium printed on the {side} side")
    if (a.get("max_vol_oi") or 0) >= 5:
        bits.append(f"a contract traded ~{a['max_vol_oi']:.0f}x its open interest")
    return bits


# ---------------------------------------------------------------- families --
def families(item):
    """Independent evidence families firing for this ticker (ordering/tiering only).

    PRESENTATION BUG FIX (2026-10-15, Priority 1 -- Chad's Live-Mode
    diagnostic): `historical_percentile` used to be folded into the same
    "dark_pool" family as the REAL `dark_pool_anomaly` gate. But
    `historical_percentile`'s default implementation is
    `signal_engine.synthetic_percentile_gate` -- a SYNTHETIC/STATIC premium-
    size bucket estimator, NOT real historical dark-pool data (see that
    function's own docstring). Folding it into "dark_pool" meant a ticker
    whose real dark_pool_anomaly gate FAILED (bootstrap/insufficient
    baseline -- confirmed structurally always true in this build's Live
    Mode, since real UW darkpool trades carry no baseline field) could
    still show "Dark-pool activity above normal" purely because the
    synthetic placeholder gate passed on premium size alone. Kept as its
    own family below so it is labeled honestly and never rendered as real
    dark-pool evidence.
    """
    f = []
    gp = set(item.get("gates_passed") or [])
    if "dark_pool_anomaly" in gp:
        f.append("dark_pool")
    if "historical_percentile" in gp:
        f.append("synthetic_baseline")
    if hot_side(item.get("options")) or "options_flow_anomaly" in gp:
        f.append("options")
    if alert_surge(item.get("alerts")):
        f.append("alerts")
    if (item.get("alerts") or {}).get("floor_n"):
        f.append("floor")
    # BUG FIX (#33, 2026-10-15 round 2): catalyst_identified is one of
    # signal_engine.INDEPENDENT_SIGNAL_GATES -- the engine already treats a
    # manually-verified real catalyst as a genuinely independent category
    # for multi_signal_confirmation/CONFIRMED/"high" confidence (see
    # docs/SIGNAL_INTEGRITY_RULES.md rule 2). Before this fix, tip_evidence
    # had no family/category for it at all, so a ticker like CATX (real
    # options_flow_anomaly + catalyst_identified -> genuinely CONFIRMED,
    # "high" confidence, signal_type "multi-signal" per the engine) still
    # showed only 1 category here ("options") and was excluded from
    # Highest Attention / labeled "OPTIONS" instead of multi-source --
    # directly contradicting its own engine-computed state on the same
    # page. Recognizing catalyst_identified as its own family/category
    # aligns tip_evidence with the ALREADY-APPROVED INDEPENDENT_SIGNAL_GATES
    # set; it does not add or change any signal_engine threshold/semantics.
    #
    # NOTE: deliberately read from the dedicated `catalyst_identified` item
    # field, NOT `gates_passed` -- scan_service.evaluate_ticker_scan's
    # gates_passed list intentionally EXCLUDES catalyst_identified (along
    # with multi_signal_confirmation/data_quality) so it never appears as a
    # generic "TIP anomaly gate passed" bullet / never trips qualifies()'s
    # ANOMALY_GATES check on its own. Checking gates_passed here would
    # silently never fire through the real app pipeline (it only ever
    # would in a hand-built test item that sets gates_passed directly,
    # bypassing scan_service) -- see app/main.py's _to_evidence_item.
    if item.get("catalyst_identified"):
        f.append("catalyst")
    # TIP lifecycle state (CONFIRMED etc.) is persistence of the same gate evidence,
    # so it orders names but is NOT counted as an independent kind of evidence.
    return f


# Distinct evidence CATEGORIES (what Highest Attention and ordering count). Families are
# descriptive tags; several tags can describe the same underlying data source:
#   dark_pool -> TIP dark-pool gates (dark-pool prints)
#   options   -> aggregate per-ticker options volume/premium (UW options-volume snapshot or
#                TIP's options_flow_anomaly gate) -- a separate dataset from UW flow alerts
#   alerts, floor -> both are UW flow-alert records, so together they are ONE category
#   catalyst  -> signal_engine's catalyst_identified gate (manually-verified real catalyst,
#                see the families() note above -- matches INDEPENDENT_SIGNAL_GATES exactly)
CATEGORY_OF = {"dark_pool": "dark_pool", "options": "options", "alerts": "uw_flow", "floor": "uw_flow",
               "synthetic_baseline": "synthetic_baseline", "catalyst": "catalyst"}
CATEGORY_LABELS = {"dark_pool": "dark-pool activity", "options": "broader options activity", "uw_flow": "UW-flow context",
                    "synthetic_baseline": "synthetic baseline estimate (not real historical data)",
                    "catalyst": "a manually-identified catalyst"}


def categories(item):
    """Distinct evidence categories, in fixed order, each counted once.

    SIGNAL-INTEGRITY RULE CHANGE (2026-10-15, matching signal_engine.py's
    confirmation-math rule change): `synthetic_baseline` is EXCLUDED from
    this tally. `present()`'s "Highest Attention" split (and rank_key's
    ordering) requires >= 2 distinct categories -- counting the SYNTHETIC
    historical_percentile placeholder as one of those 2 would let a ticker
    with only ONE real evidence family (e.g. dark_pool) plus the synthetic
    placeholder get emphasised as "MULTI-SOURCE"/Highest Attention, exactly
    the zero-weight-for-synthetic-gates rule signal_engine.py now enforces
    for confirmation/confidence. The synthetic_baseline family itself is
    UNCHANGED everywhere else (still shown, still labeled, still a real
    `families()`/`evidence()`/`dominant_label()` entry) -- only its
    contribution to the >=2-category COUNT is removed.
    """
    out = []
    for f in families(item):
        if f == "synthetic_baseline":
            continue
        c = CATEGORY_OF[f]
        if c not in out:
            out.append(c)
    return out


def qualifies(item):
    """Market Radar entry (swing-oriented, rule set market_radar_entry_v2):
      * an existing TIP anomaly gate passed this session, OR
      * meaningful UW alert premium: alert_premium_surge().
    Alert COUNT/PACE alone and FloorTrade-family activity alone do NOT qualify;
    both remain evidence (and count toward Highest Attention) once a name is in.
    Used only by Market Radar."""
    gp = set(item.get("gates_passed") or [])
    return bool(gp & set(ANOMALY_GATES)) or alert_premium_surge(item.get("alerts"))


def rank_key(item):
    """Internal deterministic ordering. NEVER displayed; not a score of anything."""
    fam = families(item)
    cats = categories(item)
    dp = min(float((item.get("dp") or {}).get("ratio") or 0), 50)
    mn, mp = alert_multiples(item.get("alerts"))
    am = min(max(mn or 0, mp or 0), 50)
    o = item.get("options") or {}
    op = min(max(o.get("call_vs7") or 0, o.get("put_vs7") or 0) / 50, 10) if o.get("call_vs7") is not None else 0
    strength = dp + am + op + (10 if "floor" in fam else 0)
    return (-len(cats), -STATE_RANK.get(item.get("state"), 0), -round(strength, 6), item["ticker"])


# ------------------------------------------------------------- explanation --
def _short(family, item):
    """Compact, magnitude-first version of a family's evidence (presentation only).
    Never states a multiple that alert_multiples()/dp ratio did not support."""
    if family == "dark_pool":
        dp = item.get("dp") or {}
        r, h = dp.get("ratio"), dp.get("hist") or {}
        if not r:
            return "Dark-pool activity above normal"
        thin = not (h.get("window_n", 0) >= 10 and h.get("z_score") is not None)
        return f"Dark pool ~{float(r):.1f}x recent average" + (" (short history)" if thin else "")
    if family == "options":
        o = item.get("options") or {}
        if o.get("call_vs7") is None:
            return "Options flow anomaly"
        side, v = ("Calls", o["call_vs7"]) if o["call_vs7"] >= o["put_vs7"] else ("Puts", o["put_vs7"])
        return f"{side} volume {fmt_pct(v)} vs 7-day avg"
    if family == "alerts":
        a = item.get("alerts") or {}
        mn, mp = alert_multiples(a)
        if alert_premium_surge(a) and mp is not None:  # lead with the qualifying (size) fact
            return f"UW alert premium {mp:.1f}x normal ({fmt_money(a['prem'])})"
        if mn is not None and (mp is None or mn >= mp):
            return f"UW alert pace {mn:.1f}x normal ({a['n']} vs ~{a['base_n']:.1f})"
        if mp is not None:
            return f"UW alert premium {mp:.1f}x normal ({fmt_money(a['prem'])})"
        return f"{_plural(a.get('n', 0), 'UW flow alert')} (none on recent sessions)"
    if family == "floor":
        n = (item.get("alerts") or {}).get("floor_n") or 0
        return f"FloorTrade-family activity ({_plural(n, 'alert')})"
    if family == "synthetic_baseline":
        m = (item.get("gates_metrics") or {}).get("historical_percentile") or {}
        pct = m.get("percentile")
        pct_txt = f"{pct:.0%}" if isinstance(pct, (int, float)) else "elevated"
        return f"Synthetic baseline estimate: {pct_txt} (static premium-size model, not real historical data)"
    if family == "catalyst":
        return "Catalyst identified (manually curated, not auto-detected)"
    return None


def headline(item):
    """One-line magnitude-first summary for compact rows: the qualifying evidence
    kinds in their fixed order. Presentation only; changes no selection/ordering.

    PRESENTATION BUG FIX (#35, 2026-10-15 round 2): `families(item)`'s own
    iteration order puts `synthetic_baseline` BEFORE `options`/`alerts`/
    `floor` (it is checked right after `dark_pool_anomaly`). This function
    used to build its parts straight from that order, so any ticker with
    real options/alerts/floor evidence PLUS the synthetic placeholder had
    the placeholder's "Synthetic baseline estimate: ..." text leading the
    headline, ahead of the real finding -- making a zero-weight fabricated
    metric look like the primary result. Real families are now always
    placed first (in their existing relative order), with
    `synthetic_baseline` moved to the end regardless of where `families()`
    put it -- it can still appear, just never lead, and never when any real
    evidence exists to lead with instead.
    """
    fam = families(item)
    ordered = [f for f in fam if f != "synthetic_baseline"]
    if "synthetic_baseline" in fam:
        ordered.append("synthetic_baseline")
    parts = [_short(f, item) for f in ordered]
    parts = [p for p in parts if p]
    if not parts:
        gp = item.get("gates_passed") or []
        # Signal-integrity remediation: with no evidence family at all, the
        # old "Unusual activity" fallback asserted something nothing showed.
        parts = (["TIP anomaly gate passed: " + ", ".join(g.replace("_", " ") for g in gp)] if gp
                 else ["No qualifying evidence this scan"])
    return " \u00b7 ".join(parts)


def dominant_label(item):
    """BUG FIX (#35): this used to key off `len(families(item)) >= 2`
    without excluding `synthetic_baseline`, and fell back to `fam[0]` when
    not -- both are family-ARITY checks that can be satisfied by a
    synthetic+real pair with no real second category at all. A ticker whose
    only families were ["synthetic_baseline", "options"] used to get
    labeled "UW FLOW" (wrong; that label means alerts+floor) via the first
    bypass, or "SYNTHETIC BASELINE" via `fam[0]` even though real options
    evidence existed -- the placeholder must never be chosen as the
    primary/dominant label while real evidence exists. Every branch below
    now operates on `real_fam` (families() minus synthetic_baseline); the
    synthetic-only label is reachable only when NO real family fired.
    """
    fam = families(item)
    if len(categories(item)) >= 2:
        return "MULTI-SOURCE"
    real_fam = [f for f in fam if f != "synthetic_baseline"]
    if len(real_fam) >= 2:  # UW alerts + FloorTrade: one category
        return "UW FLOW"
    if real_fam:
        return {"dark_pool": "DARK POOL", "options": "OPTIONS", "alerts": "UW ALERTS",
                "floor": "FLOORTRADE", "catalyst": "CATALYST"}.get(real_fam[0], "OTHER")
    if "synthetic_baseline" in fam:
        return "SYNTHETIC BASELINE"
    return "OTHER"


def evidence(item, session_live=False):
    """List of {"family", "text", "short", "key"} reasons -- the 'why TIP surfaced this'.
    key=True marks the bullets that are the reason the name qualified (plus price);
    the rest is supporting context (shown on expansion).

    PRESENTATION BUG FIX (2026-10-15): the "Dark pool ..." bullet used to
    fire off EITHER the real dark_pool_anomaly gate OR the SYNTHETIC
    historical_percentile placeholder gate, rendering the synthetic gate's
    pass as if it were real dark-pool evidence (see families() docstring
    for the full writeup). Split into two honestly-labeled bullets below --
    a real "Dark pool ..." bullet only when dark_pool_anomaly itself passed,
    and a separate, explicitly-disclosed "Synthetic baseline estimate ..."
    bullet when historical_percentile passed, never conflated.
    """
    out = []
    dp = item.get("dp") or {}
    gp = set(item.get("gates_passed") or [])
    if "dark_pool_anomaly" in gp:
        b = dp_bits(dp.get("ratio"), dp.get("hist"), dp.get("prints"))
        if b:
            out.append({"family": "dark_pool", "text": "Dark pool " + "; ".join(b)})
    if "historical_percentile" in gp:
        out.append({"family": "synthetic_baseline",
                    "text": _short("synthetic_baseline", item) + " -- this is a static estimator built "
                    "into this demo build, not a real trailing-history comparison."})
    o = item.get("options")
    if item.get("options_status") == "UNAVAILABLE":
        out.append({"family": "options_unavailable",
                     "text": "Options data unavailable for this build (options evidence could not be assessed)"})
    ob = options_bits(o)
    if ob:
        txt = "Options " + "; ".join(ob)
        if item.get("options_partial"):
            txt += " (partial session vs full-day average \u2014 understates)"
        out.append({"family": "options", "text": txt})
    ab = alert_bits(item.get("alerts"), session_live)
    if ab:
        out.append({"family": "alerts", "text": "; ".join(ab)})
    a = item.get("alerts") or {}
    if a.get("floor_n"):
        rules = ", ".join(sorted(set(a.get("floor_rules") or [])))
        out.append({"family": "floor", "text": f"{a['floor_n']} FloorTrade-family alert{'s' if a['floor_n'] > 1 else ''}"
                    + (f" ({rules})" if rules else "")})
    if item.get("catalyst_identified"):
        out.append({"family": "catalyst",
                    "text": "Catalyst identified -- manually curated for this ticker, never "
                    "auto-detected by TIP from any UW feed."})
    if item.get("state") in STATE_RANK:
        out.append({"family": "lifecycle", "text": f"TIP signal state {item['state']}"})
    px = item.get("price")
    if px and px.get("chg_pct") is not None:
        out.append({"family": "price", "text": f"Price {fmt_pct(px['chg_pct'], 1)}"
                    + (f" at {px['last']:,.2f}" if px.get("last") else "")})
    ctx = item.get("context") or {}
    if ctx.get("kind") in ("CONFIRMED_EVENT", "POSSIBLE_CONTEXT"):
        out.append({"family": "context", "text": ctx["text"]})
    fam = set(families(item))
    for e in out:
        # BUG FIX (#35): `synthetic_baseline` is a member of `fam` (it's a
        # real, tracked evidence family for ordering/labeling purposes) but
        # must NEVER count as "key"/qualifying evidence -- it carries zero
        # decision weight (see docs/SIGNAL_INTEGRITY_RULES.md rule 1) and
        # must always render as supporting/disclosed context only, however
        # genuine the real evidence around it looks.
        e["key"] = (e["family"] != "synthetic_baseline" and e["family"] in fam) or e["family"] == "price" or (
            e["family"] == "context" and ctx.get("kind") == "CONFIRMED_EVENT")
        e["short"] = _short(e["family"], item) if e["family"] in fam else e["text"]
    return out


def current_read(item):
    """Short deterministic interpretation. Describes agreement/disagreement and
    price response; never asserts direction the data does not support.

    PRESENTATION BUG FIX (2026-10-15, Priority 1): this used to assert
    "dark-pool activity well above normal" unconditionally whenever the
    dark_pool family fired -- including (before the families() fix above)
    when that family only ever fired off the SYNTHETIC historical_percentile
    placeholder, with no real dark-pool ratio/history behind it at all. Now
    the "dark_pool" family can only fire from the real dark_pool_anomaly
    gate (which itself can only pass with a real, non-bootstrapped ratio --
    see signal_engine.evaluate_gates), so this phrase is only ever written
    when backed by a real ratio. The synthetic placeholder gets its own,
    explicitly-labeled lead phrase instead of being folded into this one.

    PRESENTATION BUG FIX (#35, 2026-10-15 round 2): the lead-phrase logic
    used to check `"synthetic_baseline" in fam` BEFORE the `options`
    fallback (`if not lead and "options" in fam`). When the only families
    were ["synthetic_baseline", "options"], the synthetic branch populated
    `lead` first, so the real options finding was silently never mentioned
    anywhere in the read at all -- not even as a fallback. Real families
    (dark_pool/alerts/floor/options) are now evaluated into `lead`
    completely independently of synthetic_baseline; the synthetic phrase
    only becomes the LEAD when no real family fired, and otherwise is
    appended afterward as an explicit, clearly-subordinate disclosure that
    carries no decision weight. Separately (Priority 1c): when exactly one
    real evidence category exists (categories() < 2, i.e. not already
    MULTI-SOURCE), the read now explicitly discloses that no second
    independent category corroborates it yet -- so a single real finding
    never reads as if it were already confirmed/corroborated.
    """
    fam = families(item)
    cats = categories(item)
    real_fam = [f for f in fam if f != "synthetic_baseline"]
    lead = []
    if "dark_pool" in real_fam:
        lead.append("dark-pool activity well above normal")
    if "alerts" in real_fam:
        lead.append("UW flow alerts running well above this name's usual pace")
    if "floor" in real_fam:
        lead.append("FloorTrade-family prints")
    if "catalyst" in real_fam:
        lead.append("a manually-identified catalyst")
    if not lead and "options" in real_fam:
        lead.append("unusual options volume")
    synthetic_only = not lead and "synthetic_baseline" in fam
    if synthetic_only:
        lead.append("a synthetic baseline estimate flagged elevated premium (not a real historical comparison)")
    elif not lead:
        lead.append("unusual activity")
    parts = [" and ".join(lead)]
    read = options_read(item.get("options"))
    if read and lead != ["unusual options volume"]:
        parts.append(read)
    elif read and "agree" in read:
        parts.append(read)
    mv = move_phrase(item.get("price"))
    flows = {"dark_pool", "alerts", "floor", "options"} & set(fam)
    if mv == "stock little changed" and flows:
        parts.append("no corresponding price response")
    elif mv:
        parts.append(mv)
    if not synthetic_only and "synthetic_baseline" in fam:
        parts.append("a synthetic baseline estimate also flagged elevated premium, but it carries no decision weight")
    if real_fam and len(cats) < 2:
        parts.append("no second independent category corroborates this yet")
    s = "; ".join(parts) + "."
    return s[0].upper() + s[1:]


def open_question(item, next_session):
    """A specific checkable question, or None. Reuses the EOD carry-forward rule
    so every view agrees about what is unresolved."""
    b = {"options": item.get("options"), "price": item.get("price"),
         "dp_ratio": (item.get("dp") or {}).get("ratio") if "dark_pool" in families(item) else None,
         "next_earnings_date": item.get("next_earnings_date")}
    cr = carry_reason(b, next_session)
    if cr:
        return {"code": cr[0], "text": cr[1][0].upper() + cr[1][1:] + ("" if cr[1].endswith("?") else ".")}
    fam = families(item)
    px = item.get("price") or {}
    if "alerts" in fam and px.get("chg_pct") is not None and abs(px["chg_pct"]) < PRICE_MOVE_PCT:
        return {"code": "ALERTS_NO_PRICE_RESPONSE",
                "text": "Alert activity well above normal with the stock flat; does price follow next session?"}
    return None


def present(items, next_session, session_live=False, highest_max=HIGHEST_MAX):
    """Order qualifying items and split them into (highest, other). No minimum
    in either section. Highest Attention needs >= 2 distinct evidence CATEGORIES
    (categories(), not families); the strongest few (<= highest_max) are emphasised."""
    q = sorted((i for i in items if qualifies(i)), key=rank_key)
    out = []
    for i in q:
        out.append({"ticker": i["ticker"], "name": i.get("name"), "families": families(i),
                    "evidence": evidence(i, session_live), "current_read": current_read(i),
                    "headline": headline(i), "label": dominant_label(i), "categories": categories(i),
                    "index_context": i["ticker"] in INDEX_CONTEXT_TICKERS,
                    "question": open_question(i, next_session), "research": list(i.get("research") or []),
                    "enriched": bool(i.get("enriched", True)),
                    # BUG FIX (2026-10-15): present() rebuilds a brand-new dict per
                    # qualifying item and, until this fix, never copied the real
                    # `price` field (chg_pct/last) through from the input item --
                    # it was computed correctly upstream (scan_service /
                    # _to_evidence_item from a real stock_state) and then silently
                    # dropped here, so every Market Radar card always rendered
                    # fmtPriceLine(undefined) -> "price data unavailable in this
                    # scenario", even for the 11/11 fixtures that DO carry real
                    # stock_state.last_price/change_percent. Not a fixture-data or
                    # adapter bug -- a dropped field in this presentation layer.
                    "price": i.get("price")})
    multi = [x for x in out if len(x["categories"]) >= 2]
    highest = multi[:highest_max]
    hs = {x["ticker"] for x in highest}
    other = [x for x in out if x["ticker"] not in hs]
    return highest, other
