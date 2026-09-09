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
    ]

    with zipfile.ZipFile(work / "data" / "companyfacts.zip", "w") as z:
        for cik, name, _, _, prof in specs:
            z.writestr(f"CIK{cik:010d}.json", json.dumps(make_companyfacts(cik, name, prof)))
    with zipfile.ZipFile(work / "data" / "submissions.zip", "w") as z:
        for cik, name, sic, exch, _ in specs:
            z.writestr(f"CIK{cik:010d}.json", json.dumps(make_submissions(cik, name, sic, exch)))
    json.dump({str(i): {"cik_str": cik, "ticker": f"T{cik}", "title": name}
               for i, (cik, name, _, _, _) in enumerate(specs)},
              open(work / "data" / "company_tickers.json", "w"))

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
        failures += not check("REIT classified as reit", got.get("T6") == "reit", str(got))
        failures += not check("bank classified as financials", got.get("T3") == "financials")
        failures += not check("software classified as software", got.get("T2") == "software")

        print("\n=== gates run over that universe ===")
        r = subprocess.run([sys.executable, "run_screen.py", "--screen"],
                           capture_output=True, text=True, cwd=work)
        failures += not check("gate run exits cleanly", r.returncode == 0,
                              (r.stdout + r.stderr)[-300:])
        failures += not check("results.csv written", (work / "out" / "results.csv").exists())
    finally:
        import os
        os.chdir(cwd)

    print(f"\n{'='*60}")
    print("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    print(f"{'='*60}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
