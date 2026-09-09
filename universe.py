"""
Build the eligible universe from SEC data alone.

Design note — why there is no price provider in this step any more.

The first version fetched prices for every SEC filer to compute market cap, so
the size filter could be applied before the gates. That put a third-party price
API on the critical path for ~8,000 companies, and price APIs are exactly what
fails from a CI runner: Yahoo and most free endpoints throttle or block
datacenter IP ranges, and a partial failure silently produces an empty universe.

Market cap and liquidity are only an *eligibility* filter. They decide nothing
about business quality. So the order is inverted:

    1. universe   = every SEC filer with enough filing history   (zip, no network)
    2. gates      = the quality tests                            (zip, no network)
    3. size filter= market cap and liquidity on the survivors    (~dozens of lookups)

Nothing on the critical path needs the network, and prices are fetched for a few
dozen names instead of thousands. If the price step fails, the run still produces
a gated shortlist — it just isn't size-filtered yet, which is recoverable.
"""

from __future__ import annotations

import csv
import json
import sys
import zipfile
from pathlib import Path

import config
import secdata

SHARES_TAGS = ["EntityCommonStockSharesOutstanding"]
PRICE_BATCH = 100


def shares_outstanding(facts: dict) -> float | None:
    """
    Latest reported share count, from the dei taxonomy rather than us-gaap.

    dei carries the cover-page figure: filed by everyone, and closer to today
    than the weighted-average count in the income statement.
    """
    dei = facts.get("facts", {}).get("dei", {})
    best_date, best_val = "", None
    for tag in SHARES_TAGS:
        node = dei.get(tag)
        if not node:
            continue
        for unit_vals in node.get("units", {}).values():
            for item in unit_vals:
                end, val = item.get("end", ""), item.get("val")
                if val and end > best_date:
                    best_date, best_val = end, float(val)
    return best_val


def sic_to_module(sic: str | None) -> str | None:
    """Sector module from the SEC's own SIC code. Coarse, but present for every filer."""
    if not sic or not str(sic).isdigit():
        return None
    n = int(sic)
    # Real estate before financials: 6798 (REITs) sits inside the 6700-6799
    # holding-company range, so testing financials first misclassifies every REIT.
    if 6500 <= n <= 6599 or n == 6798:
        return "reit"
    if 6000 <= n <= 6499 or 6700 <= n <= 6799:
        return "financials"
    if 4900 <= n <= 4949:
        return "utilities"
    if 1200 <= n <= 1399 or 2900 <= n <= 2999:
        return "energy"
    if 7370 <= n <= 7379 or 3570 <= n <= 3579 or 3670 <= n <= 3679:
        return "software"
    if 8000 <= n <= 8099 or 2830 <= n <= 2836 or 3840 <= n <= 3851:
        return "healthcare"
    if 5200 <= n <= 5999 or 2000 <= n <= 2199 or 5800 <= n <= 5899:
        return "consumer"
    if 1000 <= n <= 3999 or 4000 <= n <= 4799 or 5000 <= n <= 5199:
        return "industrials"
    return None


def excluded_by_sic(sic: str | None) -> str | None:
    """Tier 1 exclusions that are visible from the SIC code alone."""
    if not sic or not str(sic).isdigit():
        return None
    n = int(sic)
    if n == 2836 or n == 8731:
        return "clinical-stage biotech / research"
    if n == 6770:
        return "blank check / SPAC"
    if n == 6726:
        return "closed-end fund / investment office"
    return None


