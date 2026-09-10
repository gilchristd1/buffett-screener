"""
The gate engine.

Every gate returns a GateResult carrying its own reason string, so a rejection
can always be audited: "why did this fail?" must be answerable without rerunning
anything. That matters more than speed — a screen you cannot interrogate is a
screen you will eventually stop trusting.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

import config
import metrics as M
from secdata import AnnualSeries


@dataclass
class GateResult:
    code: str
    passed: bool | None       # None = could not evaluate (missing data)
    reason: str
    value: float | None = None

    @property
    def evaluable(self) -> bool:
        return self.passed is not None


@dataclass
class ScreenResult:
    ticker: str
    name: str
    sector: str
    gates: list[GateResult] = field(default_factory=list)

    @property
    def failures(self) -> list[GateResult]:
        return [g for g in self.gates if g.passed is False]

    @property
    def unevaluable(self) -> list[GateResult]:
        return [g for g in self.gates if g.passed is None]

    def passed(self, allow_excused: int = config.MAX_EXCUSED_MISSES) -> bool:
        """
        Excusable gates (C1, C3) may absorb `allow_excused` misses between them.
        Everything else is strict. Unevaluable gates never count as a pass.
        """
        excusable = {"C1", "C3"}
        hard = [g for g in self.failures if g.code not in excusable]
        soft = [g for g in self.failures if g.code in excusable]
        if hard or self.unevaluable:
            return False
        return len(soft) <= allow_excused


def _fmt(v, pct=True):
    if v is None:
        return "n/a"
    return f"{v:.1%}" if pct else f"{v:,.2f}"


# ------------------------------------------------------------ core gates --
def gate_C1_cash_conversion(s: AnnualSeries, sector: str) -> GateResult:
    if sector == "financials":
        return GateResult("C1", True, "exempt — financials (M4)")
    after_sbc = config.SECTOR_MODULES.get(sector, {}).get("fcf_after_sbc", False)
    ys = M.common_years(s, ["operating_cash_flow", "capex", "net_income"],
                        config.C1_LOOKBACK_YEARS)
    if len(ys) < 3:
        return GateResult("C1", None, "insufficient cash flow history")
    fcf = sum(M.free_cash_flow(s, y, after_sbc) or 0.0 for y in ys)
    ni = sum(s.series("net_income")[y] for y in ys)
    if ni <= 0:
        return GateResult("C1", False, "cumulative net income not positive")
    ratio = fcf / ni
    ok = ratio >= config.C1_MIN_FCF_TO_NI
    return GateResult("C1", ok,
                      f"FCF/NI {ratio:.2f} vs {config.C1_MIN_FCF_TO_NI:.2f} min"
                      + (" (after SBC)" if after_sbc else ""), ratio)


def gate_C2_dilution(s: AnnualSeries, sector: str) -> GateResult:
    ratio = M.share_count_ratio(s, config.C2_LOOKBACK_YEARS)
    if ratio is None:
        return GateResult("C2", None, "share count history unavailable")
    ok = ratio <= config.C2_MAX_SHARE_COUNT_RATIO
    return GateResult("C2", ok,
                      f"share count {ratio:.3f}x vs 5y ago "
                      f"(max {config.C2_MAX_SHARE_COUNT_RATIO})", ratio)


def gate_C3_earnings_durability(s: AnnualSeries, sector: str) -> GateResult:
    ni = s.series("net_income")
    ys = sorted(ni)[-config.C3_YEARS:]
    if len(ys) < config.C3_YEARS:
        return GateResult("C3", None, f"only {len(ys)}y of earnings history")
    pos_ni = sum(1 for y in ys if ni[y] > 0)
    fcf_years = [y for y in ys if M.free_cash_flow(s, y) is not None]
    pos_fcf = sum(1 for y in fcf_years if M.free_cash_flow(s, y) > 0)
    if len(fcf_years) < config.C3_YEARS:
        return GateResult("C3", None, f"only {len(fcf_years)}y of FCF history")
    ok = pos_ni >= config.C3_MIN_POSITIVE_NI_YEARS and pos_fcf >= config.C3_MIN_POSITIVE_FCF_YEARS
    return GateResult("C3", ok,
                      f"{pos_ni}/10 profitable years, {pos_fcf}/10 FCF-positive")


def gate_C4_leverage(s: AnnualSeries, sector: str) -> GateResult:
    band = config.LEVERAGE_BANDS.get(sector)
    if band is None or band[0] is None:
        return GateResult("C4", True, "capital ratios apply instead (M4)")
    max_nd, min_cover, at_trough = band
    ys = M.common_years(s, ["operating_income", "depreciation_amortisation"], 10)
    if not ys:
        return GateResult("C4", None, "EBITDA unavailable")
    fy = ys[-1]
    nd = M.net_debt(s, fy)
    ebitdas = [M.ebitda(s, y) for y in ys if M.ebitda(s, y) is not None]
    if not ebitdas or nd is None:
        return GateResult("C4", None, "leverage inputs unavailable")
    # Cyclicals are tested against the worst EBITDA in the window, not the latest
    e = min(ebitdas) if at_trough else M.ebitda(s, fy)
    if e is None or e <= 0:
        return GateResult("C4", False, "EBITDA not positive at test point")
    ratio = nd / e
    cover = M.interest_cover(s, fy)
    label = "trough" if at_trough else "latest"

    if cover is None:
        # Unknown is not "safe". A company with net debt whose interest cost we
        # cannot read has not passed a debt-survivability test — it has evaded
        # one. Only a genuinely net-cash balance sheet is excused.
        if nd > 0:
            return GateResult("C4", None,
                              f"net debt/EBITDA {ratio:.2f}x ({label}) but interest "
                              f"expense is not reported — cover unknown, not infinite",
                              ratio)
        return GateResult("C4", ratio <= max_nd,
                          f"net cash ({ratio:.2f}x {label}); no interest expense to cover",
                          ratio)

    ok = ratio <= max_nd and cover >= min_cover
    return GateResult("C4", ok,
                      f"net debt/EBITDA {ratio:.2f}x ({label}, max {max_nd}x), "
                      f"interest cover {cover:.1f}x (min {min_cover})", ratio)


def gate_C5_gross_profitability(s: AnnualSeries, sector: str) -> GateResult:
    floor = config.GROSS_PROFITABILITY_FLOORS.get(sector)
    if floor is None:
        return GateResult("C5", True, "module gates apply instead")
    ys = M.common_years(s, ["revenue", "cost_of_revenue", "total_assets"], 3)
    if not ys:
        return GateResult("C5", None, "gross profit or assets unavailable")
    vals = [M.gross_profitability(s, y) for y in ys]
    vals = [v for v in vals if v is not None]
    if not vals:
        return GateResult("C5", None, "gross profitability not computable")
    gp = statistics.median(vals)
    return GateResult("C5", gp >= floor,
                      f"gross profit/assets {gp:.1%} vs {floor:.0%} floor", gp)


def gate_C6_accounting(s: AnnualSeries, sector: str) -> GateResult:
    """
    Only the mechanical half is automatable. Restatements, auditor changes and
    material weaknesses need the filings themselves — flagged for manual check.
    """
    return GateResult("C6", True, "MANUAL: check restatements, auditor change, 8-K Item 4.02")


def gate_C7_capital_allocation(s: AnnualSeries, sector: str) -> GateResult:
    """
    Two ways to allocate capital well, so two tests.

    Retained: did the money kept earn a decent return? Returned: did shrinking
    the capital base actually grow value per share? The old single-branch
    version marked every capital-returning company unevaluable, which counted
    as a failure — rejecting buyback discipline as if it were missing data.
    """
    r = M.capital_allocation(s)
    if r is None:
        return GateResult("C7", None, "capital-allocation inputs unavailable")
    mode, value = r
    if mode == "retained":
        ok = value >= config.C7_MIN_INCREMENTAL_ROIC
        return GateResult("C7", ok,
                          f"incremental ROIC on retained capital {value:.1%} "
                          f"vs {config.C7_MIN_INCREMENTAL_ROIC:.0%} min", value)
    floor = config.C7_MIN_PER_SHARE_GROWTH_IF_RETURNING
    ok = value >= floor
    return GateResult("C7", ok,
                      f"capital returned, not retained; owner earnings/share CAGR "
                      f"{value:.1%} vs {floor:.0%} min", value)


def gate_C8_rollup(s: AnnualSeries, sector: str) -> GateResult:
    ys = s.series("total_assets")
    if not ys:
        return GateResult("C8", None, "total assets unavailable")
    fy = max(ys)
    share = M.goodwill_intangibles_share(s, fy)
    if share is None:
        return GateResult("C8", None, "goodwill/intangibles unavailable")
    if share <= config.C8_MAX_GOODWILL_INTANGIBLES_SHARE:
        return GateResult("C8", True, f"goodwill+intangibles {share:.1%} of assets", share)
    # Escape hatch: acquisitive is fine if returns survive including goodwill
    mod = config.SECTOR_MODULES.get(sector, {})
    r = M.roic(s, fy, mod.get("capitalise_rnd", False))
    ok = r is not None and r >= mod.get("roic_median_min", 0.15)
    return GateResult("C8", ok,
                      f"goodwill+intangibles {share:.1%} of assets; "
                      f"ROIC incl. goodwill {_fmt(r)}", share)


# --------------------------------------------------------- sector module --
def gate_C9_current_trading(s: AnnualSeries, sector: str) -> GateResult:
    """
    C9: is the business still earning what its ten-year record says it earns?

    The gap run 5 exposed. Every other gate in this file reads annual filings,
    so the screen's most recent view of a company was up to fifteen months old.
    lululemon reached the top of the queue with a 98/100 durability score and
    an 18.1% owner-earnings yield while comparable sales were falling 9%,
    margins were collapsing and guidance had been cut twice.

    This is not a forecast. It compares the latest filed quarters against the
    same quarters a year earlier — the comparison the company's own results
    release makes — and refuses to carry a broken business forward on the
    strength of its history. A company that fails here is not judged a bad
    business; it is judged one whose future has stopped being predictable,
    which is the honest reason for a quality screen to step back rather than
    price it.
    """
    if not config.C9_ENABLED:
        return GateResult("C9", True, "current-trading test disabled")
    cur = M.current_vs_year_ago(s)
    if cur is None:
        if config.C9_UNVERIFIED_BLOCKS:
            return GateResult("C9", None, "no comparable quarterly filings")
        return GateResult("C9", True,
                          "NOT VERIFIED — no comparable quarterly filings; this company "
                          "is still being judged on annual data alone")

    g = cur["revenue_growth"]
    r = cur["operating_income_ratio"]
    n, asof = cur["quarters"], cur["as_of"]
    bits = [f"{n}Q to {asof}: revenue {g:+.1%} YoY"]
    if r is not None:
        bits.append(f"operating income {r - 1:+.1%} YoY")
    if cur["margin_now"] is not None:
        bits.append(f"margin {cur['margin_now']:.1%} vs {cur['margin_then']:.1%}")
    note = "; ".join(bits)

    if g < -config.C9_MAX_REVENUE_DECLINE:
        return GateResult("C9", False, f"{note} — revenue break", g)
    if r is not None and r < config.C9_MIN_OPERATING_INCOME_RATIO:
        return GateResult("C9", False, f"{note} — earnings break", r)
    return GateResult("C9", True, note, g)


def gate_module_roic(s: AnnualSeries, sector: str) -> GateResult:
    mod = config.SECTOR_MODULES.get(sector)
    if not mod:
        return GateResult("M-ROIC", None, f"no module for sector '{sector}'")

    if sector == "financials":
        ys = sorted(s.series("net_income"))[-10:]
        roes = [M.roe(s, y) for y in ys]
        roes = [r for r in roes if r is not None]
        if len(roes) < 5:
            return GateResult("M-ROIC", None, "ROE history insufficient")
        med = statistics.median(roes)
        return GateResult("M-ROIC", med >= mod["roe_median_min"],
                          f"median ROE {med:.1%} vs {mod['roe_median_min']:.0%} min", med)

    if sector == "fee_business":
        # ROE, not ROIC. A fee business often holds more cash than equity, and
        # invested_capital() correctly refuses to return a negative capital
        # base — which would have made the gate unevaluable for exactly the
        # net-cash balance sheets M-FEE-BS is designed to reward.
        ys = sorted(s.series("net_income"))[-10:]
        roes = [r for r in (M.roe(s, y) for y in ys) if r is not None]
        if len(roes) < 5:
            return GateResult("M-ROIC", None, "ROE history insufficient")
        med = statistics.median(roes)
        floor = mod["roe_median_min"]
        return GateResult("M-ROIC", med >= floor,
                          f"median ROE {med:.1%} vs {floor:.0%} min "
                          f"(fee business — ROE, not ROIC)", med)

    if sector == "reit":
        return _reit_gate(s, mod)
    if sector == "utilities":
        return _utility_gate(s, mod)

    cap_rnd = mod.get("capitalise_rnd", False)
    ys = M.common_years(s, ["operating_income", "total_equity"], 10)
    roics = [(y, M.roic(s, y, cap_rnd)) for y in ys]
    roics = [(y, r) for y, r in roics if r is not None]
    if len(roics) < 5:
        return GateResult("M-ROIC", None, "ROIC history insufficient")

    med = statistics.median([r for _, r in roics])
    note = " (R&D capitalised)" if cap_rnd else ""

    if sector == "energy":
        avg = statistics.mean([r for _, r in roics])
        trough = min(r for _, r in roics)
        ok = avg >= mod["roic_cycle_avg_min"] and trough >= mod["roic_trough_min"]
        return GateResult("M-ROIC", ok,
                          f"cycle-avg ROIC {avg:.1%} (min {mod['roic_cycle_avg_min']:.0%}), "
                          f"trough {trough:.1%} (min {mod['roic_trough_min']:.0%})", avg)

    lookback = mod.get("roic_every_year_lookback", 5)
    recent = [r for _, r in roics[-lookback:]]
    every_min = mod.get("roic_every_year_min", 0.0)
    ok = med >= mod["roic_median_min"] and all(r >= every_min for r in recent)
    return GateResult("M-ROIC", ok,
                      f"median ROIC {med:.1%} vs {mod['roic_median_min']:.0%}{note}; "
                      f"min of last {lookback}y {min(recent):.1%} vs {every_min:.0%}", med)


def gate_module_growth(s: AnnualSeries, sector: str) -> GateResult:
    mod = config.SECTOR_MODULES.get(sector, {})
    floor = mod.get("revenue_cagr_min")
    if floor is None:
        return GateResult("M-GROWTH", True, "no revenue growth floor for this sector")
    if mod.get("growth_measure") == "peak_to_peak":
        g = M.peak_to_peak_cagr(s, "revenue")
        how = "peak-to-peak"
    else:
        g = M.calendar_cagr(s, "revenue")
        how = "10y"
    if g is None:
        return GateResult("M-GROWTH", None, "revenue history insufficient")
    return GateResult("M-GROWTH", g >= floor,
                      f"revenue CAGR {g:.1%} ({how}) vs {floor:.0%} min", g)


def gate_module_sbc(s: AnnualSeries, sector: str) -> GateResult:
    """M3: SBC is a real cost, paid in shareholder ownership."""
    mod = config.SECTOR_MODULES.get(sector, {})
    cap = mod.get("max_sbc_to_revenue")
    if cap is None:
        return GateResult("M-SBC", True, "not applicable to this sector")
    ys = M.common_years(s, ["share_based_comp", "revenue"], 3)
    if not ys:
        return GateResult("M-SBC", None, "SBC not disclosed")
    ratios = [abs(s.series("share_based_comp")[y]) / s.series("revenue")[y]
              for y in ys if s.series("revenue")[y]]
    if not ratios:
        return GateResult("M-SBC", None, "SBC ratio not computable")
    r = statistics.median(ratios)
    return GateResult("M-SBC", r <= cap, f"SBC/revenue {r:.1%} vs {cap:.0%} max", r)


def _reit_gate(s: AnnualSeries, mod: dict) -> GateResult:
    """
    REITs on XBRL-computable proxies. AFFO and same-store NOI are not GAAP
    tags, so §M6's real tests stay manual — but a company being untestable on
    two measures is no reason to reject it by throwing an exception, which is
    what happened to all 357 REITs in the first run.
    """
    ys = M.common_years(s, ["net_income", "depreciation_amortisation",
                            "diluted_shares", "total_assets"], 10)
    if len(ys) < 5:
        return GateResult("M-ROIC", None, "REIT: FFO history insufficient")
    ffo_ps = {y: (s.series("net_income")[y] + abs(s.series("depreciation_amortisation")[y]))
                 / s.series("diluted_shares")[y]
              for y in ys if s.series("diluted_shares")[y] > 0}
    if len(ffo_ps) < 5:
        return GateResult("M-ROIC", None, "REIT: share history insufficient")
    first, last = min(ffo_ps), max(ffo_ps)
    growth = M.cagr(ffo_ps[first], ffo_ps[last], last - first)

    fy = max(ys)
    ta = s.series("total_assets")[fy]
    debt = (s.series("long_term_debt").get(fy, 0.0) or 0.0) + \
           (s.series("current_debt").get(fy, 0.0) or 0.0)
    ltv = debt / ta if ta else None
    cover = M.interest_cover(s, fy)

    checks, ok = [], True
    if growth is None:
        return GateResult("M-ROIC", None, "REIT: FFO/share growth not computable")
    checks.append(f"FFO/share CAGR {growth:.1%} (min {mod['ffo_per_share_cagr_min']:.0%})")
    ok &= growth >= mod["ffo_per_share_cagr_min"]
    if ltv is None:
        return GateResult("M-ROIC", None, "REIT: leverage not computable")
    checks.append(f"debt/assets {ltv:.0%} (max {mod['max_debt_to_assets']:.0%} book)")
    ok &= ltv <= mod["max_debt_to_assets"]
    if cover is None:
        return GateResult("M-ROIC", None, "REIT: interest expense not reported")
    checks.append(f"fixed-charge cover {cover:.1f}x (min {mod['min_fixed_charge_cover']})")
    ok &= cover >= mod["min_fixed_charge_cover"]
    return GateResult("M-ROIC", ok, "REIT: " + "; ".join(checks)
                      + " | MANUAL: AFFO, same-store NOI, WALE", growth)


def _utility_gate(s: AnnualSeries, mod: dict) -> GateResult:
    """
    Utilities on ROIC and ROE floors. The real §M6 moat test — achieved return
    versus allowed return — is not in XBRL and must be checked by hand.
    """
    ys = M.common_years(s, ["operating_income", "total_equity"], 10)
    roics = [r for r in (M.roic(s, y) for y in ys) if r is not None]
    roes = [r for r in (M.roe(s, y) for y in ys) if r is not None]
    if len(roics) < 5 or len(roes) < 5:
        return GateResult("M-ROIC", None, "utility: ROIC/ROE history insufficient")
    mr, me = statistics.median(roics), statistics.median(roes)
    ok = mr >= mod["roic_median_min"] and me >= mod["roe_median_min"]
    return GateResult("M-ROIC", ok,
                      f"utility: median ROIC {mr:.1%} (min {mod['roic_median_min']:.0%}), "
                      f"median ROE {me:.1%} (min {mod['roe_median_min']:.0%}) "
                      f"| MANUAL: achieved vs allowed ROE, rate-base growth", mr)


def gate_M_fee_margin(s: AnnualSeries, sector: str) -> GateResult:
    """
    M-FEE-MARGIN: a fee business earns its keep on operating margin.

    It replaces the gross-profitability gate, which cannot be computed for a
    company that reports no cost of revenue. Tested on the MEDIAN of ten years,
    not the latest, because fee revenue is levered to markets and the latest
    year flatters in a bull market.
    """
    mod = config.SECTOR_MODULES.get(sector, {})
    floor = mod.get("operating_margin_min")
    if floor is None:
        return GateResult("M-FEE-MARGIN", True, "not applicable to this sector")
    ys = M.common_years(s, ["operating_income", "revenue"], 10)
    margins = [s.series("operating_income")[y] / s.series("revenue")[y]
               for y in ys if s.series("revenue")[y]]
    if len(margins) < 5:
        return GateResult("M-FEE-MARGIN", None, "operating margin history insufficient")
    med = statistics.median(margins)
    return GateResult("M-FEE-MARGIN", med >= floor,
                      f"median operating margin {med:.1%} vs {floor:.0%} min", med)


def gate_M_fee_balance_sheet(s: AnnualSeries, sector: str) -> GateResult:
    """
    M-FEE-BS: a fee business should not be carrying debt.

    There is no asset base to finance, so leverage here is either an acquisition
    hangover or a shareholder-return policy borrowing against a cyclical revenue
    line. Measured against REVENUE rather than EBITDA — EBITDA for an asset
    manager swings with markets, and dividing by it in a bad year manufactures a
    leverage crisis that is really a revenue dip.
    """
    mod = config.SECTOR_MODULES.get(sector, {})
    cap = mod.get("max_net_debt_to_revenue")
    if cap is None:
        return GateResult("M-FEE-BS", True, "not applicable to this sector")

    # A fee business that has never seen revenue fall has demonstrated it can
    # carry debt; one whose revenue swings with markets has not. Behavioural,
    # so it does not depend on guessing the sub-industry from a SIC code.
    stable_cap = mod.get("max_net_debt_to_revenue_if_stable")
    worst = None
    if stable_cap:
        rev = s.series("revenue")
        ys = sorted(rev)[-11:]
        drops = [(rev[b] - rev[a]) / rev[a] for a, b in zip(ys, ys[1:]) if rev[a] > 0]
        if len(drops) >= 5:
            worst = min(drops)
            if worst >= -mod.get("revenue_stability_max_decline", 0.05):
                cap = stable_cap
    ys = M.common_years(s, ["revenue", "total_equity"], 3)
    if not ys:
        return GateResult("M-FEE-BS", None, "revenue history unavailable")
    fy = ys[-1]
    nd, rev = M.net_debt(s, fy), s.series("revenue").get(fy)
    if nd is None or not rev:
        return GateResult("M-FEE-BS", None, "net debt or revenue unavailable")
    ratio = nd / rev
    if ratio <= 0:
        return GateResult("M-FEE-BS", True, f"net cash ({ratio:.2f}x revenue)", ratio)
    why = ""
    if worst is not None:
        why = (f" (revenue never fell more than {abs(worst):.1%} in 10y — stable cap)"
               if cap == mod.get("max_net_debt_to_revenue_if_stable")
               else f" (worst annual revenue fall {worst:.1%} — market-linked cap)")
    return GateResult("M-FEE-BS", ratio <= cap,
                      f"net debt {ratio:.2f}x revenue vs {cap:.2f}x max{why}", ratio)


def gate_M4_loss_history(s: AnnualSeries, sector: str) -> GateResult:
    """
    §M4's most discriminating financial test: no annual loss in 20 years,
    2008-09 included. Cheap to run and impossible to game.
    """
    # Fee businesses take this test too. An asset manager that lost money in
    # 2008-09 was carrying risk its fee model was not supposed to carry.
    if sector not in ("financials", "fee_business"):
        return GateResult("M-LOSS", True, "not applicable to this sector")
    r = M.loss_years(s)
    if r is None:
        return GateResult("M-LOSS", None, "earnings history insufficient")
    losses, span = r
    cap = config.SECTOR_MODULES[sector]["max_loss_years_in_20"]
    return GateResult("M-LOSS", losses <= cap,
                      f"{losses} loss-making year(s) in {span}y of history (max {cap})",
                      float(losses))


CORE_GATES = [
    gate_C1_cash_conversion, gate_C2_dilution, gate_C3_earnings_durability,
    gate_C4_leverage, gate_C5_gross_profitability, gate_C6_accounting,
    gate_C7_capital_allocation, gate_C8_rollup, gate_C9_current_trading,
]
MODULE_GATES = [gate_module_roic, gate_module_growth, gate_module_sbc,
                gate_M4_loss_history, gate_M_fee_margin, gate_M_fee_balance_sheet]


def run_gates(s: AnnualSeries, ticker: str, sector: str) -> ScreenResult:
    res = ScreenResult(ticker=ticker, name=s.name, sector=sector)
    for fn in CORE_GATES + MODULE_GATES:
        try:
            res.gates.append(fn(s, sector))
        except (KeyError, AttributeError, TypeError, NameError) as exc:
            # A code defect, not a data gap. Recording these as "unevaluable"
            # is how a missing config key silently rejected every REIT and
            # utility in the first run while the summary reported success.
            raise RuntimeError(
                f"{fn.__name__} is broken for sector '{sector}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except Exception as exc:      # genuinely bad data must not kill the run
            res.gates.append(GateResult(fn.__name__, None, f"data error: {exc}"))
    return res
