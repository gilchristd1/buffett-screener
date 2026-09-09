"""
End-to-end pipeline test against fixtures shaped like REAL SEC bulk data.

This file exists because the previous fixture lied. It invented a `sic` field
inside companyfacts.json, so the pipeline passed here and then excluded all
10,407 companies against live data — SEC puts SIC in submissions.zip, not
companyfacts.zip. A fixture that encodes your assumptions tests nothing.

Two properties of the real format are reproduced deliberately:

  1. companyfacts.json is ONLY {cik, entityName, facts}. No sic, no exchange,
     no ticker. Anything else must come from submissions.zip.

  2. Each 10-K reports its own year PLUS prior-year comparatives, and every one
     of those entries carries the FILING's fy/fp. A 2025 10-K emits FY2025,
     FY2024 and FY2023 rows all tagged fy=2025, fp=FY. Code that keys on `fy`
     silently collapses three years into one.
"""

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import secdata  # noqa: E402
import universe as U  # noqa: E402

YEARS = list(range(2015, 2026))
FY_END = "-12-31"


def _annual_entries(tag_values: dict[int, float], instant: bool):
    """
    Emit entries the way EDGAR does: each 10-K repeats the two prior years,
    and every entry carries the filing's fiscal year, not the period's.
    """
    out = []
    for filing_year in YEARS:
        filed = f"{filing_year + 1}-02-15"
        for back in (0, 1, 2):                      # this year + 2 comparatives
            period = filing_year - back
            if period not in tag_values:
                continue
            item = {
                "val": tag_values[period],
                "accn": f"0000000000-{filing_year}-000001",
                "fy": filing_year,                   # THE FILING's year
                "fp": "FY",
                "form": "10-K",
                "filed": filed,
                "end": f"{period}{FY_END}",
            }
            if not instant:
                item["start"] = f"{period}-01-01"
            out.append(item)
    return out


def make_companyfacts(cik: int, name: str, profitable=True, growth=1.06):
    rev = {y: 1000.0 * (growth ** (y - 2015)) for y in YEARS}
    ni_margin = 0.166 if profitable else 0.02
    flows = {
        "Revenues": 1.0, "CostOfRevenue": 0.55, "OperatingIncomeLoss": 0.22,
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": 0.21,
        "IncomeTaxExpenseBenefit": 0.044, "NetIncomeLoss": ni_margin,
        "DepreciationDepletionAndAmortization": 0.04,
        "NetCashProvidedByUsedInOperatingActivities": 0.22,
        "PaymentsToAcquirePropertyPlantAndEquipment": 0.05,
        "InterestExpense": 0.005, "ShareBasedCompensation": 0.02,
        "ResearchAndDevelopmentExpense": 0.08,
    }
    instants = {
        "Assets": 1.5, "StockholdersEquity": 0.75,
        "CashAndCashEquivalentsAtCarryingValue": 0.10,
        "LongTermDebtNoncurrent": 0.25, "Goodwill": 0.15,
        "IntangibleAssetsNetExcludingGoodwill": 0.05,
    }
    gaap = {}
    for tag, frac in flows.items():
        gaap[tag] = {"units": {"USD": _annual_entries({y: rev[y] * frac for y in YEARS}, False)}}
    for tag, frac in instants.items():
        gaap[tag] = {"units": {"USD": _annual_entries({y: rev[y] * frac for y in YEARS}, True)}}
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
        "units": {"shares": _annual_entries({y: 100e6 * (0.98 ** (y - 2015)) for y in YEARS}, False)}}

    # NOTE: no "sic", no "exchanges", no "tickers" — exactly like the real file.
    return {
        "cik": cik, "entityName": name,
        "facts": {
            "us-gaap": gaap,
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2025-12-31", "val": 90_000_000, "form": "10-K", "fy": 2025, "fp": "FY"}]}}},
        },
    }


def make_submissions(cik: int, name: str, sic: str, exchanges: list[str]):
    return {"cik": cik, "name": name, "sic": sic,
            "sicDescription": f"SIC {sic}", "exchanges": exchanges,
            "tickers": [f"T{cik}"]}


def statistics_median_owner_earnings(s):
    import statistics as st
    import metrics as M
    ys = M.common_years(s, ["net_income", "depreciation_amortisation", "capex"], 3)
    return st.median([M.owner_earnings(s, y) for y in ys])


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    return ok


