"""
Import a universe from a web screener export (Finviz, stockanalysis.com, TradingView).

Why this exists: a web screener does the top of the funnel far more cheaply than
we can. Configure market cap, liquidity, exchange and a couple of crude quality
proxies once, export the CSV, and ~1,200 candidates become ~150-250. The SEC
pipeline then runs on that shortlist via the per-CIK API in about thirty seconds,
which removes the need for the 1.5GB bulk download entirely.

What a web screener must NOT be trusted to do is the gates themselves. Every
public screener filters on trailing-twelve-month or latest-year snapshots. The
gates that make this a Buffett screen rather than a generic quality factor screen
— ROIC positive in every one of ten years, incremental ROIC on retained earnings,
peak-to-peak growth for cyclicals, R&D-capitalised ROIC, leverage at trough
EBITDA — are all multi-year and none of them are screenable on any free site.
Use the screener to narrow, never to decide.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

# Column headers vary by provider. Match case-insensitively on these aliases.
COLUMN_ALIASES = {
    "ticker": ["symbol", "ticker", "code"],
    "name": ["company", "name", "company name", "description"],
    "market_cap": ["market cap", "marketcap", "market capitalization", "mktcap"],
    "sector": ["sector", "gics sector"],
    "industry": ["industry", "gics industry"],
    "price": ["price", "last", "close", "share price"],
    "volume": ["volume", "avg volume", "average volume", "vol"],
}

# Sector labels differ between providers; normalise to our module names.
SECTOR_TO_MODULE = {
    # Finviz / stockanalysis / yfinance
    "technology": "software",
    "communication services": "software",
    "communications": "software",
    "consumer cyclical": "consumer",
    "consumer defensive": "consumer",
    "consumer discretionary": "consumer",
    "consumer staples": "consumer",
    "industrials": "industrials",
    "basic materials": "industrials",
    "materials": "industrials",
    "financial": "financials",
    "financials": "financials",
    "financial services": "financials",
    "healthcare": "healthcare",
    "health care": "healthcare",
    "energy": "energy",
    "utilities": "utilities",
    "real estate": "reit",
}

_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def parse_number(raw: str | None) -> float | None:
    """
    Screener exports write numbers in whatever way suits their UI:
    '412.50B', '$1,234,567', '12.3%', '-', ''. Normalise all of it.
    """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("$", "").replace("%", "")
    if not s or s in {"-", "--", "N/A", "n/a", "NULL"}:
        return None
    m = re.fullmatch(r"(-?\d*\.?\d+)\s*([KMBTkmbt])?", s)
    if not m:
        return None
    value = float(m.group(1))
    suffix = m.group(2)
    return value * _SUFFIX[suffix.lower()] if suffix else value


def _resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    """Map our canonical field names to whatever this export calls them."""
    lookup = {f.strip().lower(): f for f in fieldnames}
    resolved: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                resolved[canonical] = lookup[alias]
                break
    return resolved


def read_export(path: str | Path) -> tuple[list[dict], list[str]]:
    """
    Parse a screener CSV into normalised rows.

    Returns (rows, warnings). Warnings name what could not be resolved — a
    silently-dropped column would quietly shrink the universe, so they surface.
    """
    path = Path(path)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return [], [f"{path.name}: no header row"]
        cols = _resolve_columns(list(reader.fieldnames))
        raw_rows = list(reader)

    warnings: list[str] = []
    for required in ("ticker", "market_cap"):
        if required not in cols:
            warnings.append(
                f"no '{required}' column found — headers were: {reader.fieldnames}"
            )
    if "sector" not in cols:
        warnings.append("no 'sector' column — every row will need a sector assigned by hand")

    rows, unmapped_sectors = [], set()
    for r in raw_rows:
        ticker = (r.get(cols.get("ticker", ""), "") or "").strip().upper()
        if not ticker:
            continue
        sector_raw = (r.get(cols.get("sector", ""), "") or "").strip()
        module = SECTOR_TO_MODULE.get(sector_raw.lower())
        if sector_raw and not module:
            unmapped_sectors.add(sector_raw)
        price = parse_number(r.get(cols.get("price", "")))
        volume = parse_number(r.get(cols.get("volume", "")))
        rows.append({
            "ticker": ticker,
            "name": (r.get(cols.get("name", ""), "") or "").strip(),
            "market_cap": parse_number(r.get(cols.get("market_cap", ""))),
            "adv": (price * volume) if (price and volume) else None,
            "screener_sector": sector_raw,
            "module": module,
        })

    if unmapped_sectors:
        warnings.append(f"unmapped sectors (add to SECTOR_TO_MODULE): {sorted(unmapped_sectors)}")
    return rows, warnings


def apply_tier1(rows: list[dict], min_market_cap: float, min_adv: float) -> tuple[list[dict], dict]:
    """
    Tier 1 filters, applied locally so the result doesn't depend on how the
    screener's own filters were configured. ADV is skipped where the export
    lacks price or volume rather than rejecting the row on missing data.
    """
    kept, counts = [], {"total": len(rows), "no_market_cap": 0, "too_small": 0,
                        "illiquid": 0, "no_sector": 0, "kept": 0}
    for r in rows:
        if r["market_cap"] is None:
            counts["no_market_cap"] += 1
            continue
        if r["market_cap"] < min_market_cap:
            counts["too_small"] += 1
            continue
        if r["adv"] is not None and r["adv"] < min_adv:
            counts["illiquid"] += 1
            continue
        if not r["module"]:
            counts["no_sector"] += 1
            continue
        kept.append(r)
    counts["kept"] = len(kept)
    return kept, counts
