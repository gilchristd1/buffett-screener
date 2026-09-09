"""
SEC XBRL acquisition and tag normalisation.

Two jobs:
  1. Get companyfacts JSON (bulk zip preferred; per-CIK API as fallback).
  2. Collapse the mess of US-GAAP tag variants into canonical fields.

The tag map is the maintenance burden of the free-data route. Filers use
different tags for the same concept, and change them between years. Priority
order matters: the first tag with data for a given fiscal year wins.
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

import config

SEC_BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

CACHE = Path("data")


# --------------------------------------------------------------- tag map --
# Ordered by preference. First tag that yields a value for the year wins.
TAG_MAP: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "RevenuesNetOfInterestExpense",              # financials
    ],
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        "CostOfServices",
    ],
    "operating_income": [
        "OperatingIncomeLoss",
    ],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
    ],
    "tax_expense": [
        "IncomeTaxExpenseBenefit",
    ],
    "depreciation_amortisation": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
    ],
    "share_based_comp": [
        "ShareBasedCompensation",
        "AllocatedShareBasedCompensationExpense",
    ],
    "research_development": [
        "ResearchAndDevelopmentExpense",
    ],
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseDebt",
        "InterestExpenseNonoperating",
    ],
    # --- balance sheet (instant) ---
    "total_assets": ["Assets"],
    "total_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "short_term_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "current_debt": ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"],
    "goodwill": ["Goodwill"],
    "intangibles": [
        "IntangibleAssetsNetExcludingGoodwill",
        "FiniteLivedIntangibleAssetsNet",
    ],
    "deposits": ["Deposits"],
    "loans": ["LoansAndLeasesReceivableNetReportedAmount", "NotesReceivableNet"],
    # --- shares (flow-ish; reported per period) ---
    "diluted_shares": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
}

# Fields recorded at a point in time rather than over a period.
INSTANT_FIELDS = {
    "total_assets", "total_equity", "cash", "short_term_investments",
    "long_term_debt", "current_debt", "goodwill", "intangibles",
    "deposits", "loans",
}

SHARE_FIELDS = {"diluted_shares"}


# ------------------------------------------------------------- retrieval --
def _headers() -> dict:
    return {
        "User-Agent": config.require_user_agent(),
        "Accept-Encoding": "gzip, deflate",
    }


MIN_BULK_BYTES = 200_000_000   # a real companyfacts.zip is >1GB; anything near
                               # this is a redirect page or a truncated transfer


def download_bulk_companyfacts(dest: Path = CACHE / "companyfacts.zip") -> Path:
    """
    One bulk download instead of ~8,000 API calls. Once a quarter is plenty.

    The size check matters: a silent partial download produces a zip that opens
    but is missing most companies, and the failure then surfaces much later as
    an empty universe with no obvious cause.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > MIN_BULK_BYTES:
        print(f"  cached: {dest} ({dest.stat().st_size:,} bytes)")
        return dest
    with requests.get(SEC_BULK_URL, headers=_headers(), stream=True, timeout=1800) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    size = dest.stat().st_size
    print(f"  downloaded {size:,} bytes")
    if size < MIN_BULK_BYTES:
        raise SystemExit(
            f"companyfacts.zip is only {size:,} bytes — expected over "
            f"{MIN_BULK_BYTES:,}. The download was truncated or the URL now "
            f"returns something else ({SEC_BULK_URL})."
        )
    with zipfile.ZipFile(dest) as z:
        print(f"  contains {len(z.namelist()):,} company records")
    return dest


def load_ticker_map(dest: Path = CACHE / "company_tickers.json") -> dict[str, dict]:
    """ticker -> {cik, title}"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        r = requests.get(SEC_TICKERS_URL, headers=_headers(), timeout=120)
        r.raise_for_status()
        dest.write_bytes(r.content)
    raw = json.loads(dest.read_text())
    return {
        v["ticker"].upper(): {"cik": int(v["cik_str"]), "title": v["title"]}
        for v in raw.values()
    }


def facts_from_zip(zip_path: Path, cik: int) -> dict | None:
    with zipfile.ZipFile(zip_path) as z:
        name = f"CIK{cik:010d}.json"
        try:
            return json.loads(z.read(name))
        except KeyError:
            return None


_last_call = [0.0]


def facts_from_api(cik: int) -> dict | None:
    """Fallback for a handful of companies. Respects the SEC rate limit."""
    gap = 1.0 / config.SEC_RATE_LIMIT_PER_SEC
    elapsed = time.time() - _last_call[0]
    if elapsed < gap:
        time.sleep(gap - elapsed)
    _last_call[0] = time.time()
    r = requests.get(SEC_FACTS_URL.format(cik=cik), headers=_headers(), timeout=120)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------ extraction --
@dataclass
class AnnualSeries:
    """Canonical fields keyed by fiscal year."""
    cik: int
    name: str
    fields: dict[str, dict[int, float]]

    def series(self, field: str) -> dict[int, float]:
        return self.fields.get(field, {})

    def years(self) -> list[int]:
        if "revenue" in self.fields and self.fields["revenue"]:
            return sorted(self.fields["revenue"])
        seen: set[int] = set()
        for s in self.fields.values():
            seen.update(s)
        return sorted(seen)

    def latest(self, field: str) -> float | None:
        s = self.series(field)
        return s[max(s)] if s else None


def _is_annual_period(item: dict) -> bool:
    """Flow items: keep only ~12-month periods, not quarters."""
    start, end = item.get("start"), item.get("end")
    if not start or not end:
        return False
    from datetime import date

    y1, m1, d1 = (int(x) for x in start.split("-"))
    y2, m2, d2 = (int(x) for x in end.split("-"))
    days = (date(y2, m2, d2) - date(y1, m1, d1)).days
    return 350 <= days <= 380


def _pick_by_year(units: list[dict], instant: bool) -> dict[int, float]:
    """
    Collapse XBRL fact entries to one value per fiscal year.

    Only 10-K facts are used — 10-Q data would contaminate annual series.
    Where a year appears multiple times (original filing plus restatements in
    later filings), the most recently filed value wins.
    """
    best: dict[int, tuple[str, float]] = {}
    for item in units:
        if item.get("form") != "10-K":
            continue
        if not instant and not _is_annual_period(item):
            continue
        if instant and item.get("start"):
            continue
        fy = item.get("fy")
        if fy is None or item.get("fp") != "FY":
            continue
        filed = item.get("filed", "")
        val = item.get("val")
        if val is None:
            continue
        if fy not in best or filed > best[fy][0]:
            best[fy] = (filed, float(val))
    return {fy: v for fy, (_, v) in best.items()}


def extract(facts: dict) -> AnnualSeries:
    """companyfacts JSON -> canonical annual series."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    out: dict[str, dict[int, float]] = {}

    for field, candidates in TAG_MAP.items():
        instant = field in INSTANT_FIELDS
        unit_key = "shares" if field in SHARE_FIELDS else "USD"
        merged: dict[int, float] = {}
        # Walk candidates in priority order; earlier tags are not overwritten.
        for tag in candidates:
            node = gaap.get(tag)
            if not node:
                continue
            units = node.get("units", {}).get(unit_key)
            if not units:
                continue
            for fy, val in _pick_by_year(units, instant).items():
                merged.setdefault(fy, val)
        if merged:
            out[field] = merged

    return AnnualSeries(
        cik=int(facts.get("cik", 0)),
        name=facts.get("entityName", ""),
        fields=out,
    )
