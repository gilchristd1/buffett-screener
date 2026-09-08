"""
Build the eligible universe without a screener export.

Why this exists as its own module: the obvious approach — walk every SEC ticker
and ask yfinance for `.info` — issues one HTTP request per company. Across ~8,000
filers that is hours of wall-clock and reliably rate-limited long before it
finishes. It looks fine on ten tickers and fails on the real list.

This does it in bulk instead:

  shares outstanding  <- SEC XBRL (dei:EntityCommonStockSharesOutstanding),
                         read from the bulk companyfacts zip already on disk
  price and volume    <- one batched yfinance download covering all tickers
  market cap          <- shares x price

Two bulk sources, no per-company calls. On a GitHub runner the 1.5GB bulk
download is a few minutes of fast network, which is why the zip is the right
input here even though it is the wrong input on a laptop.
"""

from __future__ import annotations

import csv
import json
import sys
import zipfile
from pathlib import Path

import config
import secdata

# Provider sector label -> screening module (see SECTOR_MODULES in config.py)
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

PRICE_BATCH = 300          # tickers per yfinance download call
SHARES_TAGS = ["EntityCommonStockSharesOutstanding"]


def shares_outstanding(facts: dict) -> float | None:
    """
    Latest reported share count, from the dei taxonomy rather than us-gaap.

    dei is where the cover-page figure lives; it is filed by everyone and is
    closer to today than the weighted-average count in the income statement.
    """
    dei = facts.get("facts", {}).get("dei", {})
    best_date, best_val = "", None
    for tag in SHARES_TAGS:
        node = dei.get(tag)
        if not node:
            continue
        for unit_vals in node.get("units", {}).values():
            for item in unit_vals:
                end = item.get("end", "")
                val = item.get("val")
                if val and end > best_date:
                    best_date, best_val = end, float(val)
    return best_val


def sic_to_module(sic: str | None) -> str | None:
    """
    Fallback sector classification from the SEC's own SIC code, used when the
    price provider returns no sector. Coarse, but it keeps a company in the
    funnel rather than dropping it for a missing label.
    """
    if not sic or not sic.isdigit():
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


def fetch_prices(tickers: list[str]) -> dict[str, tuple[float, float]]:
    """
    ticker -> (last close, average daily volume) from batched downloads.

    yf.download is genuinely bulk — one request per batch, not per ticker.
    """
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("pip install yfinance  (SEC data carries no prices)")

    out: dict[str, tuple[float, float]] = {}
    for i in range(0, len(tickers), PRICE_BATCH):
        batch = tickers[i:i + PRICE_BATCH]
        print(f"  prices {i:,}/{len(tickers):,}", end="\r", flush=True)
        try:
            df = yf.download(batch, period="3mo", interval="1d",
                             group_by="ticker", auto_adjust=False,
                             progress=False, threads=True)
        except Exception as exc:
            print(f"\n  batch {i} failed ({type(exc).__name__}) — continuing")
            continue
        for t in batch:
            try:
                sub = df[t] if len(batch) > 1 else df
                close = sub["Close"].dropna()
                vol = sub["Volume"].dropna()
                if close.empty or vol.empty:
                    continue
                out[t] = (float(close.iloc[-1]), float(vol.mean()))
            except Exception:
                continue
    print(f"  prices {len(tickers):,}/{len(tickers):,}")
    return out


def build(out_dir: Path, zip_path: Path) -> int:
    if not zip_path.exists():
        sys.exit(f"{zip_path} not found — run with --download first.\n"
                 "The bulk zip is what makes this approach fast; without it there\n"
                 "is no way to get share counts in bulk.")

    tickers = secdata.load_ticker_map()
    print(f"{len(tickers):,} SEC-registered tickers")

    prices = fetch_prices(sorted(tickers))
    print(f"  {len(prices):,} with usable price history")

    rows, skipped = [], {"no_price": 0, "no_shares": 0, "too_small": 0,
                         "illiquid": 0, "no_sector": 0, "no_facts": 0}

    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        for n, (ticker, meta) in enumerate(sorted(tickers.items()), 1):
            if n % 500 == 0:
                print(f"  screening universe {n:,}/{len(tickers):,}", end="\r", flush=True)
            px = prices.get(ticker)
            if not px:
                skipped["no_price"] += 1
                continue
            price, volume = px
            adv = price * volume
            if adv < config.MIN_AVG_DAILY_VALUE:
                skipped["illiquid"] += 1
                continue

            fname = f"CIK{meta['cik']:010d}.json"
            if fname not in names:
                skipped["no_facts"] += 1
                continue
            try:
                facts = json.loads(z.read(fname))
            except Exception:
                skipped["no_facts"] += 1
                continue

            sh = shares_outstanding(facts)
            if not sh:
                skipped["no_shares"] += 1
                continue
            mcap = sh * price
            if mcap < config.MIN_MARKET_CAP:
                skipped["too_small"] += 1
                continue

            module = sic_to_module(str(facts.get("sic") or ""))
            if not module:
                skipped["no_sector"] += 1
                continue

            rows.append({
                "ticker": ticker, "cik": meta["cik"], "name": meta["title"],
                "market_cap": int(mcap), "adv": int(adv),
                "yf_sector": facts.get("sicDescription", ""), "module": module,
            })

    out_dir.mkdir(exist_ok=True)
    with open(out_dir / "universe.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"\nuniverse: {len(rows):,} names -> {out_dir/'universe.csv'}")
    print("  excluded: " + ", ".join(f"{k} {v:,}" for k, v in skipped.items()))
    return len(rows)
