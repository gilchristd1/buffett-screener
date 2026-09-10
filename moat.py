"""
Moat durability, computed rather than declared.

The framework needs a moat width because it sets the margin of safety and gates
the quality credit on the hurdle. Until now that came from a hand-maintained
`moats.csv`, which meant in practice that every company defaulted to "narrow"
and no name ever received the wide-moat treatment.

A moat, though, is not an opinion you attach to a company — it is a *durable
excess return*, and the pipeline already extracts everything needed to measure
one. Six signals, all from data the screen has in hand by the time it ranks:

    level        median ROIC spread over the required return
    persistence  how many of the last ten years cleared that return
    trend        is the spread widening or eroding
    stability    operating-margin volatility
    profitability gross profit / assets, which travels across sectors
    share        revenue growth against the company's own sector

Persistence and trend are the two that distinguish a moat from a good decade.
Anyone can earn 25% for three years; earning it for ten while the spread holds
is what an advantage looks like from outside the business.

WHAT THIS CANNOT DO, stated plainly rather than buried: it measures the evidence
of a moat, not its source, and it is entirely backward-looking. Kodak and Nokia
would both have scored highly here on trailing ten-year data shortly before
their advantages evaporated. Nothing in arithmetic fixes that. It is why the
Tier 6 checklist exists and why the shortlist is read by a human — the score
sets the price you are willing to pay, not the decision to buy.
"""

from __future__ import annotations

import statistics

import config
import metrics as M
from secdata import AnnualSeries


def _band(value, thresholds, points):
    """Award `points[i]` for the first threshold the value clears."""
    if value is None:
        return None
    for t, p in zip(thresholds, points):
        if value >= t:
            return p
    return 0.0


def _slope(pairs: list[tuple[int, float]]) -> float | None:
    """Least-squares slope of y against x. Change in ROIC per year."""
    if len(pairs) < 4:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


# ------------------------------------------------------------- the score --
def durability(s: AnnualSeries, module: str,
               sector_revenue_cagr: float | None = None) -> tuple[float, list[tuple], float]:
    """
    Returns (score out of 100, components, points available).

    Components are (name, earned, available, note), the same shape the Tier 4
    score uses, so a thin tag record lowers coverage rather than the score.
    Coverage is reported because it feeds the margin of safety: knowing less
    about a company is itself a reason to demand a wider discount.
    """
    out: list[tuple] = []
    cap_rnd = config.SECTOR_MODULES.get(module, {}).get("capitalise_rnd", False)
    ys = M.common_years(s, ["operating_income", "total_equity"], 10)
    pairs = [(y, r) for y in ys if (r := M.roic(s, y, cap_rnd)) is not None]
    roics = [r for _, r in pairs]
    req = config.REQUIRED_RETURN

    # --- level (25) -------------------------------------------------------
    if roics:
        spread = statistics.median(roics) - req
        pts = _band(spread, [0.25, 0.18, 0.12, 0.07, 0.03], [25, 20, 14, 8, 3])
        out.append(("Excess return level", pts, 25,
                    f"median ROIC {statistics.median(roics):.1%}, {spread:+.1%} vs {req:.0%}"))
    else:
        out.append(("Excess return level", 0, 0, "no ROIC history"))

    # --- persistence (25) -------------------------------------------------
    # The signal that separates a moat from a good decade. A wide moat should
    # clear its cost of capital in essentially every year, including the bad ones.
    if len(roics) >= 5:
        hits = sum(1 for r in roics if r > req)
        frac = hits / len(roics)
        # Bands are deliberately brutal. Every company scored here has ALREADY
        # passed gates requiring returns above the cost of capital, so anything
        # short of a perfect record is the informative signal.
        pts = _band(frac, [1.0, 0.95, 0.85, 0.70], [25, 16, 8, 3])
        out.append(("Persistence of excess return", pts, 25,
                    f"{hits}/{len(roics)} years above {req:.0%}"))
    else:
        out.append(("Persistence of excess return", 0, 0,
                    f"only {len(roics)}y of ROIC history"))

    # --- trend (15) -------------------------------------------------------
    # A moat under attack shows up here before it shows up in the median.
    sl = _slope(pairs)
    if sl is not None:
        pts = _band(sl, [0.005, 0.0, -0.005, -0.010, -0.020], [15, 13, 10, 6, 2])
        out.append(("Trend in excess return", pts, 15,
                    f"{sl * 100:+.2f} ROIC points per year"))
    else:
        out.append(("Trend in excess return", 0, 0, "too little history to fit a trend"))

    # --- stability (10) ---------------------------------------------------
    cv = M.margin_stability(s)
    if cv is not None:
        pts = _band(-cv, [-0.015, -0.03, -0.05, -0.08], [10, 8, 5, 2])
        out.append(("Operating margin stability", pts, 10, f"std dev {cv:.1%}"))
    else:
        out.append(("Operating margin stability", 0, 0, "margin history unavailable"))

    # --- gross profitability (10) ----------------------------------------
    gps = [g for y in ys if (g := M.gross_profitability(s, y)) is not None]
    if gps:
        med = statistics.median(gps)
        # Also raised: C5 already gates on 20-40% by sector, so the old bands
        # gave full marks to two-thirds of survivors and carried no information.
        pts = _band(med, [0.60, 0.45, 0.32, 0.22], [10, 8, 5, 2])
        out.append(("Gross profitability", pts, 10, f"median GP/assets {med:.1%}"))
    else:
        # Normal for financials and fee businesses, which report no cost of revenue.
        out.append(("Gross profitability", 0, 0, "cost of revenue not reported"))

    # --- share gain (15) --------------------------------------------------
    # The one signal that needs the rest of the universe. Growing faster than
    # your own sector for a decade is share being taken, which is what an
    # advantage looks like from the outside.
    #
    # This is also a candidate to retire part of §6's manual "market position"
    # allocation, which is currently 6 of the 30 points that need a human. NOT
    # done here on purpose: the Tier 4 score already changes this run through
    # the DCF fade, and moving two things at once makes the run-over-run diff
    # unattributable. Durability feeds the buy price, not the score, so the two
    # effects stay separable.
    own = M.calendar_cagr(s, "revenue", 10)
    if own is not None and sector_revenue_cagr is not None:
        gap = own - sector_revenue_cagr
        pts = _band(gap, [0.03, 0.01, 0.0, -0.02], [15, 11, 8, 3])
        out.append(("Revenue growth vs sector", pts, 15,
                    f"{own:.1%} vs sector median {sector_revenue_cagr:.1%} ({gap:+.1%})"))
    else:
        out.append(("Revenue growth vs sector", 0, 0, "no sector benchmark"))

    earned = sum(c[1] for c in out)
    available = sum(c[2] for c in out)
    score = 100.0 * earned / available if available else 0.0
    return score, out, available


