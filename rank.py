"""
Tier 4 (score) and Tier 5 (valuation) — turning a pass/fail list into a decision.

Both tiers were specified in screening-criteria.md from v0.1 and neither existed
in code until now, which is why runs 1 and 2 produced 54 and 41 survivors with
no ordering and no buy prices.

An honest note on the score. §6 allocates 100 points across five categories, but
30 of those points rest on things XBRL simply does not carry — market share,
recurring-revenue mix, customer concentration, insider ownership, acquisition
track record, debt maturity profile. Rather than quietly award or withhold them,
each component reports the points it earned AND the points it was eligible for,
and the score is normalised over what could actually be measured. Every company
is scored on the same 70 available points, and the 30 manual points are listed
against each name so you know what the machine has not looked at.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

import config
import metrics as M
import secdata

# Components that need a human. Listed against every score so the gap is visible.
MANUAL_COMPONENTS = {
    "market position / switching costs": 6,
    "acquisition track record": 3,
    "insider ownership and comp alignment": 3,
    "debt maturity profile": 4,
    "off-balance-sheet obligations": 5,
    "recurring or contracted revenue share": 5,
    "customer and geographic concentration": 4,
}
MANUAL_POINTS = sum(MANUAL_COMPONENTS.values())      # 30


def _band(value, thresholds, points):
    """Award `points[i]` for the first threshold the value clears."""
    if value is None:
        return None
    for t, p in zip(thresholds, points):
        if value >= t:
            return p
    return 0.0


# ------------------------------------------------------------ components --
def score_components(s: secdata.AnnualSeries, sector: str,
                     oe_yield: float | None = None,
                     discount_to_iv: float | None = None,
                     multiple_vs_history: float | None = None) -> list[tuple]:
    """
    Returns [(name, earned, available, note), ...].

    `available` is 0 where the input is missing, so a company is never punished
    in the normalised score for a tag gap — it is scored on what is knowable
    about it, and the coverage is reported alongside.
    """
    out = []
    mod = config.SECTOR_MODULES.get(sector, {})
    cap_rnd = mod.get("capitalise_rnd", False)

    # --- business quality and moat (24 of 30 measurable) ---
    ys = M.common_years(s, ["operating_income", "total_equity"], 10)
    roics = [r for r in (M.roic(s, y, cap_rnd) for y in ys) if r is not None]
    if roics:
        med = statistics.median(roics)
        spread = med - config.REQUIRED_RETURN
        pts = _band(spread, [0.20, 0.15, 0.10, 0.05, 0.0], [10, 8, 6, 4, 2])
        out.append(("ROIC spread over required return", pts, 10,
                    f"median ROIC {med:.1%}, {spread:+.1%} vs {config.REQUIRED_RETURN:.0%}"))
    else:
        out.append(("ROIC spread over required return", 0, 0, "no ROIC history"))

    cv = M.margin_stability(s)
    if cv is not None:
        pts = _band(-cv, [-0.02, -0.04, -0.06, -0.10], [8, 6, 4, 2])
        out.append(("Operating margin stability", pts, 8, f"std dev {cv:.1%}"))
    else:
        out.append(("Operating margin stability", 0, 0, "margin history unavailable"))

    worst = M.worst_earnings_decline(s)
    if worst is not None:
        pts = _band(-worst, [-0.05, -0.15, -0.30, -0.50], [6, 4.5, 3, 1.5])
        out.append(("Resilience through the worst year", pts, 6,
                    f"worst annual earnings decline {worst:.0%}"))
    else:
        out.append(("Resilience through the worst year", 0, 0, "earnings history unavailable"))

    # --- capital allocation (14 of 20 measurable) ---
    ca = M.capital_allocation(s)
    if ca:
        mode, value = ca
        if mode == "retained":
            pts = _band(value, [0.25, 0.18, 0.13, 0.10], [8, 6.5, 5, 3])
            note = f"incremental ROIC {value:.1%} on retained capital"
        else:
            pts = _band(value, [0.15, 0.10, 0.07, 0.05], [8, 6.5, 5, 3])
            note = f"owner earnings/share CAGR {value:.1%} while returning capital"
        out.append(("Return on capital deployed", pts, 8, note))
    else:
        out.append(("Return on capital deployed", 0, 0, "not computable"))

    ratio = M.share_count_ratio(s)
    if ratio is not None:
        pts = _band(-ratio, [-0.90, -0.95, -1.00, -1.02], [6, 4.5, 3, 1.5])
        out.append(("Share count discipline", pts, 6, f"{ratio:.3f}x vs 5 years ago"))
    else:
        out.append(("Share count discipline", 0, 0, "share history unavailable"))

    # --- financial strength (6 of 15 measurable) ---
    band = config.LEVERAGE_BANDS.get(sector)
    if band and band[0] and ys:
        fy = ys[-1]
        e, nd = M.ebitda(s, fy), M.net_debt(s, fy)
        if e and e > 0 and nd is not None:
            lev = nd / e
            head = band[0] - lev
            pts = _band(head, [band[0], band[0] * 0.6, band[0] * 0.3, 0.0], [6, 4.5, 3, 1.5])
            out.append(("Leverage headroom", pts, 6,
                        f"net debt/EBITDA {lev:.2f}x vs {band[0]}x limit"))
        else:
            out.append(("Leverage headroom", 0, 0, "leverage not computable"))
    else:
        out.append(("Leverage headroom", 0, 0, "sector uses capital ratios"))

    # --- predictability (6 of 15 measurable) ---
    if cv is not None:
        pts = _band(-cv, [-0.015, -0.03, -0.05, -0.08], [6, 4.5, 3, 1.5])
        out.append(("Earnings predictability", pts, 6, f"margin CoV proxy {cv:.1%}"))
    else:
        out.append(("Earnings predictability", 0, 0, "not computable"))

    # --- valuation (20 of 20 measurable, once a price exists) ---
    if oe_yield is not None:
        pts = _band(oe_yield, [0.08, 0.065, 0.05, 0.04], [10, 8, 5, 2])
        out.append(("Owner-earnings yield", pts, 10, f"{oe_yield:.1%} of enterprise value"))
    else:
        out.append(("Owner-earnings yield", 0, 0, "no price"))

    if discount_to_iv is not None:
        pts = _band(discount_to_iv, [0.40, 0.30, 0.20, 0.0], [6, 4.5, 3, 1])
        out.append(("Discount to intrinsic value", pts, 6, f"{discount_to_iv:.0%} below DCF"))
    else:
        out.append(("Discount to intrinsic value", 0, 0, "no DCF"))

    if multiple_vs_history is not None:
        pts = _band(-multiple_vs_history, [-0.80, -1.00, -1.20, -1.50], [4, 3, 2, 1])
        out.append(("Multiple vs own 10-year history", pts, 4,
                    f"{multiple_vs_history:+.2f} std dev from median EV/EBIT"))
    else:
        out.append(("Multiple vs own 10-year history", 0, 0, "no history"))

    return out


MAX_MEASURABLE_POINTS = 70          # the 100 in §6 less the 30 that need a human
VALUATION_POINTS = 20               # yield 10 + DCF discount 6 + multiple 4


def total_score(components: list[tuple]) -> tuple[float, float, float]:
    """(normalised score out of 100, points earned, points available)."""
    earned = sum(c[1] for c in components)
    available = sum(c[2] for c in components)
    return (100.0 * earned / available if available else 0.0), earned, available


VALUATION_COMPONENTS = {"Owner-earnings yield", "Discount to intrinsic value",
                        "Multiple vs own 10-year history"}


def valuation_coverage(components: list[tuple]) -> float:
    """Valuation points that could actually be scored, out of VALUATION_POINTS."""
    return sum(c[2] for c in components if c[0] in VALUATION_COMPONENTS)


def is_price_comparable(components: list[tuple]) -> bool:
    """
    True when the company was actually judged on price.

    Normalising over available points is right for tag gaps, but it quietly
    makes an unpriced company look comparable to a priced one: in run 3 Alphabet
    ranked 8th on 40/50 points with all twenty valuation points simply absent.
    A score that omits price is not the same measurement.

    The test is the OWNER-EARNINGS YIELD, not all twenty points. Requiring the
    full twenty was too strict once Lens C started returning "unavailable"
    honestly: in run 5 it demoted TJX — which has a market cap and a yield and
    was missing only the 4-point multiple-vs-history component — into the same
    bucket as three companies with no price at all. Yield is the lens §7 calls
    the gate; the rest is reported as coverage.
    """
    return any(c[0] == "Owner-earnings yield" and c[2] > 0 for c in components)


# ------------------------------------------------------------ valuation --
def normalised_owner_earnings(s: secdata.AnnualSeries,
                              guided_factor: float | None = None) -> tuple[float, float] | None:
    """
    (owner earnings used for valuation, haircut factor applied).

    The three-year median, cut back to current earning power when the latest
    filed quarters show the business earning less than a year ago. Without the
    haircut the median carries the good years while the price reflects the bad
    one, and the screen calls a broken business cheap.
    """
    ys = M.common_years(s, ["net_income", "depreciation_amortisation", "capex"], 3)
    if not ys:
        return None
    oes = [o for o in (M.owner_earnings(s, y) for y in ys) if o is not None]
    if not oes:
        return None
    factor = M.current_earnings_factor(s)
    factor = 1.0 if factor is None else factor
    # Management's own guided change, where the overlay carries one. The LOWER
    # of the two applies: value a company on the worse of what it is currently
    # earning and what its own management says it will earn.
    if guided_factor is not None:
        factor = min(factor, guided_factor)
    return statistics.median(oes) * factor, factor


def owner_earnings_yield(s: secdata.AnnualSeries, market_cap: float,
                         guided_factor: float | None = None) -> float | None:
    """Lens A: current-adjusted owner earnings over enterprise value."""
    ys = M.common_years(s, ["net_income", "depreciation_amortisation", "capex"], 3)
    if not ys or not market_cap:
        return None
    got = normalised_owner_earnings(s, guided_factor)
    if not got:
        return None
    oe, _ = got
    ev = M.enterprise_value(market_cap, s, ys[-1])
    return oe / ev if ev and ev > 0 else None


def dcf_intrinsic_value(s: secdata.AnnualSeries,
                        guided_factor: float | None = None) -> float | None:
    """
    Lens B: two-stage DCF on owner earnings, with growth FADING to terminal.

    The first version held the starting growth rate flat for ten years. With
    growth capped at 10% and the discount rate also 10%, stage one contributed
    roughly ten times base earnings undiscounted, and the model ended up paying
    about 24x owner earnings for anything that had grown quickly — extrapolating
    a decade of high growth from a decade of history, which is precisely the
    error §7 Lens C exists to catch ("the forecast, not the price, is doing the
    work").

    Growth now declines linearly from its starting rate to terminal growth over
    the ten years, which is how competition actually erodes returns.
    """
    ys = M.common_years(s, ["net_income", "depreciation_amortisation", "capex"], 11)
    if len(ys) < 5:
        return None
    oes = {y: M.owner_earnings(s, y) for y in ys}
    oes = {y: v for y, v in oes.items() if v is not None and v > 0}
    if len(oes) < 5:
        return None
    base = statistics.median(list(oes.values())[-3:])
    # Same haircut as Lens A. Discounting a decade of cash flows off a base the
    # company has already stopped earning is the single largest way this model
    # can be wrong, and it is wrong in the direction that manufactures bargains.
    factor = M.current_earnings_factor(s)
    if factor is None:
        factor = 1.0
    if guided_factor is not None:
        factor = min(factor, guided_factor)
    base *= factor
    first, last = min(oes), max(oes)
    hist = M.cagr(oes[first], oes[last], last - first) or 0.0
    g = max(0.0, min(hist, 0.10))
    r, tg = config.REQUIRED_RETURN, config.TERMINAL_GROWTH_MAX

    n = 10
    pv, cash = 0.0, base
    for yr in range(1, n + 1):
        # Linear fade from the starting rate to terminal growth by year n.
        g_yr = g + (tg - g) * (yr - 1) / (n - 1)
        cash *= (1 + g_yr)
        pv += cash / ((1 + r) ** yr)
    terminal = cash * (1 + tg) / (r - tg)
    pv += terminal / ((1 + r) ** n)
    return pv


def multiple_vs_history(s: secdata.AnnualSeries, market_cap: float,
                        year_end_closes: dict[int, float] | None = None) -> float | None:
    """
    Lens C: today's EV/EBIT in standard deviations from its own 10-year median.

    THE FIRST VERSION WAS STRUCTURALLY BROKEN, and run 4 is the proof. It held
    enterprise value constant at today's level and varied only EBIT, so for any
    company with growing EBIT the multiple series was monotonically decreasing
    and the latest year was its minimum BY CONSTRUCTION. Run 4: 34 of 37 priced
    survivors scored negative, median z-score -0.79. A measure that tells you
    almost every company is cheap against its own history is not measuring
    anything, and it was quietly awarding up to 4 score points to everyone while
    its veto could essentially never fire.

    The fix needs a real historical enterprise value, which means a historical
    price. Share counts and net debt are already in the XBRL record, so:

        EV(y) = close(y) x diluted shares(y) + net debt(y)

    Without prices this returns None — Lens C becomes unavailable and normalises
    out of the score. It must never fall back to the old version: a number that
    is wrong in a known direction is worse than no number.
    """
    ys = M.common_years(s, ["operating_income"], 10)
    if len(ys) < 5 or not market_cap:
        return None
    if not year_end_closes:
        return None

    ebit = s.series("operating_income")
    shares = s.series("diluted_shares")
    mult: list[tuple[int, float]] = []
    for y in ys:
        close = year_end_closes.get(y)
        if close is None or y not in shares or ebit.get(y, 0) <= 0 or shares[y] <= 0:
            continue
        nd = M.net_debt(s, y)
        if nd is None:
            continue
        ev_y = close * shares[y] + nd
        if ev_y > 0:
            mult.append((y, ev_y / ebit[y]))
    if len(mult) < 5:
        return None

    ev_now = M.enterprise_value(market_cap, s, ys[-1])
    if not ev_now or ev_now <= 0 or ebit.get(ys[-1], 0) <= 0:
        return None
    now = ev_now / ebit[ys[-1]]

    vals = [m for _, m in mult]
    med, sd = statistics.median(vals), statistics.pstdev(vals)
    if sd <= 0:
        # A history with no variation cannot produce a z-score. The old code
        # returned 0.0 here, which reads as "normally priced" and would award
        # the score points to a company trading at twice its historical
        # multiple. Unavailable is the honest answer.
        return None
    return (now - med) / sd


def buy_price(intrinsic: float | None, shares: float, moat: str,
              mos: float | None = None) -> float | None:
    """
    Intrinsic value per share, less the margin of safety §7 requires.

    `mos` is the computed, continuous margin from moat.py and takes precedence.
    The `moat` label remains the fallback for the manual override path and for
    callers that have a label but no score.
    """
    if not intrinsic or not shares:
        return None
    if mos is None:
        mos = config.MARGIN_OF_SAFETY.get(moat, config.MARGIN_OF_SAFETY["uncertain"])
    return (intrinsic / shares) * (1 - mos)


def load_moat_classifications(path: Path) -> dict[str, str]:
    """
    ticker -> wide | narrow | uncertain, read from a file you maintain by hand.

    This is now an OVERRIDE, not the source. moat.py computes durability from
    the financial record for every company; a ticker listed here overrides that
    computation with your own judgement, and an absent file means every company
    is scored rather than every company defaulting to "narrow".

    Keep it short. A row here should record a disagreement you can articulate —
    a moat the numbers cannot see yet, or one you believe is already broken.
    """
    if not path.exists():
        return {}
    with open(path) as fh:
        return {r["ticker"].upper(): r["moat"].strip().lower()
                for r in csv.DictReader(fh) if r.get("ticker")}
