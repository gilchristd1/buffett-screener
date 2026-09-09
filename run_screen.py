#!/usr/bin/env python3
"""
Run the screen.

    python3 run_screen.py --download          # SEC bulk companyfacts
    python3 run_screen.py --build-universe    # eligible filers, from the zip, no network
    python3 run_screen.py --screen            # run the gates
    python3 run_screen.py --size-filter       # market cap + liquidity on the survivors
    python3 run_screen.py --explain AAPL               # why did one name pass or fail?

    python3 run_screen.py --build-universe   # fallback: every SEC ticker via yfinance (slow)
    python3 run_screen.py --download         # only needed for a full-universe run (~1.5GB)

Outputs land in out/:
    universe.csv    the eligible universe with market cap and sector
    results.csv     every company, every gate, pass/fail with reasons
    survivors.csv   names clearing all gates

Requires network access to sec.gov and a price source. If your network blocks
either, nothing else in this package will work — check that first.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import zipfile
from pathlib import Path

import config
import gates
import secdata

OUT = Path("out")
DATA = Path("data")

# yfinance sector -> screening module (see SECTOR_MODULES in config.py)
SECTOR_TO_MODULE = {
    "Technology": "software",
    "Communication Services": "software",
    "Consumer Cyclical": "consumer",
    "Consumer Defensive": "consumer",
    "Industrials": "industrials",
    "Basic Materials": "industrials",
    "Financial Services": "financials",
    "Healthcare": "healthcare",
    "Energy": "energy",
    "Utilities": "utilities",
    "Real Estate": "reit",
}


def universe_from_screener(path: str) -> None:
    """
    Build the universe from a web-screener export (Finviz, stockanalysis, TradingView).

    Preferred over --build-universe: a screener does the top of the funnel in one
    export, cutting ~1,200 candidates to ~150-250, so the SEC step runs on a
    shortlist via the per-CIK API in about thirty seconds and the 1.5GB bulk
    download is never needed. Configure the screener with the Tier 1 filters plus
    crude quality proxies (ROIC TTM, net debt/EBITDA, positive FCF) — never the
    gates themselves, which are all multi-year and not screenable on any free site.
    """
    import screener_import as SI

    rows, warnings = SI.read_export(path)
    for w in warnings:
        print(f"  warning: {w}")
    kept, counts = SI.apply_tier1(rows, config.MIN_MARKET_CAP, config.MIN_AVG_DAILY_VALUE)
    print(f"  {counts['total']:,} rows -> {counts['kept']:,} after Tier 1 "
          f"(too small {counts['too_small']}, illiquid {counts['illiquid']}, "
          f"no sector {counts['no_sector']}, no market cap {counts['no_market_cap']})")

    tickers = secdata.load_ticker_map()
    out, missing = [], []
    for r in kept:
        entry = tickers.get(r["ticker"]) or tickers.get(r["ticker"].replace(".", "-"))
        if not entry:
            missing.append(r["ticker"])
            continue
        out.append({
            "ticker": r["ticker"], "cik": entry["cik"],
            "name": r["name"] or entry["title"],
            "market_cap": int(r["market_cap"]), "adv": int(r["adv"] or 0),
            "yf_sector": r["screener_sector"], "module": r["module"],
        })
    if missing:
        print(f"  {len(missing)} tickers had no SEC CIK (often foreign filers): "
              f"{', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}")

    OUT.mkdir(exist_ok=True)
    if not out:
        sys.exit("No rows survived Tier 1 — check the screener export columns.")

    with open(OUT / "universe.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"universe: {len(out):,} names -> {OUT/'universe.csv'}")


def size_filter() -> None:
    """Apply market cap and liquidity to the gated survivors (see universe.py)."""
    import universe
    universe.size_filter(OUT)


def build_universe() -> None:
    """
    Full-universe build with no screener export. Delegates to universe.py, which
    uses two bulk sources instead of one HTTP call per company — see that module
    for why the naive approach cannot finish.
    """
    import universe
    universe.build(OUT, DATA / "companyfacts.zip", DATA / "submissions.zip")


def load_universe() -> list[dict]:
    path = OUT / "universe.csv"
    if not path.exists():
        sys.exit("No universe.csv — run --from-screener CSV (or --download then --build-universe) first")
    with open(path) as fh:
        return list(csv.DictReader(fh))


def _facts_for(cik: int, zf: zipfile.ZipFile | None) -> dict | None:
    if zf is not None:
        try:
            return json.loads(zf.read(f"CIK{cik:010d}.json"))
        except KeyError:
            return None
    return secdata.facts_from_api(cik)


def screen() -> None:
    universe = load_universe()
    zip_path = DATA / "companyfacts.zip"
    zf = zipfile.ZipFile(zip_path) if zip_path.exists() else None
    if zf is None:
        print("No bulk zip found — falling back to the per-CIK API (slow).")

    OUT.mkdir(exist_ok=True)
    results, survivors = [], []
    for n, row in enumerate(universe, 1):
        print(f"  screening {n:,}/{len(universe):,}", end="\r")
        facts = _facts_for(int(row["cik"]), zf)
        if not facts:
            continue
        series = secdata.extract(facts)
        res = gates.run_gates(series, row["ticker"], row["module"])
        passed = res.passed()
        for g in res.gates:
            results.append({
                "ticker": row["ticker"], "name": row["name"], "module": row["module"],
                "gate": g.code, "passed": g.passed, "value": g.value, "reason": g.reason,
            })
        if passed:
            survivors.append({
                "ticker": row["ticker"], "name": row["name"], "module": row["module"],
                "shares": row.get("shares", ""), "market_cap": row.get("market_cap", ""),
                "adv": row.get("adv", ""),
                "excused": ",".join(g.code for g in res.failures),
            })

    if not results:
        sys.exit("No companies were evaluated. Either universe.csv is empty or no "
                 "SEC facts could be read for any of them.")

    with open(OUT / "results.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)
    if survivors:
        with open(OUT / "survivors.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(survivors[0]))
            w.writeheader()
            w.writerows(survivors)

    print(f"\n{len(universe):,} screened -> {len(survivors):,} survivors")
    print(f"  detail:    {OUT/'results.csv'}")
    print(f"  survivors: {OUT/'survivors.csv'}")

    # Which gate is doing the most rejecting? Tells you where to tune.
    from collections import Counter
    rejects = Counter(r["gate"] for r in results if r["passed"] is False)
    print("\nrejections by gate:")
    for gate, count in rejects.most_common():
        print(f"  {gate:<10} {count:,}")


def explain(ticker: str) -> None:
    universe = {r["ticker"]: r for r in load_universe()}
    row = universe.get(ticker.upper())
    if not row:
        sys.exit(f"{ticker} is not in the universe (size, liquidity or sector filter)")
    zip_path = DATA / "companyfacts.zip"
    zf = zipfile.ZipFile(zip_path) if zip_path.exists() else None
    facts = _facts_for(int(row["cik"]), zf)
    if not facts:
        sys.exit(f"No SEC facts for CIK {row['cik']}")
    series = secdata.extract(facts)
    res = gates.run_gates(series, row["ticker"], row["module"])
    print(f"\n{row['name']} ({ticker.upper()})  module: {row['module']}")
    print(f"fiscal years available: {series.years()}\n")
    for g in res.gates:
        mark = {True: "PASS", False: "FAIL", None: "  ??"}[g.passed]
        print(f"  [{mark}] {g.code:<10} {g.reason}")
    print(f"\noverall: {'PASS' if res.passed() else 'FAIL'}\n")


def main() -> None:
    p = argparse.ArgumentParser(description="Buffett-style stock screen")
    p.add_argument("--from-screener", metavar="CSV",
                   help="build the universe from a screener export (recommended)")
    p.add_argument("--build-universe", action="store_true",
                   help="build the eligible universe from the SEC bulk file (no network)")
    p.add_argument("--download", action="store_true")
    p.add_argument("--screen", action="store_true")
    p.add_argument("--size-filter", action="store_true",
                   help="apply market cap and liquidity to the gated survivors")
    p.add_argument("--explain", metavar="TICKER")
    a = p.parse_args()

    if a.download:
        print("Downloading SEC bulk companyfacts...")
        print("->", secdata.download_bulk_companyfacts())
        print("Downloading SEC bulk submissions (sector and exchange)...")
        print("->", secdata.download_bulk_submissions())
    if a.from_screener:
        universe_from_screener(a.from_screener)
    if a.build_universe:
        build_universe()
    if a.screen:
        screen()
    if a.size_filter:
        size_filter()
    if a.explain:
        explain(a.explain)
    if not any([a.download, a.from_screener, a.build_universe, a.screen,
                a.size_filter, a.explain]):
        p.print_help()


if __name__ == "__main__":
    main()
