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


def rank() -> None:
    """
    Tier 4 + Tier 5: score the survivors, value them, and write the ranked queue.

    Runs after --size-filter, because the valuation lenses need a market cap.
    Produces out/ranked.csv — the research queue, ordered — and out/watchlist.csv
    for names that clear on quality but not yet on price.
    """
    import zipfile
    import metrics as M
    import rank as R

    src = OUT / "survivors.csv"
    if not src.exists():
        sys.exit("No survivors.csv — run --screen first.")
    with open(src) as fh:
        survivors = list(csv.DictReader(fh))
    if not survivors:
        sys.exit("survivors.csv is empty — nothing to rank.")

    moats = R.load_moat_classifications(Path("moats.csv"))
    if not moats:
        print("  no moats.csv — every company treated as 'narrow', so none receives")
        print("  the quality credit on the hurdle or the 30% margin of safety.")

    zip_path = DATA / "companyfacts.zip"
    zf = zipfile.ZipFile(zip_path) if zip_path.exists() else None
    universe = {r["ticker"]: r for r in load_universe()}

    rows, watch = [], []
    for r in survivors:
        t_ = r["ticker"]
        facts = _facts_for(int(universe[t_]["cik"]), zf) if t_ in universe else None
        if not facts:
            continue
        s = secdata.extract(facts)
        module = r.get("module", "")
        mcap = float(r["market_cap"]) if r.get("market_cap") else None
        shares = float(r["shares"]) if r.get("shares") else None
        moat = moats.get(t_, "narrow")

        oey = R.owner_earnings_yield(s, mcap) if mcap else None
        iv = R.dcf_intrinsic_value(s)
        mvh = R.multiple_vs_history(s, mcap) if mcap else None
        bp = R.buy_price(iv, shares, moat) if (iv and shares) else None
        price = (mcap / shares) if (mcap and shares) else None
        disc = (1 - price / (iv / shares)) if (iv and shares and price) else None

        comps = R.score_components(s, module, oey, disc, mvh)
        score, earned, avail = R.total_score(comps)

        # §7 Lens A: the hurdle this company's owner-earnings yield must clear,
        # which falls as quality and reinvestment runway rise.
        hurdle = None
        if oey is not None:
            import statistics as st
            cap_rnd = config.SECTOR_MODULES.get(module, {}).get("capitalise_rnd", False)
            rs = [x for x in (M.roic(s, y, cap_rnd)
                              for y in M.common_years(s, ["operating_income", "total_equity"], 10))
                  if x is not None]
            hurdle = M.required_owner_earnings_yield(
                st.median(rs) if rs else None, M.classify_reinvestment(s), moat)

        row = {
            "ticker": t_, "name": r["name"], "module": module, "moat": moat,
            "score": round(score, 1), "points": f"{earned:.0f}/{avail:.0f}",
            "owner_earnings_yield": f"{oey:.4f}" if oey is not None else "",
            "hurdle": f"{hurdle:.4f}" if hurdle is not None else "",
            "clears_hurdle": ("yes" if (oey is not None and hurdle is not None and oey >= hurdle)
                              else "no" if oey is not None else ""),
            "price": f"{price:.2f}" if price else "",
            "buy_price": f"{bp:.2f}" if bp else "",
            "discount_to_iv": f"{disc:.3f}" if disc is not None else "",
            "ev_ebit_vs_history_sd": f"{mvh:.2f}" if mvh is not None else "",
            "market_cap": r.get("market_cap", ""),
        }
        rows.append(row)
        if bp and price and price > bp:
            watch.append(row)

    rows.sort(key=lambda x: -x["score"])
    OUT.mkdir(exist_ok=True)
    with open(OUT / "ranked.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    if watch:
        with open(OUT / "watchlist.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(watch[0]))
            w.writeheader(); w.writerows(watch)

    buyable = [r for r in rows if r["clears_hurdle"] == "yes"]
    print(f"\nranked {len(rows)} survivors -> {OUT/'ranked.csv'}")
    print(f"  clearing the owner-earnings hurdle today: {len(buyable)}")
    print(f"  on the watchlist (quality yes, price no):  {len(watch)}")
    print(f"\n  {'rank':<5}{'ticker':<8}{'score':>6}  {'pts':<8}{'yield':>7}{'hurdle':>8}  name")
    for i, r in enumerate(rows[:12], 1):
        y = f"{float(r['owner_earnings_yield']):.1%}" if r["owner_earnings_yield"] else "  -"
        h = f"{float(r['hurdle']):.1%}" if r["hurdle"] else "  -"
        print(f"  {i:<5}{r['ticker']:<8}{r['score']:>6.1f}  {r['points']:<8}{y:>7}{h:>8}  {r['name'][:34]}")
    print(f"\n  {R.MANUAL_POINTS} of 100 points need a human: "
          + ", ".join(R.MANUAL_COMPONENTS))


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
    p.add_argument("--rank", action="store_true",
                   help="score and value the survivors (Tiers 4 and 5)")
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
    if a.rank:
        rank()
    if a.explain:
        explain(a.explain)
    if not any([a.download, a.from_screener, a.build_universe, a.screen,
                a.size_filter, a.rank, a.explain]):
        p.print_help()


if __name__ == "__main__":
    main()
