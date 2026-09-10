"""
Derived metrics: the definitions the gates depend on.

Everything here is deliberately explicit — where a definition has a choice in
it (ROIC's treatment of goodwill, what counts as maintenance capex, whether
R&D is capitalised), the choice is written down rather than assumed, because
those choices decide which companies pass.
"""

from __future__ import annotations

import math
import statistics

import config
from secdata import AnnualSeries


def _get(s: AnnualSeries, field: str, fy: int) -> float | None:
    return s.series(field).get(fy)


def common_years(s: AnnualSeries, fields: list[str], n: int | None = None) -> list[int]:
    """Fiscal years for which every listed field has a value."""
    sets = [set(s.series(f)) for f in fields]
    if not sets or any(not x for x in sets):
        return []
    ys = sorted(set.intersection(*sets))
    return ys[-n:] if n else ys


# ------------------------------------------------------------- cash flow --
def free_cash_flow(s: AnnualSeries, fy: int, after_sbc: bool = False) -> float | None:
    ocf = _get(s, "operating_cash_flow", fy)
    capex = _get(s, "capex", fy)
    if ocf is None or capex is None:
        return None
    fcf = ocf - abs(capex)
    if after_sbc:
        sbc = _get(s, "share_based_comp", fy)
        if sbc is not None:
            fcf -= abs(sbc)
    return fcf


def maintenance_capex(s: AnnualSeries, fy: int) -> float | None:
    """
    Proxy: the lower of D&A or actual capex.

    Crude, and deliberately conservative — where a company discloses its own
    maintenance/growth capex split, override this with the disclosure.
    """
    da = _get(s, "depreciation_amortisation", fy)
    capex = _get(s, "capex", fy)
    if da is None and capex is None:
        return None
    if da is None:
        return abs(capex)
    if capex is None:
        return abs(da)
    return min(abs(da), abs(capex))


def owner_earnings(s: AnnualSeries, fy: int) -> float | None:
    """Net income + D&A - maintenance capex. Working capital omitted where unavailable."""
    ni = _get(s, "net_income", fy)
    da = _get(s, "depreciation_amortisation", fy)
    mcx = maintenance_capex(s, fy)
    if ni is None or da is None or mcx is None:
        return None
    return ni + da - mcx


# ------------------------------------------------------------ capital --
def invested_capital(s: AnnualSeries, fy: int, capitalised_rnd: float = 0.0) -> float | None:
    """
    Total debt + equity - cash. Goodwill stays IN the capital base — the
    harsh version, so acquisitive companies cannot hide poor returns.
    """
    eq = _get(s, "total_equity", fy)
    if eq is None:
        return None
    debt = (_get(s, "long_term_debt", fy) or 0.0) + (_get(s, "current_debt", fy) or 0.0)
    cash = (_get(s, "cash", fy) or 0.0) + (_get(s, "short_term_investments", fy) or 0.0)
    ic = eq + debt - cash + capitalised_rnd
    return ic if ic > 0 else None


def capitalised_rnd_balance(s: AnnualSeries, fy: int, years: int = 5) -> float:
    """
    M3: expensing R&D understates the capital base and inflates software ROIC.
    Straight-line amortisation over `years`, so the unamortised balance at fy
    is sum over k of R&D(fy-k) * (years-k)/years.
    """
    total = 0.0
    for k in range(years):
        rnd = _get(s, "research_development", fy - k)
        if rnd:
            total += abs(rnd) * (years - k) / years
    return total


def nopat(s: AnnualSeries, fy: int) -> float | None:
    ebit = _get(s, "operating_income", fy)
    if ebit is None:
        return None
    tax = _get(s, "tax_expense", fy)
    pretax = _get(s, "pretax_income", fy)
    rate = 0.21  # US federal statutory default
    if tax is not None and pretax and pretax > 0:
        implied = tax / pretax
        if 0.0 <= implied <= 0.50:
            rate = implied
    return ebit * (1 - rate)


def roic(s: AnnualSeries, fy: int, capitalise_rnd: bool = False) -> float | None:
    np_ = nopat(s, fy)
    if np_ is None:
        return None
    crd = capitalised_rnd_balance(s, fy) if capitalise_rnd else 0.0
    if capitalise_rnd:
        # R&D added back to NOPAT, less the year's amortisation charge
        rnd = _get(s, "research_development", fy)
        if rnd:
            amort = sum(
                abs(_get(s, "research_development", fy - k) or 0.0) / 5 for k in range(5)
            )
            np_ = np_ + abs(rnd) - amort
    ic = invested_capital(s, fy, crd)
    return np_ / ic if ic else None