# ------------------------------------------------------ what it drives --
def label(score: float, available: float = 100.0) -> str:
    """
    Descriptive only. The number drives the margin of safety now; the label
    exists so the output still reads in the language of the criteria document
    and so the hurdle's quality credit has something to test.
    """
    if available < config.DURABILITY_MIN_COVERAGE:
        return "uncertain"
    if score >= config.DURABILITY_WIDE_MIN:
        return "wide"
    if score >= config.DURABILITY_NARROW_MIN:
        return "narrow"
    return "uncertain"


def margin_of_safety(score: float, available: float = 100.0) -> float:
    """
    Continuous margin of safety, replacing the three-bucket cliff.

    Under the old scheme a name a hair below the wide threshold paid ten more
    percentage points of discount than one a hair above it — a step change on a
    noisy measurement, at exactly the point where the measurement is least able
    to bear it. Here the discount slides from MOS_AT_ZERO_DURABILITY to
    MOS_AT_FULL_DURABILITY, and is deliberately calibrated so that a score of 50
    lands on 40% — the old "narrow" default. Nothing shifts silently.

    Thin coverage pulls the requirement back toward the uncertain end: not
    knowing much about a business is itself a reason to demand a wider discount,
    and the old scheme had no way to say so.
    """
    hi, lo = config.MOS_AT_ZERO_DURABILITY, config.MOS_AT_FULL_DURABILITY
    mos = hi - (hi - lo) * max(0.0, min(score, 100.0)) / 100.0
    if available < config.DURABILITY_MIN_COVERAGE:
        # Blend toward the widest margin in proportion to how much is missing.
        w = available / config.DURABILITY_MIN_COVERAGE if config.DURABILITY_MIN_COVERAGE else 0.0
        mos = mos * w + hi * (1 - w)
    return round(mos, 4)


def grants_quality_credit(score: float, available: float = 100.0) -> bool:
    """§7's hurdle credit, previously reserved for a hand-typed 'wide'."""
    return (available >= config.DURABILITY_MIN_COVERAGE
            and score >= config.DURABILITY_WIDE_MIN)


# ------------------------------------------------------------ benchmark --
def sector_medians(rows: list[tuple[str, float]]) -> dict[str, float]:
    """
    module -> median 10-year revenue CAGR, from every company screened.

    Deliberately built from the whole universe rather than the survivors: the
    benchmark has to include the companies losing share, or "faster than the
    sector" degenerates into "faster than other winners".

    The grouping is the screening module, which is coarse — seven buckets across
    the market. A company in a fast sub-industry inside a slow module scores a
    share gain it did not earn. Worth tightening to SIC major group once there
    is evidence it matters; not worth pretending it is precise now.
    """
    by_mod: dict[str, list[float]] = {}
    for module, cagr in rows:
        if module and cagr is not None:
            by_mod.setdefault(module, []).append(cagr)
    return {m: statistics.median(v) for m, v in by_mod.items() if len(v) >= 20}
