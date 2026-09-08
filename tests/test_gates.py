"""
Gate logic verification against synthetic companies.

These run with no network — they test that the gates behave as the criteria
document says, not that the data pipeline works. Each case is built to trip
exactly one gate, so a failure here points at one definition.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gates  # noqa: E402
import metrics as M  # noqa: E402
from secdata import AnnualSeries  # noqa: E402

YEARS = list(range(2016, 2026))


def build(name="TestCo", **overrides) -> AnnualSeries:
    """
    A high-quality operating company by default:
    ~20% ROIC, 30% gross profitability, 6% revenue growth, low leverage,
    strong cash conversion, shrinking share count.
    """
    rev = {y: 1000.0 * (1.06 ** (y - 2016)) for y in YEARS}
    f = {
        "revenue": rev,
        "cost_of_revenue": {y: rev[y] * 0.55 for y in YEARS},
        "operating_income": {y: rev[y] * 0.22 for y in YEARS},
        "pretax_income": {y: rev[y] * 0.21 for y in YEARS},
        "tax_expense": {y: rev[y] * 0.21 * 0.21 for y in YEARS},
        "net_income": {y: rev[y] * 0.166 for y in YEARS},
        "depreciation_amortisation": {y: rev[y] * 0.04 for y in YEARS},
        "operating_cash_flow": {y: rev[y] * 0.22 for y in YEARS},
        "capex": {y: rev[y] * 0.05 for y in YEARS},
        "interest_expense": {y: rev[y] * 0.005 for y in YEARS},
        "total_assets": {y: rev[y] * 1.5 for y in YEARS},
        "total_equity": {y: rev[y] * 0.75 for y in YEARS},
        "cash": {y: rev[y] * 0.10 for y in YEARS},
        "long_term_debt": {y: rev[y] * 0.25 for y in YEARS},
        "current_debt": {y: 0.0 for y in YEARS},
        "goodwill": {y: rev[y] * 0.15 for y in YEARS},
        "intangibles": {y: rev[y] * 0.05 for y in YEARS},
        "diluted_shares": {y: 100.0 * (0.98 ** (y - 2016)) for y in YEARS},
        "share_based_comp": {y: rev[y] * 0.02 for y in YEARS},
        "research_development": {y: rev[y] * 0.08 for y in YEARS},
    }
    f.update(overrides)
    return AnnualSeries(cik=1, name=name, fields=f)


def codes(result):
    return {g.code: g for g in result.gates}


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail else ''}")
    return condition


def main():
    failures = 0
    print("\n=== baseline: high-quality consumer company should clear every gate ===")
    r = gates.run_gates(build(), "TEST", "consumer")
    for g in r.gates:
        print(f"    {g.code:<10} {str(g.passed):<5} {g.reason}")
    failures += not check("overall pass", r.passed(),
                          f"failures={[g.code for g in r.failures]} "
                          f"unevaluable={[g.code for g in r.unevaluable]}")

    print("\n=== C1: weak cash conversion must fail ===")
    s = build(operating_cash_flow={y: 1000.0 * (1.06 ** (y - 2016)) * 0.10 for y in YEARS})
    g = codes(gates.run_gates(s, "T", "consumer"))["C1"]
    failures += not check("C1 fails on FCF/NI below 0.80", g.passed is False, g.reason)

    print("\n=== C2: dilution must fail ===")
    s = build(diluted_shares={y: 100.0 * (1.05 ** (y - 2016)) for y in YEARS})
    g = codes(gates.run_gates(s, "T", "consumer"))["C2"]
    failures += not check("C2 fails on rising share count", g.passed is False, g.reason)

    print("\n=== C3: a loss year must fail ===")
    ni = {y: 1000.0 * (1.06 ** (y - 2016)) * 0.166 for y in YEARS}
    ni[2020] = -50.0
    g = codes(gates.run_gates(build(net_income=ni), "T", "consumer"))["C3"]
    failures += not check("C3 fails on a loss year", g.passed is False, g.reason)

    print("\n=== C3 is excusable: one soft miss still passes overall ===")
    r = gates.run_gates(build(net_income=ni), "T", "consumer")
    failures += not check("overall still passes with one excused miss", r.passed(),
                          f"soft failures={[x.code for x in r.failures]}")

    print("\n=== C4: leverage band breach must fail ===")
    s = build(long_term_debt={y: 1000.0 * (1.06 ** (y - 2016)) * 1.2 for y in YEARS})
    g = codes(gates.run_gates(s, "T", "consumer"))["C4"]
    failures += not check("C4 fails above 2.5x net debt/EBITDA", g.passed is False, g.reason)

    print("\n=== C5: gross profitability floor is sector-specific ===")
    # Costco-like: thin gross margin, high asset turnover.
    rev = {y: 1000.0 * (1.06 ** (y - 2016)) for y in YEARS}
    thin = build(cost_of_revenue={y: rev[y] * 0.87 for y in YEARS},
                 total_assets={y: rev[y] * 0.35 for y in YEARS})
    g = codes(gates.run_gates(thin, "T", "consumer"))["C5"]
    failures += not check("thin-margin, high-turnover retailer clears C5",
                          g.passed is True, g.reason)
    g = codes(gates.run_gates(thin, "T", "software"))["C5"]
    failures += not check("same company fails the software floor",
                          g.passed is False, g.reason)

    print("\n=== C8: roll-up must fail unless returns survive goodwill ===")
    s = build(goodwill={y: 1000.0 * (1.06 ** (y - 2016)) * 0.9 for y in YEARS},
              total_assets={y: 1000.0 * (1.06 ** (y - 2016)) * 1.5 for y in YEARS},
              operating_income={y: 1000.0 * (1.06 ** (y - 2016)) * 0.05 for y in YEARS})
    g = codes(gates.run_gates(s, "T", "consumer"))["C8"]
    failures += not check("C8 fails a goodwill-heavy low-return roll-up",
                          g.passed is False, g.reason)

    print("\n=== M3: R&D capitalisation must lower reported software ROIC ===")
    s = build()
    plain = M.roic(s, 2025, capitalise_rnd=False)
    capd = M.roic(s, 2025, capitalise_rnd=True)
    failures += not check("capitalised-R&D ROIC is lower than expensed",
                          capd < plain, f"{plain:.1%} -> {capd:.1%}")

    print("\n=== M3: SBC-heavy software company must fail M-SBC ===")
    s = build(share_based_comp={y: 1000.0 * (1.06 ** (y - 2016)) * 0.18 for y in YEARS})
    g = codes(gates.run_gates(s, "T", "software"))["M-SBC"]
    failures += not check("M-SBC fails above 10% of revenue", g.passed is False, g.reason)

    print("\n=== M-GROWTH: 3% grower fails the software 8% floor, passes consumer ===")
    slow_rev = {y: 1000.0 * (1.03 ** (y - 2016)) for y in YEARS}
    s = build(revenue=slow_rev, cost_of_revenue={y: slow_rev[y] * 0.30 for y in YEARS},
              total_assets={y: slow_rev[y] * 1.0 for y in YEARS})
    failures += not check("slow grower passes consumer growth floor",
                          codes(gates.run_gates(s, "T", "consumer"))["M-GROWTH"].passed is True)
    failures += not check("slow grower fails software growth floor",
                          codes(gates.run_gates(s, "T", "software"))["M-GROWTH"].passed is False)

    print("\n=== cyclical: peak-to-peak growth beats a misleading calendar window ===")
    # Ends at a cycle trough: the calendar window understates the trend.
    cyc = {}
    for i, y in enumerate(range(2011, 2026)):
        base = 1000.0 * (1.04 ** i)
        cyc[y] = base * (0.55 if y in (2011, 2016, 2020, 2025) else 1.0)
    s = build(revenue=cyc)
    cal = M.calendar_cagr(s, "revenue")
    ptp = M.peak_to_peak_cagr(s, "revenue")
    failures += not check("peak-to-peak reads higher than the calendar window",
                          ptp is not None and cal is not None and ptp > cal,
                          f"calendar {cal:.1%} vs peak-to-peak {ptp:.1%}")

    print("\n=== valuation hurdle: quality credit and its guardrails ===")
    y_high = M.required_owner_earnings_yield(0.28, "high", "wide")
    y_low = M.required_owner_earnings_yield(0.13, "low", "wide")
    failures += not check("high-ROIC compounder gets the lowest hurdle",
                          abs(y_high - 0.040) < 1e-9, f"{y_high:.1%}")
    failures += not check("low-ROIC business gets the highest hurdle",
                          abs(y_low - 0.080) < 1e-9, f"{y_low:.1%}")
    y_narrow = M.required_owner_earnings_yield(0.28, "high", "narrow")
    failures += not check("quality credit withheld without a wide moat",
                          y_narrow > y_high, f"wide {y_high:.1%} vs narrow {y_narrow:.1%}")

    print("\n=== financials take the substitute track, not the operating gates ===")
    bank = build(name="BankCo",
                 cost_of_revenue={}, operating_cash_flow={}, capex={},
                 total_equity={y: 1000.0 * (1.06 ** (y - 2016)) * 1.2 for y in YEARS},
                 net_income={y: 1000.0 * (1.06 ** (y - 2016)) * 0.18 for y in YEARS})
    r = gates.run_gates(bank, "BANK", "financials")
    c = codes(r)
    failures += not check("C1 exempt for financials", c["C1"].passed is True, c["C1"].reason)
    failures += not check("C4 exempt for financials", c["C4"].passed is True, c["C4"].reason)
    failures += not check("C5 exempt for financials", c["C5"].passed is True, c["C5"].reason)
    failures += not check("M-ROIC uses ROE for financials",
                          "ROE" in c["M-ROIC"].reason, c["M-ROIC"].reason)

    print("\n=== unevaluable data must never count as a pass ===")
    empty = AnnualSeries(cik=2, name="NoData", fields={"revenue": {2025: 100.0}})
    r = gates.run_gates(empty, "ND", "consumer")
    failures += not check("company with no data fails overall", not r.passed(),
                          f"unevaluable={[g.code for g in r.unevaluable]}")

    print(f"\n{'='*60}")
    print("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    print(f"{'='*60}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