def roe(s: AnnualSeries, fy: int) -> float | None:
    ni = _get(s, "net_income", fy)
    eq = _get(s, "total_equity", fy)
    return ni / eq if ni is not None and eq and eq > 0 else None


# ------------------------------------------------------------- quality --
def gross_profit(s: AnnualSeries, fy: int) -> float | None:
    rev = _get(s, "revenue", fy)
    cor = _get(s, "cost_of_revenue", fy)
    return rev - cor if rev is not None and cor is not None else None


def gross_profitability(s: AnnualSeries, fy: int) -> float | None:
    """Gross profit / total assets — travels across sectors as an absolute margin does not."""
    gp = gross_profit(s, fy)
    ta = _get(s, "total_assets", fy)
    return gp / ta if gp is not None and ta and ta > 0 else None


def ebitda(s: AnnualSeries, fy: int) -> float | None:
    ebit = _get(s, "operating_income", fy)
    da = _get(s, "depreciation_amortisation", fy)
    return ebit + abs(da) if ebit is not None and da is not None else None


def net_debt(s: AnnualSeries, fy: int) -> float | None:
    debt = (_get(s, "long_term_debt", fy) or 0.0) + (_get(s, "current_debt", fy) or 0.0)
    cash = (_get(s, "cash", fy) or 0.0) + (_get(s, "short_term_investments", fy) or 0.0)
    return debt - cash


def interest_cover(s: AnnualSeries, fy: int) -> float | None:
    """
    EBIT / interest expense, or None when interest expense is not reported.

    None means "unknown", NOT "infinite". The previous version returned
    math.inf on a missing tag, so a leverage gate was satisfied by the absence
    of the debt-cost figure — 331 companies passed C4 that way, several
    carrying real net debt. Every other gate fails closed on missing data;
    this one failed open, which is the more dangerous direction.
    """
    ebit = _get(s, "operating_income", fy)
    interest = _get(s, "interest_expense", fy)
    if ebit is None or not interest:
        return None
    return ebit / abs(interest)


# -------------------------------------------------------------- growth --
def cagr(first: float, last: float, years: int) -> float | None:
    if years <= 0 or first is None or last is None or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def calendar_cagr(s: AnnualSeries, field: str, years: int = 10) -> float | None:
    ser = s.series(field)
    if len(ser) < 3:
        return None
    ys = sorted(ser)[-(years + 1):]
    return cagr(ser[ys[0]], ser[ys[-1]], len(ys) - 1)


def peak_to_peak_cagr(s: AnnualSeries, field: str, years: int = 15) -> float | None:
    """
    Cyclicals measured peak-to-peak. Taking a calendar window that starts at a
    cycle top and ends at a trough (or the reverse) produces a growth rate that
    describes the cycle, not the business.
    """
    ser = s.series(field)
    if len(ser) < 5:
        return None
    ys = sorted(ser)[-years:]
    vals = [(y, ser[y]) for y in ys]
    peaks = [
        (y, v) for i, (y, v) in enumerate(vals)
        if (i == 0 or v >= vals[i - 1][1]) and (i == len(vals) - 1 or v >= vals[i + 1][1])
    ]
    if len(peaks) < 2:
        return calendar_cagr(s, field, years)
    return cagr(peaks[0][1], peaks[-1][1], peaks[-1][0] - peaks[0][0])


def worst_earnings_decline(s: AnnualSeries, years: int = 10) -> float | None:
    ser = s.series("net_income")
    ys = sorted(ser)[-years:]
    if len(ys) < 2:
        return None
    worst = 0.0
    for prev, cur in zip(ys, ys[1:]):
        a, b = ser[prev], ser[cur]
        if a > 0:
            worst = min(worst, (b - a) / a)
    return worst


def margin_stability(s: AnnualSeries, years: int = 10) -> float | None:
    """Standard deviation of operating margin — the predictability proxy."""
    ys = common_years(s, ["operating_income", "revenue"], years)
    margins = [
        s.series("operating_income")[y] / s.series("revenue")[y]
        for y in ys
        if s.series("revenue")[y]
    ]
    return statistics.pstdev(margins) if len(margins) >= 3 else None


