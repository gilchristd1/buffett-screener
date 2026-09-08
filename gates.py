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
    ok = ratio <= max_nd and (cover is None or cover >= min_cover)
    label = "trough" if at_trough else "latest"
    return GateResult("C4", ok,
                      f"net debt/EBITDA {ratio:.2f}x ({label}, max {max_nd}x), "
                      f"interest cover {_fmt(cover, False)} (min {min_cover})", ratio)


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
    inc = M.incremental_roic(s)
    if inc is None:
        return GateResult("C7", None, "capital not retained, or inputs unavailable")
    ok = inc >= config.C7_MIN_INCREMENTAL_ROIC
    return GateResult("C7", ok,
                      f"incremental ROIC {inc:.1%} vs {config.C7_MIN_INCREMENTAL_ROIC:.0%} min", inc)


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


CORE_GATES = [
    gate_C1_cash_conversion, gate_C2_dilution, gate_C3_earnings_durability,
    gate_C4_leverage, gate_C5_gross_profitability, gate_C6_accounting,
    gate_C7_capital_allocation, gate_C8_rollup,
]
MODULE_GATES = [gate_module_roic, gate_module_growth, gate_module_sbc]


def run_gates(s: AnnualSeries, ticker: str, sector: str) -> ScreenResult:
    res = ScreenResult(ticker=ticker, name=s.name, sector=sector)
    for fn in CORE_GATES + MODULE_GATES:
        try:
            res.gates.append(fn(s, sector))
        except Exception as exc:  # a bad filing must not kill the run
            res.gates.append(GateResult(fn.__name__, None, f"error: {exc}"))
    return res
