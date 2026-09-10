"""
The forward-looking overlay: facts about a company that XBRL does not carry.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
It is NOT a forecast, and nothing here is anyone's estimate of what a business
will earn. Every field records something already stated on the record and
dated: what management guided to, whether they have revised that guidance down,
whether the chief executive changed, whether there is a live legal or
regulatory matter. Facts with a source, maintained by hand between runs.

WHY IT EXISTS
-------------
Run 7 proved the arithmetic layer is not enough on its own. C9 reads the latest
filed quarters, so lululemon was measured on the two quarters to 2 August 2026:
revenue -0.2%, operating income -24.1%. It passed by a percentage point, the
haircut cut its yield from 18.7% to 14.3%, and it still sat first in the queue
clearing the hurdle.

What the filings could not say is that management had by then cut full-year
guidance for the second consecutive time, to a 5-7% revenue decline. That is a
fact, it is dated, it is on the record — and it is worth more than any amount
of further arithmetic on the trailing numbers.

TWO CUTS IS THE SIGNAL
----------------------
Not the level of guidance, which is management's opinion of their own year, but
the REVISION. One cut is a bad quarter. Two consecutive cuts is management
telling you their own understanding of the business was wrong, twice — and it
is the single most reliable forward signal available without forecasting
anything.
"""

from __future__ import annotations

import csv
from pathlib import Path

FIELDS = ["ticker", "as_of", "guidance", "consecutive_cuts",
          "guided_fy_earnings_change", "ceo_change_12m", "earnings_distorted",
          "issue", "severity", "source"]

SEVERITIES = {"none", "watch", "veto"}


def load(path: Path) -> dict[str, dict]:
    """ticker -> row. An absent file means no overlay, never an error."""
    if not path.exists():
        return {}
    out = {}
    with open(path) as fh:
        for r in csv.DictReader(fh):
            t = (r.get("ticker") or "").strip().upper()
            if t:
                out[t] = r
    return out


def _f(row: dict, key: str) -> float | None:
    v = (row.get(key) or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _i(row: dict, key: str) -> int:
    v = _f(row, key)
    return int(v) if v is not None else 0


def verdict(row: dict | None) -> tuple[bool, str]:
    """
    (blocks, reason). Blocking is reserved for facts that make a company
    unpriceable this quarter, not for anything merely discouraging.
    """
    if not row:
        return False, "no overlay entry"
    sev = (row.get("severity") or "none").strip().lower()
    cuts = _i(row, "consecutive_cuts")
    bits = []
    g = (row.get("guidance") or "").strip().lower()
    if g:
        bits.append(f"guidance {g}")
    if cuts:
        bits.append(f"{cuts} consecutive cut(s)")
    if (row.get("ceo_change_12m") or "").strip().lower() in ("y", "yes", "true"):
        bits.append("CEO changed within 12 months")
    if row.get("issue"):
        bits.append(row["issue"][:80])
    note = "; ".join(bits) or "overlay entry with nothing flagged"

    if sev == "veto":
        return True, f"{note} — flagged as disqualifying"
    if cuts >= 2:
        return True, (f"{note} — guidance cut in two consecutive quarters; "
                      "management's own view of the year has been wrong twice")
    return False, note


def earnings_distorted(row: dict | None) -> bool:
    """
    Is the current period's reported profit inflated by something that will not
    recur?

    Found while reading the September 2026 shortlist by hand, and it is the
    most consequential thing the qualitative pass turned up. The Supreme Court
    struck down the IEEPA tariffs in February 2026, so importers received
    refunds — and those refunds land in GAAP operating income:

        Five Below   $163.6m   operating margin 21.8% vs 5.1% a year earlier
        lululemon    $134.5m
        Logitech      $61.0m   operating income +60%, or +14% without it
        Lennox        $30.0m

    C9 compares operating income year on year to catch a business that has
    stopped earning what its record says. A one-off refund does the opposite of
    what C9 needs: it makes a deteriorating business look like an improving one
    and the gate waves it through. Five Below is the extreme case — GAAP EPS
    $3.99 against $1.68 adjusted.

    This cannot be detected from XBRL: the refund sits inside operating income
    with no separate tag. It has to be read off the results release, which is
    exactly what the overlay is for.
    """
    return (row or {}).get("earnings_distorted", "").strip().lower() in (
        "y", "yes", "true", "1")


def earnings_factor(row: dict | None) -> float | None:
    """
    Management's own guided change in full-year earnings, as a multiplier.

    Applied as a CAP on the valuation base alongside the quarterly haircut, so
    the screen values a company on the lower of what it is currently earning
    and what its own management says it will earn. Never above 1.0 — guidance
    above last year buys no credit, for the same reason one good quarter does
    not: it is a statement of intent, not a result.
    """
    v = _f(row or {}, "guided_fy_earnings_change")
    if v is None:
        return None
    return min(1.0, max(0.0, 1.0 + v))


def template(path: Path) -> None:
    """Write an empty overlay with the header, so the format is self-evident."""
    with open(path, "w", newline="") as fh:
        csv.DictWriter(fh, fieldnames=FIELDS).writeheader()