# ------------------------------------------------- current trading --
def _days(a: str, b: str) -> int:
    from datetime import date
    ya, ma, da = (int(x) for x in a.split("-"))
    yb, mb, db = (int(x) for x in b.split("-"))
    return (date(yb, mb, db) - date(ya, ma, da)).days


def recent_quarters(s: AnnualSeries, field: str, want: int = 4) -> list[str] | None:
    """
    The most recent run of CONSECUTIVE three-month periods, newest last.

    Consecutive matters: the fourth quarter is never filed on a 10-Q, so a naive
    "last four entries" would splice Q3 of one year onto Q1 of the next and
    silently compare fifteen months to twelve. Each adjacent gap must be a
    quarter, or the run stops.
    """
    q = s.quarterly(field)
    if not q:
        return None
    ends = sorted(q)
    run = [ends[-1]]
    for e in reversed(ends[:-1]):
        if len(run) >= want:
            break
        if 80 <= _days(e, run[0]) <= 100:
            run.insert(0, e)
        else:
            break
    return run if len(run) >= 2 else None


def year_ago_match(s: AnnualSeries, field: str, ends: list[str]) -> list[str] | None:
    """The same periods one year earlier, or None if any is missing."""
    q = s.quarterly(field)
    out = []
    for e in ends:
        hit = [c for c in q if 345 <= _days(c, e) <= 385]
        if not hit:
            return None
        out.append(max(hit))
    return out


def current_vs_year_ago(s: AnnualSeries) -> dict | None:
    """
    Like-for-like comparison of the latest filed quarters against the same
    quarters a year earlier.

    Deliberately NOT a trailing-twelve-month figure. A TTM needs a derived
    fourth quarter, and deriving one from an annual less three quarters goes
    wrong quietly whenever a filer restates or changes its year end. Comparing
    the same two, three or four quarters year on year needs no derivation and
    is the comparison the company's own release makes.
    """
    ends = recent_quarters(s, "revenue")
    if not ends:
        return None
    prior = year_ago_match(s, "revenue", ends)
    if not prior:
        return None
    rev_now = sum(s.quarterly("revenue")[e] for e in ends)
    rev_then = sum(s.quarterly("revenue")[e] for e in prior)
    if rev_then <= 0:
        return None

    out = {
        "quarters": len(ends), "as_of": ends[-1],
        "revenue_growth": rev_now / rev_then - 1,
        "operating_income_ratio": None, "margin_now": None, "margin_then": None,
    }
    oi = s.quarterly("operating_income")
    if all(e in oi for e in ends) and all(e in oi for e in prior):
        oi_now = sum(oi[e] for e in ends)
        oi_then = sum(oi[e] for e in prior)
        out["margin_now"] = oi_now / rev_now
        out["margin_then"] = oi_then / rev_then
        if oi_then > 0:
            out["operating_income_ratio"] = oi_now / oi_then
    return out


def current_earnings_factor(s: AnnualSeries) -> float | None:
    """
    A haircut applied to normalised owner earnings when the business is
    currently earning less than it was a year ago.

    The valuation lenses run off a three-year median of owner earnings. For a
    company whose earnings have just fallen off a cliff, that median includes
    the good years while the price reflects the bad one, so the screen reports
    a bargain — lululemon showed an 18.1% owner-earnings yield in run 5 while
    guiding to a 5-7% revenue decline. Never above 1.0: a company earning MORE
    than a year ago gets no credit for it here, because one good year is not
    evidence of a new normal. Conservative in both directions, by design.
    """
    cur = current_vs_year_ago(s)
    if not cur:
        return None
    r = cur["operating_income_ratio"]
    if r is None:
        # Fall back to revenue when operating income is not tagged quarterly.
        r = 1 + cur["revenue_growth"]
    return min(1.0, max(0.0, r))