def main():
    failures = 0
    work = Path("/tmp/pipeline_fixture")
    shutil.rmtree(work, ignore_errors=True)
    (work / "data").mkdir(parents=True)
    (work / "out").mkdir()

    specs = [
        (1, "Consumer Co",   "5331", ["Nasdaq"],              True),
        (2, "Software Co",   "7372", ["NYSE"],                True),
        (3, "Bank Co",       "6021", ["NYSE"],                True),
        (4, "Biotech Co",    "2836", ["Nasdaq"],              True),   # excluded by SIC
        (5, "SPAC Co",       "6770", ["Nasdaq"],              True),   # excluded by SIC
        (6, "REIT Co",       "6798", ["NYSE"],                True),
        (7, "Foreign Co",    "5331", ["LSE"],                 True),   # wrong exchange
        (8, "OTC Co",        "5331", [],                      True),   # no exchange
        (9, "Asset Mgr",     "6282", ["NYSE"],                True),   # fee business
        (10, "Multi Class",  "5331", ["Nasdaq"],              True),   # share classes
    ]

    with zipfile.ZipFile(work / "data" / "companyfacts.zip", "w") as z:
        for cik, name, _, _, prof in specs:
            z.writestr(f"CIK{cik:010d}.json", json.dumps(make_companyfacts(cik, name, prof)))
    with zipfile.ZipFile(work / "data" / "submissions.zip", "w") as z:
        for cik, name, sic, exch, _ in specs:
            z.writestr(f"CIK{cik:010d}.json", json.dumps(make_submissions(cik, name, sic, exch)))
    tickmap = {str(i): {"cik_str": cik, "ticker": f"T{cik}", "title": name}
               for i, (cik, name, _, _, _) in enumerate(specs)}
    # CIK 10 listed under three share classes, as Alphabet is under four.
    for j, suffix in enumerate(("A", "B", "C"), start=100):
        tickmap[str(j)] = {"cik_str": 10, "ticker": f"T10{suffix}", "title": "Multi Class"}
    json.dump(tickmap, open(work / "data" / "company_tickers.json", "w"))

    print("\n=== companyfacts must NOT be expected to carry sector data ===")
    facts = make_companyfacts(1, "Consumer Co")
    failures += not check("no 'sic' key in companyfacts (matches the real format)",
                          "sic" not in facts, str(list(facts)))
    failures += not check("submissions supplies sic and exchanges",
                          make_submissions(1, "x", "5331", ["Nasdaq"])["sic"] == "5331")

    print("\n=== fiscal years must come from the period, not the filing ===")
    series = secdata.extract(facts)
    ni = series.series("net_income")
    failures += not check("11 distinct fiscal years recovered from overlapping filings",
                          len(ni) == 11, f"got {len(ni)}: {sorted(ni)}")
    failures += not check("values increase year on year (comparatives not collapsed)",
                          all(ni[y] < ni[y + 1] for y in sorted(ni)[:-1]),
                          f"{sorted(ni)[0]}={ni[min(ni)]:,.0f} .. {sorted(ni)[-1]}={ni[max(ni)]:,.0f}")
    failures += not check("a Jan year-end is labelled the prior year",
                          secdata._fiscal_year_of("2024-01-31") == 2023)
    failures += not check("a Dec year-end keeps its own year",
                          secdata._fiscal_year_of("2023-12-31") == 2023)

    print("\n=== universe build against realistic fixtures ===")
    cwd = Path.cwd()
    try:
        import os
        os.chdir(work)
        for f in ["config.py", "secdata.py", "metrics.py", "gates.py",
                  "universe.py", "run_screen.py", "screener_import.py"]:
            shutil.copy(ROOT / f, work / f)
        os.environ["SEC_USER_AGENT"] = "Test User test@example.com"
        n = U.build(work / "out", work / "data" / "companyfacts.zip",
                    work / "data" / "submissions.zip")
        failures += not check("universe is not empty", n > 0, f"{n} names")
        rows = list(__import__("csv").DictReader(open(work / "out" / "universe.csv")))
        got = {r["ticker"]: r["module"] for r in rows}
        failures += not check("biotech excluded by SIC", "T4" not in got)
        failures += not check("SPAC excluded by SIC", "T5" not in got)
        failures += not check("foreign-listed excluded by exchange", "T7" not in got)
        failures += not check("unlisted excluded by exchange", "T8" not in got)
        # REITs and utilities are now excluded by policy (config.EXCLUDE_REITS_AND_UTILITIES),
        # because they fund themselves by issuing equity and running negative FCF,
        # which C1 and C2 treat as disqualifying.
        failures += not check("REIT excluded by policy, not by defect",
                              "T6" not in got, str(got))
        failures += not check("the classifier still knows a REIT when the policy is off",
                              U.sic_to_module("6798") == "reit")
        failures += not check("exclusion states its reason",
                              "EXCLUDE_REITS_AND_UTILITIES" in (U.excluded_by_sic("6798") or ""),
                              U.excluded_by_sic("6798") or "")
        failures += not check("utilities excluded on the same policy",
                              "EXCLUDE_REITS_AND_UTILITIES" in (U.excluded_by_sic("4911") or ""))
        failures += not check("bank classified as financials", got.get("T3") == "financials")
        failures += not check("software classified as software", got.get("T2") == "software")
        failures += not check("fee business routed off the bank track",
                              got.get("T9") == "consumer", str(got.get("T9")))
        failures += not check("share classes deduped to one row per company",
                              sum(1 for r in rows if r["cik"] == "10") == 1,
                              f"{sum(1 for r in rows if r['cik']=='10')} rows for CIK 10")

        print("\n=== gates run over that universe ===")
        r = subprocess.run([sys.executable, "run_screen.py", "--screen"],
                           capture_output=True, text=True, cwd=work)
        failures += not check("gate run exits cleanly", r.returncode == 0,
                              (r.stdout + r.stderr)[-300:])
        failures += not check("results.csv written", (work / "out" / "results.csv").exists())
    finally:
        import os
        os.chdir(cwd)

    print("\n=== fix 1: C4 must not pass on unknown interest cover ===")
    import gates as G, metrics as MM
    lev = make_companyfacts(90, "Levered Co")
    del lev["facts"]["us-gaap"]["InterestExpense"]          # untagged, as ~331 real ones are
    ser = secdata.extract(lev)
    failures += not check("interest_cover returns None, not infinity",
                          MM.interest_cover(ser, 2025) is None)
    g = {x.code: x for x in G.run_gates(ser, "LEV", "consumer").gates}["C4"]
    failures += not check("C4 is unevaluable, not a pass, when net debt exists",
                          g.passed is not True, g.reason[:90])

    print("\n=== fix 2: REIT and utility gates run instead of crashing ===")
    for sector in ("reit", "utilities"):
        res = G.run_gates(secdata.extract(make_companyfacts(91, "X")), "X", sector)
        g = {x.code: x for x in res.gates}["M-ROIC"]
        failures += not check(f"{sector} M-ROIC evaluates without KeyError",
                              not g.reason.startswith("error"), g.reason[:80])

    print("\n=== a code defect must fail the run, not be logged as missing data ===")
    broken = dict(config.SECTOR_MODULES["consumer"])
    missing_key = {k: v for k, v in broken.items() if k != "roic_median_min"}
    config.SECTOR_MODULES["consumer"] = missing_key   # exactly the reit/utility bug
    try:
        G.run_gates(secdata.extract(make_companyfacts(92, "Y")), "Y", "consumer")
        failures += not check("broken config raises rather than silently failing", False)
    except RuntimeError as e:
        failures += not check("broken config raises RuntimeError", True, str(e)[:70])
    except Exception as e:
        failures += not check("broken config raises RuntimeError", False, f"got {type(e).__name__}")
    finally:
        config.SECTOR_MODULES["consumer"] = broken

    print("\n=== fix 3: C7 credits capital returned, not just retained ===")
    shrink = make_companyfacts(93, "Buyback Co")
    for tag, direction in (("StockholdersEquity", -1), ("LongTermDebtNoncurrent", -1),
                           ("CashAndCashEquivalentsAtCarryingValue", +1)):
        for it in shrink["facts"]["us-gaap"][tag]["units"]["USD"]:
            k = int(it["end"][:4]) - 2015
            it["val"] = it["val"] * ((0.93 ** k) if direction < 0 else (1.05 ** k))
    ser = secdata.extract(shrink)
    ca = MM.capital_allocation(ser)
    failures += not check("capital_allocation reports the 'returned' mode",
                          ca is not None and ca[0] == "returned", str(ca))
    g = {x.code: x for x in G.run_gates(ser, "BB", "consumer").gates}["C7"]
    failures += not check("C7 is decided, not left unevaluable",
                          g.passed is not None, g.reason[:90])

    print("\n=== fix 4: financials get the 20-year loss test ===")
    ni = {y: 100.0 for y in YEARS}; ni[2020] = -50.0
    lossy = make_companyfacts(94, "Lossy Bank")
    lossy["facts"]["us-gaap"]["NetIncomeLoss"] = {"units": {"USD": _annual_entries(ni, False)}}
    g = {x.code: x for x in G.run_gates(secdata.extract(lossy), "LB", "financials").gates}["M-LOSS"]
    failures += not check("a loss year fails M-LOSS", g.passed is False, g.reason)
    g = {x.code: x for x in G.run_gates(
        secdata.extract(make_companyfacts(95, "Clean Bank")), "CB", "financials").gates}["M-LOSS"]
    failures += not check("a clean record passes M-LOSS", g.passed is True, g.reason)

    print("\n=== Tier 4: scoring ===")
    import rank as R
    good = secdata.extract(make_companyfacts(96, "Good Co"))
    comps = R.score_components(good, "consumer", oe_yield=0.085,
                               discount_to_iv=0.45, multiple_vs_history=-1.0)
    score, earned, avail = R.total_score(comps)
    failures += not check("score normalises over measurable points only",
                          0 < score <= 100 and avail <= 70, f"{score:.1f} from {earned:.0f}/{avail:.0f}")
    failures += not check("30 points are declared as needing a human",
                          R.MANUAL_POINTS == 30, str(R.MANUAL_POINTS))
    cheap = R.total_score(R.score_components(good, "consumer", oe_yield=0.085,
                                             discount_to_iv=0.45, multiple_vs_history=-1.0))[0]
    dear = R.total_score(R.score_components(good, "consumer", oe_yield=0.03,
                                            discount_to_iv=-0.10, multiple_vs_history=1.5))[0]
    failures += not check("a cheaper price scores higher than a dear one",
                          cheap > dear, f"cheap {cheap:.1f} vs dear {dear:.1f}")
    no_price = R.total_score(R.score_components(good, "consumer"))
    failures += not check("a company with no price is scored on fewer available points",
                          no_price[2] < avail, f"{no_price[2]:.0f} vs {avail:.0f} available")

    failures += not check("a fully priced company is flagged price-comparable",
                          R.is_price_comparable(comps))
    failures += not check("an unpriced company is NOT flagged price-comparable",
                          not R.is_price_comparable(R.score_components(good, "consumer")))

    print("\n=== Tier 5: valuation ===")
    iv = R.dcf_intrinsic_value(good)
    failures += not check("DCF returns a positive intrinsic value", iv and iv > 0,
                          f"{iv:,.0f}" if iv else "None")
    # Growth must fade, or a fast grower gets valued at a multiple no
    # Buffett-style screen should pay. Run 3 implied ~24x owner earnings.
    fast = secdata.extract(make_companyfacts(97, "Fast Co", growth=1.15))
    base = statistics_median_owner_earnings(fast)
    mult = R.dcf_intrinsic_value(fast) / base
    failures += not check("a 15% grower is valued below 20x owner earnings",
                          mult < 20, f"{mult:.1f}x")
    slow = secdata.extract(make_companyfacts(98, "Slow Co", growth=1.03))
    slow_mult = R.dcf_intrinsic_value(slow) / statistics_median_owner_earnings(slow)
    failures += not check("faster growth still earns a higher multiple than slower",
                          mult > slow_mult, f"{mult:.1f}x vs {slow_mult:.1f}x")
    bp_wide = R.buy_price(iv, 100e6, "wide")
    bp_unc = R.buy_price(iv, 100e6, "uncertain")
    failures += not check("a wider moat permits a higher buy price",
                          bp_wide > bp_unc, f"wide {bp_wide:.2f} vs uncertain {bp_unc:.2f}")
    failures += not check("wide moat applies exactly the 30% margin of safety",
                          abs(bp_wide - (iv / 100e6) * 0.70) < 1e-6)
    failures += not check("unknown moat defaults to the harshest 50% haircut",
                          abs(R.buy_price(iv, 100e6, "not-a-moat") - (iv / 100e6) * 0.50) < 1e-6)
    y = R.owner_earnings_yield(good, 1_000.0)
    failures += not check("owner-earnings yield is computable from a market cap",
                          y is not None and y > 0, f"{y:.1%}" if y else "None")

    print(f"\n{'='*60}")
    print("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    print(f"{'='*60}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
