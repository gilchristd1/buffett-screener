#!/usr/bin/env python3
"""
Probe: could EdgarTools replace the hand-written XBRL parser in secdata.py?

WHY THIS IS A PROBE AND NOT THE MIGRATION
-----------------------------------------
sec.gov is unreachable from both environments where this code is written, so
EdgarTools cannot be run, tested or even imported outside the GitHub runner.
Writing a full replacement parser against an API that cannot be executed once
before it ships is exactly how the first four runs went wrong.

So this measures first. It answers three questions with data, on the runner,
non-fatally, without touching the screen's output:

  1. Does it work here at all, and through which access path?
  2. How long does it take per company? The current parser reads a bulk zip
     already on disk. If EdgarTools needs one network call per company, 2,150
     calls at the SEC's rate limit is minutes, not seconds, and every one is a
     chance for a 403 mid-run.
  3. Where do the two parsers disagree on the SAME company? Every disagreement
     is a bug in one of them. Given the fiscal-year, tag-variant and quarterly
     traps already found in mine, the prior should be that most are mine.

Output: out/parser_probe.csv, one row per company per field, plus a summary
printed to the log. Nothing downstream reads it.
"""

from __future__ import annotations

import csv
import json
import sys
import time
import zipfile
from pathlib import Path

import config
import secdata

OUT = Path("out")
DATA = Path("data")

# Fields worth comparing: the ones the gates actually depend on.
COMPARE = ["revenue", "operating_income", "net_income", "total_assets",
           "total_equity", "operating_cash_flow", "capex",
           "depreciation_amortisation", "diluted_shares"]

SAMPLE = 30          # enough to be informative, small enough to stay cheap
TOLERANCE = 0.005    # 0.5% — rounding and restatement noise, not a disagreement


def _probe_import():
    """Report exactly how the library is reachable, rather than assuming."""
    try:
        import edgar  # noqa: F401
    except ImportError as exc:
        return None, f"import failed: {exc}"
    try:
        from edgar import set_identity
        set_identity(config.require_user_agent())
    except Exception as exc:
        return None, f"set_identity failed: {type(exc).__name__}: {exc}"
    import edgar
    return edgar, f"edgar {getattr(edgar, '__version__', 'unknown version')}"


def _facts_via_edgar(edgar, cik: int):
    """
    Try each plausible access path in turn and report which one worked.

    The documentation covers Company(...).get_facts() and an offline local-store
    mode but does not settle which is available in a given version, so the probe
    tries rather than assumes.
    """
    attempts = []
    for label, fn in (
        ("Company(cik).get_facts()", lambda: edgar.Company(cik).get_facts()),
        ("Company(cik).facts", lambda: edgar.Company(cik).facts),
    ):
        try:
            got = fn()
            if got is not None:
                return got, label, attempts
            attempts.append(f"{label}: returned None")
        except Exception as exc:
            attempts.append(f"{label}: {type(exc).__name__}: {exc}")
    return None, None, attempts


def _series_from_edgar(facts_obj) -> dict[str, dict[int, float]]:
    """
    Pull the comparable fields out of whatever EdgarTools returns.

    Deliberately defensive: this runs against an API that has never executed in
    the environment where it was written. Anything unexpected is recorded as a
    gap, never raised — a probe that crashes tells you nothing.
    """
    out: dict[str, dict[int, float]] = {}
    concept_for = {
        "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"],
        "operating_income": ["OperatingIncomeLoss"],
        "net_income": ["NetIncomeLoss"],
        "total_assets": ["Assets"],
        "total_equity": ["StockholdersEquity"],
        "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
        "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
        "depreciation_amortisation": ["DepreciationDepletionAndAmortization"],
        "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    }
    for field, concepts in concept_for.items():
        for concept in concepts:
            try:
                df = facts_obj.query().by_concept(concept).to_dataframe()
            except Exception:
                continue
            if df is None or len(df) == 0:
                continue
            got: dict[int, float] = {}
            cols = {c.lower(): c for c in df.columns}
            endcol = cols.get("period_end") or cols.get("end") or cols.get("date")
            valcol = cols.get("value") or cols.get("val") or cols.get("numeric_value")
            formcol = cols.get("form")
            if not endcol or not valcol:
                continue
            for _, row in df.iterrows():
                try:
                    if formcol and str(row[formcol]) != "10-K":
                        continue
                    end = str(row[endcol])[:10]
                    fy = secdata._fiscal_year_of(end)
                    v = row[valcol]
                    if fy is None or v is None:
                        continue
                    got[fy] = float(v)
                except Exception:
                    continue
            if got:
                out[field] = got
                break
    return out


def main() -> int:
    OUT.mkdir(exist_ok=True)
    edgar, note = _probe_import()
    print(f"EdgarTools: {note}")
    if edgar is None:
        print("  probe stops here — nothing else can be measured")
        return 0

    src = OUT / "universe.csv"
    if not src.exists():
        print("  no out/universe.csv — run --build-universe first")
        return 0
    universe = list(csv.DictReader(open(src)))[:SAMPLE]

    zpath = DATA / "companyfacts.zip"
    zf = zipfile.ZipFile(zpath) if zpath.exists() else None
    if zf is None:
        print("  no companyfacts.zip — cannot compare against the current parser")
        return 0

    rows, timings, path_used = [], [], None
    agree = differ = missing_mine = missing_theirs = 0

    for r in universe:
        cik, tkr = int(r["cik"]), r["ticker"]
        try:
            mine = secdata.extract(json.loads(zf.read(f"CIK{cik:010d}.json")))
        except KeyError:
            continue

        t0 = time.time()
        facts, path, attempts = _facts_via_edgar(edgar, cik)
        timings.append(time.time() - t0)
        if facts is None:
            print(f"  {tkr}: no access path worked — {'; '.join(attempts[:2])}")
            continue
        path_used = path_used or path
        theirs = _series_from_edgar(facts)

        for fieldname in COMPARE:
            a, b = mine.series(fieldname), theirs.get(fieldname, {})
            for fy in sorted(set(a) | set(b)):
                va, vb = a.get(fy), b.get(fy)
                if va is None and vb is None:
                    continue
                if va is None:
                    verdict, missing_mine = "only EdgarTools", missing_mine + 1
                elif vb is None:
                    verdict, missing_theirs = "only current parser", missing_theirs + 1
                else:
                    denom = max(abs(va), abs(vb), 1.0)
                    if abs(va - vb) / denom <= TOLERANCE:
                        verdict, agree = "agree", agree + 1
                        continue          # not worth a row
                    verdict, differ = "DISAGREE", differ + 1
                rows.append({"ticker": tkr, "field": fieldname, "fiscal_year": fy,
                             "current_parser": va, "edgartools": vb,
                             "verdict": verdict})

    with open(OUT / "parser_probe.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "field", "fiscal_year",
                                           "current_parser", "edgartools", "verdict"])
        w.writeheader()
        w.writerows(rows)

    n = len(timings) or 1
    per = sum(timings) / n
    print(f"\n  access path:        {path_used}")
    print(f"  companies probed:   {len(timings)}")
    print(f"  seconds per company:{per:6.2f}  "
          f"-> {per * 2150 / 60:.0f} min for the full 2,150-name universe")
    print(f"  values agreeing:    {agree:,}")
    print(f"  values DISAGREEING: {differ:,}")
    print(f"  only EdgarTools has:{missing_mine:,}   only current parser has: {missing_theirs:,}")
    print(f"\n  detail -> {OUT / 'parser_probe.csv'}")
    if differ or missing_mine:
        print("  Every disagreement is a bug in one parser or the other. Read them "
              "before swapping anything.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
