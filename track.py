"""
The tracking record: what the screen said, when it said it, and what happened.

WHY THIS HAS TO START NOW
-------------------------
A record assembled later is already biased. Reconstructing "what would we have
bought in September 2026" next year means choosing the entry date, the price and
the name with a year of hindsight, and every one of those choices flatters the
result. The only honest version is written on the day, before the outcome is
known, and never edited afterwards.

So `out/track.csv` is APPEND-ONLY. Every run adds a row for each name that
cleared the hurdle on that date, with the price, the buy price and the reasoning
attached. No row is ever rewritten — a correction is a new row, and the file is
the audit trail.

WHAT IT IS FOR, AND WHAT IT IS NOT
----------------------------------
It is not a portfolio, and nothing here is a position. It records what a
mechanical framework selected on a date, so that in two or three years there is
evidence about whether the framework works rather than only whether it runs.

The honest measure is not "did the picks go up". Over a year that is mostly
market direction and luck. The questions worth answering are narrower and take
longer: do the names that clear the hurdle earn more over five years than the
ones that did not? Does the durability score predict anything? Does the
two-consecutive-cuts rule avoid more damage than it costs in missed recoveries?
None of those can be answered yet, and pretending otherwise would be worse than
having no record at all.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

FIELDS = [
    "first_seen",        # the date this name first cleared — never updated
    "run_date",          # the date of the run that produced this row
    "ticker", "name", "module",
    "score", "durability", "moat",
    "price", "buy_price", "owner_earnings_yield", "hurdle",
    "clears_hurdle",
    "status",            # cleared | watchlist | dropped
    "note",              # why it changed, where a change is what prompted the row
]


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as fh:
        return list(csv.DictReader(fh))


def _key(row: dict) -> tuple:
    return (row.get("ticker", ""), row.get("status", ""))


def update(path: Path, ranked: list[dict], run_date: str | None = None) -> dict:
    """
    Append rows for anything whose status CHANGED since the last run.

    Deliberately not a row per name per run: that produces a file nobody reads
    and buries the four or five moments that actually matter. A name appears
    when it first clears, when it stops clearing, and when it drops off the
    survivor list — and those transitions are the record.

    Returns counts for the run log.
    """
    run_date = run_date or date.today().isoformat()
    existing = load(path)

    # Latest known status per ticker, and the date it was first seen clearing.
    last_status: dict[str, str] = {}
    first_seen: dict[str, str] = {}
    for r in existing:
        t = r["ticker"]
        last_status[t] = r["status"]
        first_seen.setdefault(t, r.get("first_seen") or r.get("run_date", ""))

    now: dict[str, dict] = {}
    for r in ranked:
        t = r["ticker"]
        clears = r.get("clears_hurdle") == "yes"
        now[t] = dict(r, _status="cleared" if clears else "watchlist")

    new_rows = []

    for t, r in now.items():
        status = r["_status"]
        if last_status.get(t) == status:
            continue                      # nothing changed, nothing to record
        was = last_status.get(t)
        note = ("first appearance on the list" if was is None
                else f"moved from {was} to {status}")
        new_rows.append({
            "first_seen": first_seen.get(t, run_date),
            "run_date": run_date,
            "ticker": t, "name": r.get("name", ""), "module": r.get("module", ""),
            "score": r.get("score", ""), "durability": r.get("durability", ""),
            "moat": r.get("moat", ""),
            "price": r.get("price", ""), "buy_price": r.get("buy_price", ""),
            "owner_earnings_yield": r.get("owner_earnings_yield", ""),
            "hurdle": r.get("hurdle", ""),
            "clears_hurdle": r.get("clears_hurdle", ""),
            "status": status, "note": note,
        })

    # Names that were on the list last time and are not on it now. Worth a row:
    # a name leaving is as much a record of the framework's behaviour as a name
    # arriving, and without it the file only ever tells the flattering half.
    for t, status in last_status.items():
        if t in now or status == "dropped":
            continue
        new_rows.append({
            "first_seen": first_seen.get(t, ""), "run_date": run_date,
            "ticker": t, "name": "", "module": "", "score": "", "durability": "",
            "moat": "", "price": "", "buy_price": "", "owner_earnings_yield": "",
            "hurdle": "", "clears_hurdle": "", "status": "dropped",
            "note": f"no longer among the survivors (was {status})",
        })

    if new_rows:
        write_header = not path.exists()
        with open(path, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if write_header:
                w.writeheader()
            w.writerows(new_rows)

    return {
        "appended": len(new_rows),
        "total": len(existing) + len(new_rows),
        "cleared_now": sum(1 for r in now.values() if r["_status"] == "cleared"),
        "tracked_names": len(set(last_status) | set(now)),
    }
