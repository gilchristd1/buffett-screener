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


def make_companyfacts(cik: int, name: str, profitable=True, growth=1.06,
                      margin_decay=1.0, recent_decline=1.0):
    """
    `margin_decay` < 1 erodes the profit margins year by year while revenue
    still grows — a business whose advantage is being competed away. Needed to
    test the moat trend signal, which the flat-margin fixture cannot exercise.
    """
    rev = {y: 1000.0 * (growth ** (y - 2015)) for y in YEARS}
    decay = {y: margin_decay ** (y - 2015) for y in YEARS}
    PROFIT_TAGS = {
        "OperatingIncomeLoss", "NetIncomeLoss", "IncomeTaxExpenseBenefit",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItems"
        "NoncontrollingInterest",
    }
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
        d = decay if tag in PROFIT_TAGS else {y: 1.0 for y in YEARS}
        gaap[tag] = {"units": {"USD": _annual_entries(
            {y: rev[y] * frac * d[y] for y in YEARS}, False)}}
    for tag, frac in instants.items():
        gaap[tag] = {"units": {"USD": _annual_entries({y: rev[y] * frac for y in YEARS}, True)}}
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
        "units": {"shares": _annual_entries({y: 100e6 * (0.98 ** (y - 2015)) for y in YEARS}, False)}}

    # Quarterly entries, exactly as a 10-Q emits them: the three-month period
    # AND the year-to-date cumulative, both tagged. Q4 is never filed. Code that
    # takes "the last four entries" splices a cumulative or skips Q4 and
    # compares fifteen months to twelve.
    #
    # `recent_decline` shrinks the most recent four quarters, so a business that
    # looks strong on ten years of annual filings can be falling apart now —
    # the lululemon case that C9 exists to catch.
    qflows = {"Revenues": 1.0, "OperatingIncomeLoss": 0.22}
    for tag, frac in qflows.items():
        entries = []
        for y in YEARS:
            ytd = 0.0
            for qi, (qs, qe) in enumerate(
                    [("01-01", "03-31"), ("04-01", "06-30"),
                     ("07-01", "09-30"), ("10-01", "12-31")]):
                base = rev[y] * frac / 4
                if tag in PROFIT_TAGS or tag == "OperatingIncomeLoss":
                    base *= decay[y]
                if recent_decline != 1.0 and y == YEARS[-1]:
                    base *= recent_decline
                ytd += base
                if qi == 3:
                    continue          # Q4 is not filed on a 10-Q
                entries.append({"val": base, "fy": y, "fp": f"Q{qi+1}",
                                "form": "10-Q", "filed": f"{y}-{(qi+1)*3+1:02d}-15",
                                "start": f"{y}-{qs}", "end": f"{y}-{qe}"})
                entries.append({"val": ytd, "fy": y, "fp": f"Q{qi+1}",
                                "form": "10-Q", "filed": f"{y}-{(qi+1)*3+1:02d}-15",
                                "start": f"{y}-01-01", "end": f"{y}-{qe}"})
        gaap[tag]["units"]["USD"].extend(entries)

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
                  "universe.py", "run_screen.py", "screener_import.py",
                  "rank.py", "moat.py", "overlay.py", "track.py"]:
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
        failures += not check("fee business gets its own module, not a borrowed one",
                              got.get("T9") == "fee_business", str(got.get("T9")))
        failures += not check("share classes deduped to one row per company",
                              sum(1 for r in rows if r["cik"] == "10") == 1,
                              f"{sum(1 for r in rows if r['cik']=='10')} rows for CIK 10")

        print("\n=== gates run over that universe ===")
        r = subprocess.run([sys.executable, "run_screen.py", "--screen"],
                           capture_output=True, text=True, cwd=work)
        failures += not check("gate run exits cleanly", r.returncode == 0,
                              (r.stdout + r.stderr)[-300:])
        failures += not check("results.csv written", (work / "out" / "results.csv").exists())

        # --- the rank step, end to end ---------------------------------------
        # Run 3 shipped a rank step whose output never reached the repo because
        # nothing here exercised it. Synthesise a priced survivor list and run
        # the real CLI over it, so the wiring is tested rather than assumed.
        import csv as _csv
        with open(work / "out" / "survivors.csv", "w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=["ticker", "name", "module",
                                                "shares", "market_cap", "adv", "excused"])
            w.writeheader()
            for row in rows[:4]:
                w.writerow({"ticker": row["ticker"], "name": row["name"],
                            "module": row["module"], "shares": "100000000",
                            "market_cap": "5000000000", "adv": "20000000",
                            "excused": ""})
        r = subprocess.run([sys.executable, "run_screen.py", "--rank"],
                           capture_output=True, text=True, cwd=work)
        failures += not check("rank step exits cleanly", r.returncode == 0,
                              (r.stdout + r.stderr)[-300:])
        failures += not check("ranked.csv written", (work / "out" / "ranked.csv").exists())
        failures += not check("moat.csv written", (work / "out" / "moat.csv").exists())
        if (work / "out" / "ranked.csv").exists():
            rk = list(_csv.DictReader(open(work / "out" / "ranked.csv")))
            failures += not check("ranked rows carry a computed durability score",
                                  all(r_["moat_source"] == "computed" and r_["durability"]
                                      for r_ in rk), str(rk[:1])[:120])
            failures += not check("no survivor defaults to a hand-typed 'narrow'",
                                  any(r_["moat"] != "narrow" for r_ in rk)
                                  or all(float(r_["durability"]) > 0 for r_ in rk))
            failures += not check("the margin of safety sits inside its configured range",
                                  all(0.30 <= float(r_["margin_of_safety"]) <= 0.50
                                      for r_ in rk))
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

    print("\n=== the tracking record is append-only ===")
    import track as TR
    tpath = Path("/tmp/pipeline_fixture/track_test.csv")
    tpath.unlink(missing_ok=True)
    r1 = [{"ticker": "AAA", "name": "A", "module": "consumer", "score": "80",
           "durability": "85", "moat": "wide", "price": "10", "buy_price": "12",
           "owner_earnings_yield": "0.09", "hurdle": "0.06", "clears_hurdle": "yes"},
          {"ticker": "BBB", "name": "B", "module": "consumer", "score": "70",
           "durability": "60", "moat": "narrow", "price": "20", "buy_price": "9",
           "owner_earnings_yield": "0.03", "hurdle": "0.08", "clears_hurdle": "no"}]
    s1 = TR.update(tpath, r1, "2026-09-11")
    failures += not check("first run records both names", s1["appended"] == 2, str(s1))

    s2 = TR.update(tpath, r1, "2026-12-01")
    failures += not check("an unchanged run appends nothing", s2["appended"] == 0, str(s2))

    r2 = [dict(r1[0], clears_hurdle="no"), r1[1]]
    s3 = TR.update(tpath, r2, "2027-03-01")
    failures += not check("a name that stops clearing is recorded",
                          s3["appended"] == 1, str(s3))

    s4 = TR.update(tpath, [r1[1]], "2027-06-01")
    failures += not check("a name leaving the list is recorded too",
                          s4["appended"] == 1, str(s4))

    hist = TR.load(tpath)
    failures += not check("history is never rewritten — 4 rows, oldest intact",
                          len(hist) == 4 and hist[0]["run_date"] == "2026-09-11"
                          and hist[0]["status"] == "cleared", str(len(hist)))
    failures += not check("first_seen survives a status change",
                          [h for h in hist if h["ticker"] == "AAA"][-1]["first_seen"]
                          == "2026-09-11")

    print("\n=== M2: cyclicals valued at mid-cycle, not at the peak ===")
    # A company whose last three years are far above its ten-year norm. §M2 has
    # required this since v0.1 and nothing implemented it, which is how Toll
    # Brothers reached run 10 as the only name below its buy price on a yield
    # drawn from an exceptional housing market.
    # A cycle is MARGINS peaking, not revenue growing. A constant-margin grower
    # has no cycle to normalise — its latest year is its best estimate. Here
    # margins compound upward, so the last three years sit well above the
    # ten-year norm, which is what a homebuilder at the top of a cycle looks like.
    boom = secdata.extract(make_companyfacts(70, "Boom Co", growth=1.04,
                                             margin_decay=1.12))
    mid = MM.mid_cycle_owner_earnings(boom)
    med3 = statistics_median_owner_earnings(boom)
    failures += not check("mid-cycle earnings sit below a three-year median in a boom",
                          mid is not None and mid < med3, f"{mid:,.0f} vs {med3:,.0f}")

    cyc = R.normalised_owner_earnings(boom, None, "industrials")
    non = R.normalised_owner_earnings(boom, None, "consumer")
    failures += not check("a cyclical is valued on the lower of the two",
                          cyc[0] < non[0], f"{cyc[0]:,.0f} vs {non[0]:,.0f}")
    failures += not check("a non-cyclical is untouched by the normalisation",
                          abs(non[0] - med3) < 1e-6)
    iv_cyc = R.dcf_intrinsic_value(boom, None, "industrials")
    iv_non = R.dcf_intrinsic_value(boom, None, "consumer")
    failures += not check("the DCF applies the same discipline, not just the yield",
                          iv_cyc < iv_non, f"{iv_cyc:,.0f} vs {iv_non:,.0f}")
    y_cyc = R.owner_earnings_yield(boom, 5_000.0, None, "industrials")
    y_non = R.owner_earnings_yield(boom, 5_000.0, None, "consumer")
    failures += not check("so a boom cannot be mistaken for a run-rate yield",
                          y_cyc < y_non, f"{y_cyc:.1%} vs {y_non:.1%}")

    # And it must not punish a business whose margins are simply stable.
    flat = secdata.extract(make_companyfacts(71, "Steady Cyclical", growth=1.04,
                                             margin_decay=1.0))
    f_cyc = R.normalised_owner_earnings(flat, None, "industrials")
    f_non = R.normalised_owner_earnings(flat, None, "consumer")
    failures += not check("a steady business is barely affected by mid-cycling",
                          abs(f_cyc[0] - f_non[0]) / f_non[0] < 0.12,
                          f"{f_cyc[0]:,.0f} vs {f_non[0]:,.0f}")

    print("\n=== ROIC: average capital, and R&D added back after tax ===")
    # Open item 22 from v0.7. Two defects in one ratio, pulling in OPPOSITE
    # directions, which is why neither showed up as an obviously wrong number.
    grower = secdata.extract(make_companyfacts(80, "Grower Inc", growth=1.06))
    fy = 2025

    close_ic = MM.invested_capital(grower, fy)
    avg_ic = MM.average_invested_capital(grower, fy)
    failures += not check("average invested capital sits below closing for a grower",
                          avg_ic < close_ic, f"{avg_ic:,.0f} vs {close_ic:,.0f}")
    # ...so the corrected ROIC is HIGHER. Closing capital charged the company
    # for money it did not have for most of the year. Stating the direction in
    # the test because it is easy to assume the fix must be the harsher one.
    np_ = MM.nopat(grower, fy)
    failures += not check("so the corrected ROIC exceeds the closing-capital version",
                          MM.roic(grower, fy) > np_ / close_ic,
                          f"{MM.roic(grower, fy):.2%} vs {np_ / close_ic:.2%}")

    # The first year of a series has no opening balance. It must fall back to
    # closing capital rather than dropping the year — ten years of history is
    # what C3, C7 and the durability score all depend on.
    first = min(grower.series("total_equity"))
    failures += not check("the earliest year falls back to closing capital, not None",
                          MM.average_invested_capital(grower, first) is not None)

    # R&D: the addback must be tax-effected, because NOPAT is after tax.
    r_taxed = MM.roic(grower, fy, capitalise_rnd=True)
    rate = MM.effective_tax_rate(grower, fy)
    rnd = abs(grower.series("research_development")[fy])
    amort = sum(abs(grower.series("research_development").get(fy - k, 0.0)) / 5
                for k in range(5))
    crd = MM.capitalised_rnd_balance(grower, fy)
    gross = (np_ + rnd - amort) / MM.average_invested_capital(grower, fy, crd)
    failures += not check("the tax rate is the same one NOPAT used",
                          0.0 < rate <= 0.50, f"{rate:.1%}")
    failures += not check("tax-effecting the R&D addback lowers software ROIC",
                          r_taxed < gross, f"{r_taxed:.2%} vs {gross:.2%} untaxed")
    # The capital base is NOT tax-effected — the cash spent on R&D left in full.
    failures += not check("the capitalised R&D balance still enters capital gross",
                          MM.average_invested_capital(grower, fy, crd) >
                          MM.average_invested_capital(grower, fy),
                          f"{crd:,.0f} of R&D capital")

    print("\n=== the mid-cycle cap must report whether it bound ===")
    # Run 11 shipped the cap and Toll Brothers' yield moved 10.05% -> 9.84%,
    # explained entirely by its share price that day. The output could not say
    # whether the cap failed to bind because the data was too short or because
    # the median margin genuinely was the current one. A threshold whose
    # failure to bind is invisible is assumed, not calibrated.
    d_boom = MM.mid_cycle_detail(boom)
    failures += not check("the detail records how many years the median rests on",
                          d_boom["margin_years"] >= 5, f"{d_boom['margin_years']} years")
    failures += not check("and the span those years cover, not just the count",
                          d_boom["span"] >= d_boom["margin_years"],
                          f"span {d_boom['span']}")
    failures += not check("in a boom the latest margin sits above the median",
                          d_boom["latest_margin"] > d_boom["median_margin"],
                          f"{d_boom['latest_margin']:.1%} vs {d_boom['median_margin']:.1%}")
    failures += not check("and the reported mid-cycle matches the value used",
                          abs(d_boom["mid_cycle"] - mid) < 1e-6)
    failures += not check("the window matches §1's 15-year cyclical requirement",
                          config.MID_CYCLE_LOOKBACK_YEARS == 15,
                          f"{config.MID_CYCLE_LOOKBACK_YEARS}")
    # Span, not count, is the test. Run 12 found Toll Brothers' cap computed
    # over nine years spanning 2017-2025 — an unbroken housing expansion whose
    # median is itself a boom — and the cap therefore sat above the base and
    # did nothing. The fixture runs 2015-2025, so it passes; a short one must
    # not.
    # span_ok must follow the configured bar, not a literal. Worth noting what
    # this fixture shows: eleven years of clean, complete data is still
    # reported as too short to contain a cycle. That is the intended severity —
    # the bar is about whether a downturn is in view, not about data quality.
    failures += not check("span adequacy follows the configured minimum",
                          d_boom["span_ok"] == (d_boom["span"] >= config.MID_CYCLE_MIN_SPAN),
                          f"span {d_boom['span']} vs min {config.MID_CYCLE_MIN_SPAN}")
    short = secdata.extract(make_companyfacts(72, "Short History Co",
                                              growth=1.04, margin_decay=1.12))
    for f in ("net_income", "depreciation_amortisation", "capex", "revenue"):
        ser = short.series(f)
        for y in list(ser):
            if y < 2021:
                del ser[y]
    d_short = MM.mid_cycle_detail(short)
    failures += not check("a five-year history is reported as too short to be a cycle",
                          d_short["span_ok"] is False, f"span {d_short['span']}")
    failures += not check("...and it does not block yet, by configuration",
                          config.MID_CYCLE_SHORT_SPAN_BLOCKS is False)
    failures += not check("a short span still returns a base while blocking is off",
                          R.normalised_owner_earnings(short, None, "industrials") is not None)

    d_flat = MM.mid_cycle_detail(flat)
    failures += not check("a steady business shows a latest margin near its median",
                          abs(d_flat["latest_margin"] - d_flat["median_margin"])
                          / d_flat["median_margin"] < 0.02,
                          f"{d_flat['latest_margin']:.2%} vs {d_flat['median_margin']:.2%}")

    print("\n=== C10: dated facts from outside the filings ===")
    import overlay as OV
    steady0 = secdata.extract(make_companyfacts(85, "Overlay Co"))
    G.OVERLAY = {}
    g = {x.code: x for x in G.run_gates(steady0, "OVR", "consumer").gates}["C10"]
    failures += not check("no overlay entry never blocks a company",
                          g.passed is True, g.reason)

    # Run 7: lululemon passed C9 by one percentage point while its management
    # had already cut full-year guidance twice. Two cuts is the blocking fact.
    G.OVERLAY = {"OVR": {"ticker": "OVR", "guidance": "cut", "consecutive_cuts": "2",
                         "severity": "watch", "issue": "full-year guidance cut twice"}}
    g = {x.code: x for x in G.run_gates(steady0, "OVR", "consumer").gates}["C10"]
    failures += not check("two consecutive guidance cuts block", g.passed is False, g.reason)
    G.OVERLAY = {"OVR": {"ticker": "OVR", "guidance": "cut", "consecutive_cuts": "1",
                         "severity": "watch"}}
    g = {x.code: x for x in G.run_gates(steady0, "OVR", "consumer").gates}["C10"]
    failures += not check("one cut is recorded but does not block", g.passed is True, g.reason)
    G.OVERLAY = {"OVR": {"ticker": "OVR", "severity": "veto", "issue": "regulator action"}}
    g = {x.code: x for x in G.run_gates(steady0, "OVR", "consumer").gates}["C10"]
    failures += not check("an explicit veto blocks", g.passed is False, g.reason)
    # A one-off gain inside operating income makes C9 read backwards. The 2026
    # tariff refunds are the live case: Five Below's operating margin went 5.1%
    # to 21.8% on $163.6m of refunds, which C9 would otherwise read as a
    # business improving sharply.
    G.OVERLAY = {"OVR": {"ticker": "OVR", "earnings_distorted": "yes",
                         "issue": "tariff refund in operating income",
                         "severity": "watch"}}
    g = {x.code: x for x in G.run_gates(steady0, "OVR", "consumer").gates}["C9"]
    failures += not check("a distorted period makes C9 unevaluable, not a pass",
                          g.passed is None, g.reason[:80])
    g10 = {x.code: x for x in G.run_gates(steady0, "OVR", "consumer").gates}["C10"]
    failures += not check("...and a distortion alone does not block the company",
                          g10.passed is True, g10.reason[:60])
    G.OVERLAY = {}

    # Guidance caps the valuation base, and only ever downward.
    f_down = OV.earnings_factor({"guided_fy_earnings_change": "-0.40"})
    f_up = OV.earnings_factor({"guided_fy_earnings_change": "0.30"})
    failures += not check("guidance below last year cuts the valuation base",
                          abs(f_down - 0.60) < 1e-9, f"{f_down}")
    failures += not check("guidance above last year buys no credit",
                          f_up == 1.0, f"{f_up}")
    iv_plain = R.dcf_intrinsic_value(steady0)
    iv_guided = R.dcf_intrinsic_value(steady0, 0.60)
    failures += not check("the DCF honours guided earnings",
                          iv_guided < iv_plain, f"{iv_guided:,.0f} vs {iv_plain:,.0f}")

    print("\n=== C9: the screen must see the business as it trades now ===")
    steady = secdata.extract(make_companyfacts(80, "Steady Co"))
    breaking = secdata.extract(make_companyfacts(81, "Breaking Co", recent_decline=0.62))

    # The Q4 splice is the trap: Q4 is never filed on a 10-Q, and a 10-Q also
    # reports the year-to-date cumulative alongside the quarter.
    run = MM.recent_quarters(steady, "revenue")
    failures += not check("only three-month periods are treated as quarters",
                          run is not None and all(80 <= MM._days(a_, b_) <= 100
                                                  for a_, b_ in zip(run, run[1:])),
                          str(run))
    failures += not check("year-to-date cumulatives are not counted as quarters",
                          run is not None and len(run) <= 4, str(run))

    cur = MM.current_vs_year_ago(steady)
    failures += not check("a steady business shows growth, not a break",
                          cur and cur["revenue_growth"] > 0, f"{cur['revenue_growth']:+.1%}")
    curb = MM.current_vs_year_ago(breaking)
    failures += not check("a collapsing quarter is visible year on year",
                          curb and curb["revenue_growth"] < -0.05,
                          f"{curb['revenue_growth']:+.1%}")

    g_ok = {x.code: x for x in G.run_gates(steady, "OK", "consumer").gates}["C9"]
    g_no = {x.code: x for x in G.run_gates(breaking, "NO", "consumer").gates}["C9"]
    failures += not check("C9 passes a business still trading well", g_ok.passed is True,
                          g_ok.reason)
    failures += not check("C9 FAILS a business whose current trading has broken",
                          g_no.passed is False, g_no.reason)

    # The haircut, which is the half that changes the price rather than the list.
    f_ok = MM.current_earnings_factor(steady)
    f_no = MM.current_earnings_factor(breaking)
    failures += not check("a growing business gets no upward credit (factor capped at 1)",
                          f_ok == 1.0, f"{f_ok}")
    failures += not check("a declining business is valued on the lower figure",
                          f_no is not None and f_no < 0.8, f"{f_no:.2f}")
    iv_ok = R.dcf_intrinsic_value(steady)
    iv_no = R.dcf_intrinsic_value(breaking)
    failures += not check("the DCF is cut by the same haircut, not just the yield",
                          iv_no < iv_ok, f"{iv_no:,.0f} vs {iv_ok:,.0f}")
    y_ok = R.owner_earnings_yield(steady, 5_000.0)
    y_no = R.owner_earnings_yield(breaking, 5_000.0)
    failures += not check("the owner-earnings yield no longer flatters a broken business",
                          y_no < y_ok, f"{y_no:.1%} vs {y_ok:.1%}")

    print("\n=== fee businesses and media get their own gates ===")
    fee = secdata.extract(make_companyfacts(96, "Fee Co"))
    fg = {x.code: x for x in G.run_gates(fee, "FEE", "fee_business").gates}
    failures += not check("C5 does not reject a fee business it cannot measure",
                          fg["C5"].passed is True, fg["C5"].reason)
    failures += not check("C4 defers to the fee balance-sheet test",
                          fg["C4"].passed is True, fg["C4"].reason)
    failures += not check("M-FEE-MARGIN is evaluated, not skipped",
                          fg["M-FEE-MARGIN"].passed is not None, fg["M-FEE-MARGIN"].reason)
    failures += not check("M-FEE-BS is evaluated, not skipped",
                          fg["M-FEE-BS"].passed is not None, fg["M-FEE-BS"].reason)
    # Run 6 excluded Moody's at 0.59x net debt/revenue on a flat 0.50x cap I set
    # with no calibration. A fee business whose revenue has never fallen has
    # shown it can carry debt; one whose revenue swings with markets has not.
    failures += not check("a fee business with never-falling revenue gets the stable cap",
                          "stable cap" in fg["M-FEE-BS"].reason
                          or fg["M-FEE-BS"].value <= 0, fg["M-FEE-BS"].reason)
    # Revenue that actually falls year on year, which is what the stable-cap
    # test reads — a quarterly-only shock leaves the annual record untouched.
    swingy = secdata.extract(make_companyfacts(92, "Cyclical Mgr", growth=0.90))
    sw = {x.code: x for x in G.run_gates(swingy, "SW", "fee_business").gates}["M-FEE-BS"]
    failures += not check("a fee business with falling revenue keeps the tighter cap",
                          "market-linked cap" in sw.reason or sw.value <= 0, sw.reason)
    failures += not check("M-ROIC uses ROE for a fee business",
                          "ROE" in fg["M-ROIC"].reason, fg["M-ROIC"].reason)
    failures += not check("the 20-year loss test applies to fee businesses too",
                          fg["M-LOSS"].passed is not None, fg["M-LOSS"].reason)

    # The two gates must stay inert everywhere else.
    cg = {x.code: x for x in G.run_gates(good, "CON", "consumer").gates}
    failures += not check("the fee gates do not fire on a consumer company",
                          cg["M-FEE-MARGIN"].passed is True and cg["M-FEE-BS"].passed is True)

    # Media: the 6% grower the New York Times failed on must now pass.
    nyt = secdata.extract(make_companyfacts(93, "Legacy Media Co", growth=1.06))
    mg = {x.code: x for x in G.run_gates(nyt, "NYT", "media").gates}
    sg = {x.code: x for x in G.run_gates(nyt, "NYT", "software").gates}
    failures += not check("6% revenue growth fails the software floor",
                          sg["M-GROWTH"].passed is False, sg["M-GROWTH"].reason)
    failures += not check("...and passes the media floor",
                          mg["M-GROWTH"].passed is True, mg["M-GROWTH"].reason)

    print("\n=== Lens C must not call every growing company cheap ===")
    # Run 4: 34 of 37 priced survivors scored negative, median z -0.79. Holding
    # EV constant while EBIT grows makes the latest year the minimum of the
    # series by construction, so the veto could never fire.
    grower = secdata.extract(make_companyfacts(95, "Grower Co", growth=1.10))
    failures += not check("Lens C is unavailable without price history, not biased",
                          R.multiple_vs_history(grower, 5_000.0) is None)

    ebit_years = sorted(grower.series("operating_income"))
    sh = grower.series("diluted_shares")
    ebit = grower.series("operating_income")
    # A price that tracks earnings at roughly 10x, with the mild year-to-year
    # wobble any real multiple has. Today at 10x must read as neither cheap nor
    # dear — the old method, holding EV flat, would have called it cheap.
    # Prices chosen so that EV/EBIT is exactly 10x times a mild yearly wobble —
    # the variation any real multiple has. Today at a flat 10x must then read as
    # neither cheap nor dear. The old method, holding EV constant, called it cheap.
    wobble = [1.00, 1.08, 0.94, 1.05, 0.97, 1.06, 0.95, 1.03, 0.98, 1.02, 1.00]
    px = {y: (10.0 * wobble[i % len(wobble)] * ebit[y] - MM.net_debt(grower, y)) / sh[y]
          for i, y in enumerate(ebit_years)}
    last = ebit_years[-1]
    at = lambda x: x * ebit[last] - MM.net_debt(grower, last)   # market cap at x times EBIT

    z_fair = R.multiple_vs_history(grower, at(10.0), px)
    failures += not check("a fairly-priced grower reads as neither cheap nor dear",
                          z_fair is not None and abs(z_fair) < 1.0, f"z {z_fair:.2f}")
    z_dear = R.multiple_vs_history(grower, at(20.0), px)
    failures += not check("a doubled multiple reads as expensive",
                          z_dear is not None and z_dear > 1.0,
                          f"dear {z_dear:.2f} vs fair {z_fair:.2f}")
    z_cheap = R.multiple_vs_history(grower, at(5.0), px)
    failures += not check("a halved multiple reads as cheap",
                          z_cheap is not None and z_cheap < -1.0,
                          f"cheap {z_cheap:.2f} vs fair {z_fair:.2f}")
    failures += not check("Lens C is unavailable when the history has no variation",
                          R.multiple_vs_history(
                              grower, at(10.0),
                              {y: (10.0 * ebit[y] - MM.net_debt(grower, y)) / sh[y]
                               for y in ebit_years}) is None)

    print("\n=== Moat durability, computed rather than declared ===")
    import moat as MO

    d_good, comps_good, avail_good = MO.durability(good, "consumer", 0.04)
    failures += not check("durability scores a healthy business on all 100 points",
                          avail_good == 100, f"{avail_good:.0f}/100 available")
    failures += not check("...and the score is inside the scale",
                          0 <= d_good <= 100, f"{d_good:.1f}")

    # The trend signal is the reason this is not just a restatement of the
    # Tier 4 quality score: a company earning the same median return with an
    # eroding margin is a weaker franchise, and must price accordingly.
    eroding = secdata.extract(make_companyfacts(94, "Eroding Co", margin_decay=0.93))
    d_erode, _, _ = MO.durability(eroding, "consumer", 0.04)
    failures += not check("an eroding franchise scores below a stable one",
                          d_erode < d_good, f"eroding {d_erode:.1f} vs stable {d_good:.1f}")
    failures += not check("...and therefore demands a wider margin of safety",
                          MO.margin_of_safety(d_erode) > MO.margin_of_safety(d_good),
                          f"{MO.margin_of_safety(d_erode):.2f} vs {MO.margin_of_safety(d_good):.2f}")

    # Calibration: the change must not silently re-price the middle of the
    # distribution. A score of 50 has to land on the old 'narrow' default.
    failures += not check("a score of 50 reproduces the old 40% narrow default",
                          abs(MO.margin_of_safety(50.0) - 0.40) < 1e-9,
                          f"{MO.margin_of_safety(50.0):.4f}")
    failures += not check("a perfect score reaches the 30% floor and no further",
                          abs(MO.margin_of_safety(100.0) - 0.30) < 1e-9)
    failures += not check("a zero score reaches the 50% ceiling",
                          abs(MO.margin_of_safety(0.0) - 0.50) < 1e-9)
    failures += not check("margin of safety falls monotonically as durability rises",
                          all(MO.margin_of_safety(a) > MO.margin_of_safety(b)
                              for a, b in zip(range(0, 91, 10), range(10, 101, 10))))

    # Thin coverage must widen the margin, not sit neutral.
    failures += not check("thin coverage widens the margin of safety",
                          MO.margin_of_safety(90.0, available=30) > MO.margin_of_safety(90.0, 100),
                          f"{MO.margin_of_safety(90.0, 30):.2f} vs {MO.margin_of_safety(90.0, 100):.2f}")
    failures += not check("thin coverage forces the label to 'uncertain'",
                          MO.label(95.0, available=30) == "uncertain")
    failures += not check("thin coverage withholds the hurdle's quality credit",
                          not MO.grants_quality_credit(95.0, available=30))
    failures += not check("a high score on full coverage does grant the credit",
                          MO.grants_quality_credit(95.0, available=100))
    # Read the thresholds from config rather than hardcoding them, so a
    # recalibration cannot silently invalidate the test.
    W, N = config.DURABILITY_WIDE_MIN, config.DURABILITY_NARROW_MIN
    failures += not check("labels follow the configured thresholds",
                          MO.label(W) == "wide" and MO.label(W - 1) == "narrow"
                          and MO.label(N) == "narrow" and MO.label(N - 1) == "uncertain",
                          f"wide>={W}, narrow>={N}")
    failures += not check("the wide bar is strict enough to mean something",
                          W >= 75, f"DURABILITY_WIDE_MIN={W}")

    # The benchmark must not be built from a handful of names.
    thin = MO.sector_medians([("consumer", 0.05)] * 5)
    failures += not check("a sector with too few companies yields no benchmark",
                          thin == {}, f"{thin}")
    wide_bench = MO.sector_medians([("consumer", 0.02)] * 15 + [("consumer", 0.06)] * 15)
    failures += not check("a populated sector yields its median growth rate",
                          abs(wide_bench["consumer"] - 0.04) < 1e-9, f"{wide_bench}")

    # And the share-gain signal must actually respond to the benchmark.
    hi, _, _ = MO.durability(good, "consumer", 0.01)   # company beats a slow sector
    lo, _, _ = MO.durability(good, "consumer", 0.20)   # same company, fast sector
    failures += not check("beating the sector scores above lagging it",
                          hi > lo, f"{hi:.1f} vs {lo:.1f}")

    print(f"\n{'='*60}")
    print("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    print(f"{'='*60}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
