# Buffett Screener

Implements the gates in `screening-criteria.md` v0.4 against SEC XBRL data.

## Where this runs, and why

Claude's cloud sandbox and the Cowork VM on your Mac both sit behind an egress
proxy that denies `sec.gov` and `data.sec.gov`. The live data run therefore has
to happen somewhere with unrestricted internet. Three places qualify:

| Where | Manual steps | Needs your Mac awake | Recommended |
|---|---|---|---|
| **GitHub Actions** | none after setup | no | ✅ |
| Your Mac via `launchd` | none after setup | yes | fallback |
| Your Terminal, by hand | run 3 commands | yes | one-offs |

GitHub is reachable *from* Claude's sandbox even though SEC is not, so Actions
runs the screen and commits the results, and Claude reads them from
`raw.githubusercontent.com`. That closes the loop with no manual step.

## Fully automated setup (once, ~10 minutes)

```bash
brew install gh && gh auth login     # if you don't have the GitHub CLI
cd ~/Documents/buffett-screener
./setup_github.sh
```

It prompts for your SEC contact string ("Your Name your@email.com"), runs the
test suite, creates a **public** repo, pushes, and stores the contact string as
the Actions secret `SEC_USER_AGENT`.

**The repo is public so Claude can read the results; your email is not in it.**
The SEC rejects requests without a name and contact email, so that value is read
from the environment at runtime and never committed. Locally:

```bash
export SEC_USER_AGENT="Your Name your@email.com"
```

**Never commit holdings, buy prices or position sizes.** The repo holds the
methodology and public-company data only; your portfolio lives in the Claude
project. `setup_github.sh` refuses to publish if it finds your email in a
tracked file, but it cannot catch everything.

The workflow `.github/workflows/screen.yml`:

- runs quarterly (1st of Mar/Jun/Sep/Dec), on demand, and on any `config.py` change
- runs the test suite first, so a broken gate never produces output you might trust
- builds the universe, runs the gates, commits `out/survivors.csv`, `out/results.csv`
  and `out/summary.md` back to the repo

Because every threshold change is a commit and every run is a diff, the repo
history *is* the changelog the criteria document asks for — you can see exactly
which threshold change added or removed which company.

Give Claude the raw URL of `out/summary.md` and it can read each run's output
without you touching anything.

## Running locally as well (optional)

Only needed if you want to run the screen by hand rather than waiting for Actions.

```bash
cd ~/Documents/buffett-screener
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SEC_USER_AGENT="Your Name your@email.com"
```

## Check the logic first (no network needed)

```bash
python3 tests/test_gates.py
```

```bash
python3 tests/test_screener_import.py
```

22 gate checks covering each gate, the sector overrides, the excused-miss rule,
the cyclical growth measure and the valuation hurdle guardrails, plus 24 import
checks on screener-export parsing across provider formats. All pass.

The GitHub Actions workflow runs both suites before the screen, so a broken gate
cannot silently produce output you might act on.

## Run the screen

**Recommended path — start from a web-screener export.** Configure a free screener
(stockanalysis.com or Finviz) with the Tier 1 filters plus crude quality proxies
(ROIC TTM ≥ 12%, net debt/EBITDA ≤ 2.5, positive FCF), export the CSV, then:

```bash
python3 run_screen.py --from-screener ~/Downloads/screener.csv
python3 run_screen.py --screen
python3 run_screen.py --explain COST      # why one name passed or failed
```

This cuts ~1,200 candidates to ~150–250 before the SEC step, so the per-CIK API
finishes in about thirty seconds and the 1.5GB bulk download is never needed.

Configure the screener with **Tier 1 filters and rough proxies only, never the
gates themselves**. Every public screener filters on trailing-twelve-month
snapshots; the gates that make this a Buffett screen — ROIC positive in every one
of ten years, incremental ROIC on retained earnings, peak-to-peak growth,
R&D-capitalised ROIC, leverage at trough EBITDA — are all multi-year and not
available on any free site. Narrow with the screener; decide with the gates.

**Full-universe path** (no screener export, much slower):

```bash
python3 run_screen.py --download          # SEC bulk companyfacts, ~1.5GB
python3 run_screen.py --build-universe    # every SEC ticker via yfinance
python3 run_screen.py --screen
```

`--explain` is the one to reach for most. Every gate carries its own reason
string, so a rejection is always auditable — a screen you can't interrogate is
one you'll eventually stop trusting.

## Files

| File | What it holds |
|---|---|
| `config.py` | **Every threshold.** Tune the screen here and nowhere else. Each value maps to a numbered gate in the criteria doc. |
| `secdata.py` | SEC retrieval and the XBRL tag-normalisation map |
| `screener_import.py` | Parse a Finviz / stockanalysis / TradingView export into the universe |
| `metrics.py` | Derived metrics — ROIC, owner earnings, gross profitability, growth measures |
| `gates.py` | The gate engine: core gates C1–C8 plus sector modules |
| `run_screen.py` | CLI orchestration |
| `tests/test_gates.py` | Gate logic verification, no network |
| `tests/test_screener_import.py` | Screener-export parsing verification, no network |
| `.github/workflows/screen.yml` | Scheduled run on GitHub Actions — the automated path |
| `setup_github.sh` | One-time public-repo creation, secret setup and push |

## Known limitations — read before trusting output

1. **Tag coverage is the weak point.** `TAG_MAP` in `secdata.py` handles the
   common US-GAAP variants, but filers use tags inconsistently and change them
   between years. A company that fails with several `??` (unevaluable) gates
   is usually a tag-mapping gap, not a bad business. Check with `--explain`
   before concluding anything.

2. **Unevaluable never counts as a pass.** Missing data fails the company. That
   is the right default, but it means tag gaps show up as false rejections.

3. **Gates needing filings, not figures, are stubbed as MANUAL.** C6 (accounting
   integrity) checks restatements, auditor changes and 8-K Item 4.02 — none of
   which are in XBRL. It returns PASS with a MANUAL note so it doesn't silently
   reject; you must check it by hand before buying anything.

4. **Financial-sector gates are partial.** ROE and the loss-year test are
   implemented; CET1, loans/deposits, combined ratio and reserve development are
   not — those need FR Y-9C or statutory filings, not XBRL.

5. **Utilities and REITs need manual inputs.** Achieved-vs-allowed ROE, rate-base
   growth, same-store NOI and AFFO aren't reliably tagged.

6. **Valuation (Lens A/B/C) is not wired into the run.** The hurdle function and
   its guardrails are implemented and tested in `metrics.py`, but the screen
   currently stops at the quality gates — deliberately, since that's the tier
   worth calibrating first.

## Next

Run `--screen` and check the survivor count against the estimate of 25–50. Far
more means a gate isn't binding; far fewer usually means tag coverage, not
quality. The rejection histogram printed at the end of a run tells you which.
