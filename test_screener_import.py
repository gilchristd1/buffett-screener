"""
Screener-export parsing tests.

Exports differ by provider in column naming and number formatting. These check
the normalisation handles the real variants, and that Tier 1 is applied by us
rather than trusted to whatever the screener's own filters were set to.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import screener_import as SI  # noqa: E402

# Finviz-style: suffixed market caps, "Company", separate Volume column
FINVIZ = """No.,Ticker,Company,Sector,Industry,Price,Volume,Market Cap
1,COST,Costco Wholesale,Consumer Defensive,Discount Stores,915.74,2100000,406.11B
2,MSFT,Microsoft Corp,Technology,Software - Infrastructure,512.30,18000000,3.81T
3,TINY,Tiny Cap Inc,Industrials,Tools,4.10,900000,850.5M
4,ILLQ,Illiquid Corp,Healthcare,Devices,60.00,1000,9.2B
5,XXXX,Unknown Sector Co,Blockchain,Misc,20.00,5000000,12.0B
"""

# stockanalysis-style: full-precision caps, "Symbol", "$" and commas
STOCKANALYSIS = """Symbol,Company Name,Market Cap,Sector,Share Price,Avg Volume
BRK.B,"Berkshire Hathaway Inc.","$1,092,400,000,000",Financials,505.20,3800000
UNP,"Union Pacific Corp","$132,500,000,000",Industrials,225.10,2900000
NODATA,"Missing Cap Co",-,Technology,10.00,500000
"""


def check(label, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    return condition


def write(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    fh.write(text)
    fh.close()
    return fh.name


def main():
    failures = 0

    print("\n=== number formats across providers ===")
    cases = [("406.11B", 406.11e9), ("3.81T", 3.81e12), ("850.5M", 850.5e6),
             ("$1,092,400,000,000", 1.0924e12), ("12.3%", 12.3),
             ("-", None), ("", None), (None, None), ("N/A", None)]
    for raw, expected in cases:
        got = SI.parse_number(raw)
        ok = (got is None and expected is None) or (
            got is not None and expected is not None and abs(got - expected) < max(1.0, abs(expected) * 1e-9))
        failures += not check(f"parse {raw!r}", ok, f"got {got}")

    print("\n=== Finviz-style export ===")
    rows, warns = SI.read_export(write(FINVIZ))
    failures += not check("all 5 rows parsed", len(rows) == 5, f"got {len(rows)}")
    cost = next(r for r in rows if r["ticker"] == "COST")
    failures += not check("Consumer Defensive maps to the consumer module",
                          cost["module"] == "consumer", cost["module"])
    failures += not check("market cap read as 406.11bn",
                          abs(cost["market_cap"] - 406.11e9) < 1e6)
    failures += not check("ADV derived from price x volume",
                          abs(cost["adv"] - 915.74 * 2_100_000) < 1)
    msft = next(r for r in rows if r["ticker"] == "MSFT")
    failures += not check("Technology maps to the software module",
                          msft["module"] == "software", msft["module"])
    failures += not check("unmapped sector is warned, not silently dropped",
                          any("unmapped sectors" in w for w in warns), str(warns))

    print("\n=== Tier 1 applied by us, not by the screener ===")
    kept, counts = SI.apply_tier1(rows, min_market_cap=5e9, min_adv=15e6)
    tickers = {r["ticker"] for r in kept}
    failures += not check("large liquid names kept", tickers == {"COST", "MSFT"}, str(tickers))
    failures += not check("sub-$5bn name rejected", counts["too_small"] == 1, str(counts))
    failures += not check("illiquid name rejected", counts["illiquid"] == 1, str(counts))
    failures += not check("unmapped-sector name rejected", counts["no_sector"] == 1, str(counts))

    print("\n=== stockanalysis-style export ===")
    rows2, warns2 = SI.read_export(write(STOCKANALYSIS))
    brk = next(r for r in rows2 if r["ticker"] == "BRK.B")
    failures += not check("'Symbol' header resolved as ticker", brk["ticker"] == "BRK.B")
    failures += not check("comma/dollar market cap parsed",
                          abs(brk["market_cap"] - 1.0924e12) < 1e6, str(brk["market_cap"]))
    failures += not check("Financials maps to the financials module",
                          brk["module"] == "financials", brk["module"])
    kept2, counts2 = SI.apply_tier1(rows2, 5e9, 15e6)
    failures += not check("row with no market cap is rejected, not defaulted",
                          counts2["no_market_cap"] == 1, str(counts2))

    print("\n=== missing columns are surfaced ===")
    _, warns3 = SI.read_export(write("Foo,Bar\n1,2\n"))
    failures += not check("missing ticker warned",
                          any("no 'ticker' column" in w for w in warns3), str(warns3))
    failures += not check("missing market cap warned",
                          any("no 'market_cap' column" in w for w in warns3), str(warns3))
    failures += not check("missing sector warned",
                          any("no 'sector' column" in w for w in warns3), str(warns3))

    print(f"\n{'='*60}")
    print("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    print(f"{'='*60}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