# ------------------------------------------------- capital allocation --
def capital_allocation(s: AnnualSeries, years: int = 10) -> tuple[str, float] | None:
    """
    C7, in two modes, because capital can be deployed in two directions.

    RETAINED  — invested capital grew. Test the classic incremental ROIC:
                change in NOPAT divided by the capital management kept.

    RETURNED  — invested capital shrank, which is what buybacks and special
                dividends do. The old version returned None here, and because
                unevaluable counts as a failure the gate rejected 1,625
                companies (55% of the universe) for the disciplined capital
                allocation it exists to reward. Instead, test whether shrinking
                the capital base grew value per share: owner-earnings-per-share
                CAGR over the period.

    Returns (mode, value) or None when the inputs are genuinely missing.
    """
    ys = common_years(s, ["operating_income", "total_equity"], years + 1)
    if len(ys) < 4:
        return None
    first, last = ys[0], ys[-1]
    np_first, np_last = nopat(s, first), nopat(s, last)
    ic_first, ic_last = invested_capital(s, first), invested_capital(s, last)
    if None in (np_first, np_last, ic_first, ic_last):
        return None

    delta_ic = ic_last - ic_first
    if delta_ic > 0:
        return ("retained", (np_last - np_first) / delta_ic)

    # Capital returned. Did value per share grow while the base shrank?
    shares = s.series("diluted_shares")
    oe_first, oe_last = owner_earnings(s, first), owner_earnings(s, last)
    if oe_first is None or oe_last is None or first not in shares or last not in shares:
        return None
    if shares[first] <= 0 or shares[last] <= 0 or oe_first <= 0:
        return None
    ps_first, ps_last = oe_first / shares[first], oe_last / shares[last]
    growth = cagr(ps_first, ps_last, last - first)
    return ("returned", growth) if growth is not None else None


def incremental_roic(s: AnnualSeries, years: int = 10) -> float | None:
    """Retained-capital case only. Kept for callers that want the classic figure."""
    r = capital_allocation(s, years)
    return r[1] if r and r[0] == "retained" else None


def loss_years(s: AnnualSeries, lookback: int = 20) -> tuple[int, int] | None:
    """
    (number of loss-making years, years of history) over the lookback.

    §M4 calls a clean record through 2008-09 the single most discriminating
    test available for a financial, and impossible to game. It needs only the
    net income series, which is already extracted.
    """
    ser = s.series("net_income")
    if not ser:
        return None
    ys = sorted(ser)[-lookback:]
    if len(ys) < 10:
        return None
    return sum(1 for y in ys if ser[y] < 0), len(ys)


def share_count_ratio(s: AnnualSeries, years: int = 5) -> float | None:
    ser = s.series("diluted_shares")
    if len(ser) < 2:
        return None
    ys = sorted(ser)
    latest = ys[-1]
    target = latest - years
    prior = min(ys, key=lambda y: abs(y - target))
    if prior == latest or ser[prior] <= 0:
        return None
    return ser[latest] / ser[prior]


def goodwill_intangibles_share(s: AnnualSeries, fy: int) -> float | None:
    ta = _get(s, "total_assets", fy)
    if not ta:
        return None
    gw = (_get(s, "goodwill", fy) or 0.0) + (_get(s, "intangibles", fy) or 0.0)
    return gw / ta


# ------------------------------------------------------------ valuation --
def classify_reinvestment(s: AnnualSeries) -> str:
    """High / moderate / low reinvestment runway, for the hurdle table."""
    inc = incremental_roic(s)
    ys = common_years(s, ["net_income", "total_equity"], 6)
    if len(ys) < 3:
        return "moderate"
    ic_first, ic_last = invested_capital(s, ys[0]), invested_capital(s, ys[-1])
    earnings = sum(s.series("net_income").get(y, 0.0) for y in ys[1:])
    if not earnings or ic_first is None or ic_last is None:
        return "moderate"
    redeployed = (ic_last - ic_first) / earnings
    if redeployed >= config.REINVEST_HIGH_SHARE and (inc or 0) >= config.REINVEST_HIGH_MIN_ROIC:
        return "high"
    if redeployed <= config.REINVEST_LOW_SHARE:
        return "low"
    return "moderate"


def required_owner_earnings_yield(roic_median: float | None, reinvestment: str,
                                  moat: str = "wide") -> float:
    """
    §7 Lens A. Required yield falls as quality and reinvestment runway rise —
    but the credit is withheld unless the moat is wide.
    """
    if roic_median is None:
        return config.HURDLE_DEFAULT_YIELD
    if config.QUALITY_CREDIT_REQUIRES_WIDE_MOAT and moat != "wide":
        roic_median = min(roic_median, 0.149)  # credit withheld
    for floor, band, yield_ in config.QUALITY_ADJUSTED_HURDLE:
        if roic_median >= floor and band in (reinvestment, "any"):
            return yield_
    return config.HURDLE_DEFAULT_YIELD


def enterprise_value(market_cap: float, s: AnnualSeries, fy: int) -> float | None:
    nd = net_debt(s, fy)
    return market_cap + nd if nd is not None else None