def build(out_dir: Path, zip_path: Path) -> int:
    """Every SEC filer with a ticker and enough filing history. No network."""
    if not zip_path.exists():
        sys.exit(f"{zip_path} not found — run --download first.")

    tickers = secdata.load_ticker_map()
    print(f"{len(tickers):,} SEC-registered tickers")

    rows = []
    skipped = {"no_facts": 0, "no_shares": 0, "no_sector": 0,
               "excluded_sic": 0, "short_history": 0, "unreadable": 0}

    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        print(f"  bulk file holds {len(names):,} company records")
        for n, (ticker, meta) in enumerate(sorted(tickers.items()), 1):
            if n % 1000 == 0:
                print(f"  reading {n:,}/{len(tickers):,}", flush=True)
            fname = f"CIK{meta['cik']:010d}.json"
            if fname not in names:
                skipped["no_facts"] += 1
                continue
            try:
                facts = json.loads(z.read(fname))
            except Exception:
                skipped["unreadable"] += 1
                continue

            sic = str(facts.get("sic") or "")
            if excluded_by_sic(sic):
                skipped["excluded_sic"] += 1
                continue
            module = sic_to_module(sic)
            if not module:
                skipped["no_sector"] += 1
                continue

            # Enough history to test? Cheap check before the gates do real work.
            series = secdata.extract(facts)
            years = series.series("net_income")
            need = (config.MIN_YEARS_HISTORY_CYCLICAL
                    if module in ("industrials", "energy") else config.MIN_YEARS_HISTORY)
            if len(years) < min(need, config.MIN_YEARS_HISTORY):
                skipped["short_history"] += 1
                continue

            sh = shares_outstanding(facts)
            if not sh:
                skipped["no_shares"] += 1

            rows.append({
                "ticker": ticker, "cik": meta["cik"], "name": meta["title"],
                "shares": int(sh) if sh else 0,
                "sic": sic, "sic_desc": facts.get("sicDescription", ""),
                "module": module,
                "market_cap": "", "adv": "",     # filled by the size filter, post-gates
            })

    if not rows:
        sys.exit("Universe is empty. Check the bulk zip downloaded correctly "
                 f"({zip_path}, {zip_path.stat().st_size:,} bytes).")

    out_dir.mkdir(exist_ok=True)
    with open(out_dir / "universe.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"\nuniverse: {len(rows):,} names -> {out_dir/'universe.csv'}")
    print("  excluded: " + ", ".join(f"{k} {v:,}" for k, v in skipped.items()))
    return len(rows)


# ------------------------------------------------------------ size filter --
def fetch_prices(tickers: list[str]) -> dict[str, tuple[float, float]]:
    """ticker -> (last close, average daily volume). Small list, so failures are visible."""
    try:
        import yfinance as yf
    except ImportError:
        print("  yfinance not installed — skipping the size filter")
        return {}

    out: dict[str, tuple[float, float]] = {}
    for i in range(0, len(tickers), PRICE_BATCH):
        batch = tickers[i:i + PRICE_BATCH]
        try:
            df = yf.download(batch, period="3mo", interval="1d", group_by="ticker",
                             auto_adjust=False, progress=False, threads=True)
        except Exception as exc:
            print(f"  price batch {i} failed: {type(exc).__name__}: {exc}")
            continue
        for t in batch:
            try:
                sub = df[t] if len(batch) > 1 else df
                close, vol = sub["Close"].dropna(), sub["Volume"].dropna()
                if close.empty or vol.empty:
                    continue
                out[t] = (float(close.iloc[-1]), float(vol.mean()))
            except Exception:
                continue
    return out


def size_filter(out_dir: Path) -> None:
    """
    Apply market cap and liquidity to the gated survivors only.

    Deliberately non-fatal: if prices can't be fetched, the survivor list stands
    unfiltered with a warning rather than the run failing. A shortlist you have
    to size-check by hand beats no shortlist at all.
    """
    src = out_dir / "survivors.csv"
    if not src.exists():
        print("No survivors.csv — nothing to size-filter.")
        return
    with open(src) as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("survivors.csv is empty — nothing to size-filter.")
        return

    print(f"Size-filtering {len(rows)} survivors...")
    prices = fetch_prices([r["ticker"] for r in rows])
    if not prices:
        print("  WARNING: no prices retrieved — leaving the survivor list unfiltered.")
        print("  Market cap and liquidity have NOT been applied; check by hand.")
        return

    kept, dropped = [], {"no_price": 0, "no_shares": 0, "too_small": 0, "illiquid": 0}
    for r in rows:
        px = prices.get(r["ticker"])
        if not px:
            dropped["no_price"] += 1
            kept.append(r)          # keep rather than silently lose a good name
            continue
        price, volume = px
        shares = float(r.get("shares") or 0)
        adv = price * volume
        r["adv"] = int(adv)
        if not shares:
            dropped["no_shares"] += 1
            kept.append(r)
            continue
        mcap = shares * price
        r["market_cap"] = int(mcap)
        if mcap < config.MIN_MARKET_CAP:
            dropped["too_small"] += 1
            continue
        if adv < config.MIN_AVG_DAILY_VALUE:
            dropped["illiquid"] += 1
            continue
        kept.append(r)

    with open(out_dir / "survivors.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(kept)
    print(f"  {len(rows)} -> {len(kept)} after size and liquidity")
    print("  dropped: " + ", ".join(f"{k} {v}" for k, v in dropped.items()))
