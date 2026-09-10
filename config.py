"""
Screening thresholds — the single place to tune the screen.

Every value here maps to a numbered gate in screening-criteria.md (v0.2).
Change a threshold here, note it in the criteria doc changelog, rerun.
"""

# ---------------------------------------------------------------- identity --
# SEC requires a declared User-Agent with a name and contact email, and enforces
# 10 requests/second. Requests without it are rejected.
#
# Read from the environment, never hardcoded — this repo is public, and a plain
# email address in a public file gets scraped within days. Set it locally with
#     export SEC_USER_AGENT="Your Name your@email.com"
# and in CI as the repository secret SEC_USER_AGENT.
import os

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
SEC_RATE_LIMIT_PER_SEC = 8  # deliberately under the SEC's 10/s limit


def require_user_agent() -> str:
    """Fail loudly at the point of use rather than getting opaque SEC 403s."""
    if not SEC_USER_AGENT or "@" not in SEC_USER_AGENT:
        raise SystemExit(
            "SEC_USER_AGENT is not set.\n"
            '  Locally:  export SEC_USER_AGENT="Your Name your@email.com"\n'
            "  In CI:    gh secret set SEC_USER_AGENT\n"
            "The SEC rejects requests without a name and contact email."
        )
    return SEC_USER_AGENT

# ------------------------------------------------------------ macro anchor --
# §7 of the criteria doc. Re-anchor when the 10-year moves more than 75bps.
RISK_FREE_RATE = 0.0479          # US 10-year Treasury, 4 Sep 2026
EQUITY_RISK_PREMIUM = 0.05
REQUIRED_RETURN = 0.10           # DCF discount rate
TERMINAL_GROWTH_MAX = 0.025

# ------------------------------------------------------------- Tier 1 universe --
MIN_MARKET_CAP = 5_000_000_000
MIN_AVG_DAILY_VALUE = 15_000_000
MIN_YEARS_HISTORY = 10
MIN_YEARS_HISTORY_CYCLICAL = 15

EXCHANGES = {"NYSE", "NASDAQ", "NYSE American", "NYSEAMERICAN", "AMEX"}

# Excluded on evidentiary grounds, not sector grounds (§1).
EXCLUDED_SIC_PREFIXES = {
    "2836",  # biological products
    "8731",  # commercial physical & biological research (clinical-stage)
    "6726",  # investment offices, closed-end funds
    "6770",  # blank checks / SPACs
}

# REITs and regulated utilities: excluded by policy (decision of 9 Sep 2026).
# They finance themselves through equity issuance and negative free cash flow,
# which C1 and C2 treat as disqualifying — correctly, for a minority holder.
# Set False to bring them back, and expect them to fail C1/C2 rather than pass.
EXCLUDE_REITS_AND_UTILITIES = True

# ------------------------------------------------------- position sizing --
# §10: weight by margin of safety, not equally. Size follows conviction and
# price. The cap stops one deep-discount name dominating the sleeve.
MAX_POSITION_SHARE_OF_SLEEVE = 0.35
MIN_POSITION_SHARE_OF_SLEEVE = 0.10
TARGET_HOLDINGS = (3, 5)

# ------------------------------------------------------------- core gates --
# C1 cash conversion
C1_MIN_FCF_TO_NI = 0.80
C1_LOOKBACK_YEARS = 5

# C2 dilution
C2_MAX_SHARE_COUNT_RATIO = 1.02
C2_LOOKBACK_YEARS = 5

# C3 earnings durability
C3_YEARS = 10
C3_MIN_POSITIVE_NI_YEARS = 10
C3_MIN_POSITIVE_FCF_YEARS = 9

# C4 debt survivability — sector bands below
C4_MAX_NEAR_TERM_MATURITY_SHARE = 0.30

# C6 accounting integrity
C6_MAX_ADJUSTED_VS_GAAP_GAP = 0.25

# C7 capital allocation
C7_MIN_INCREMENTAL_ROIC = 0.10

# C9 current trading — the gap lululemon exposed.
# Every other gate reads a ten-year record, so the screen's most recent view of
# a company was up to fifteen months stale. These test the latest filed
# quarters against the same quarters a year earlier.
C9_MAX_REVENUE_DECLINE = 0.05        # >5% down year on year is a break
C9_MIN_OPERATING_INCOME_RATIO = 0.75 # >25% down year on year is a break
C9_ENABLED = True
# What to do when a company has no parseable quarterly filings.
#
# FALSE on purpose, and it is the opposite of the call made on C4. C4 tests
# whether a company can survive its debts, so not knowing had to mean "not
# proven safe". C9 tests whether the annual record is still current, and not
# knowing means only that the screen is where it already was before C9 existed.
# Making it blocking on day one would quietly delete every company whose
# quarterly tagging this parser does not handle — the failure mode that
# produced an empty universe in run 1.
#
# Run 7 reports the coverage. Flip this to True once the count says the parser
# reaches nearly everything, not before.
C9_UNVERIFIED_BLOCKS = False

# C8 roll-up test
C8_MAX_GOODWILL_INTANGIBLES_SHARE = 0.40

# Excused misses permitted across C1 and C3 combined (§2).
MAX_EXCUSED_MISSES = 1

# C7 when capital was RETURNED rather than retained: minimum owner-earnings-
# per-share CAGR. Buying back stock is good capital allocation only if value
# per share actually grew.
C7_MIN_PER_SHARE_GROWTH_IF_RETURNING = 0.05

# ------------------------------------------------------- sector leverage bands --
# §4. (max_net_debt_to_ebitda, min_interest_cover, test_at_trough)
LEVERAGE_BANDS = {
    "consumer":    (2.5, 8.0, False),
    "healthcare":  (2.5, 8.0, False),
    "industrials": (2.5, 8.0, True),
    "software":    (2.0, 12.0, False),
    "energy":      (2.0, 6.0, True),
    "utilities":   (5.5, 3.0, False),
    "reit":        (6.0, 2.5, False),
    "financials":  (None, None, False),   # capital ratios instead — see M4
    "media":       (2.5, 8.0, False),     # carries more debt than software does
    # A fee business has no EBITDA leverage question worth asking — it holds no
    # inventory and finances no asset base. What matters is that it is not
    # levered at all, which M-FEE-BS tests directly as net cash.
    "fee_business": (None, None, False),
}

# ------------------------------------------------ gross profitability floors --
# §5. C5: gross profit / total assets.
GROSS_PROFITABILITY_FLOORS = {
    "software":    0.40,
    "consumer":    0.30,
    "healthcare":  0.30,
    "industrials": 0.20,
    "energy":      None,     # module gates apply instead
    "utilities":   None,
    "reit":        None,
    "financials":  None,
    "media":       0.30,
    # Asset managers, exchanges, advisers and brokers report no cost of revenue,
    # so this gate could not be computed for them at all. In run 2 it was
    # unevaluable for 81 of 108 and cost five survivors for no reason connected
    # to business quality.
    "fee_business": None,
}

# ----------------------------------------------------------- sector modules --
# Each module: ROIC threshold and definition, growth floor, quality extras.
SECTOR_MODULES = {
    "consumer": {
        "roic_median_min": 0.15,
        "roic_every_year_min": 0.12,
        "roic_every_year_lookback": 5,
        "revenue_cagr_min": 0.03,
        "growth_measure": "calendar",
        "capitalise_rnd": False,
    },
    "industrials": {
        "roic_median_min": 0.13,
        "roic_every_year_min": 0.0,        # positive every year, not 12%
        "roic_every_year_lookback": 10,
        "revenue_cagr_min": 0.03,
        "growth_measure": "peak_to_peak",
        "capitalise_rnd": False,
    },
    "software": {
        "roic_median_min": 0.20,
        "roic_every_year_min": 0.12,
        "roic_every_year_lookback": 5,
        "revenue_cagr_min": 0.08,
        "growth_measure": "calendar",
        "capitalise_rnd": True,            # M3: else ROIC is an artefact
        "rnd_amortisation_years": 5,
        "max_sbc_to_revenue": 0.10,
        "fcf_after_sbc": True,
    },
    "healthcare": {
        "roic_median_min": 0.15,
        "roic_every_year_min": 0.12,
        "roic_every_year_lookback": 5,
        "revenue_cagr_min": 0.03,
        "growth_measure": "calendar",
        "capitalise_rnd": False,
        "max_patent_cliff_exposure": 0.25,  # manual input
    },
    "energy": {
        "roic_cycle_avg_min": 0.10,
        "roic_trough_min": 0.05,
        "revenue_cagr_min": 0.03,
        "growth_measure": "peak_to_peak",
        "capitalise_rnd": False,
    },
    "utilities": {
        # Achieved-vs-allowed ROE and rate-base growth are not in XBRL at all,
        # so §M6's real moat test stays manual. These are the computable floors.
        "roic_median_min": 0.05,          # regulated returns are low by design
        "roic_every_year_min": 0.0,
        "roic_every_year_lookback": 10,
        "roe_median_min": 0.08,
        "min_achieved_vs_allowed_roe": 0.90,   # manual
        "rate_base_growth_min": 0.04,          # manual
        "growth_measure": "calendar",
        "capitalise_rnd": False,
        "manual_checks": ["achieved vs allowed ROE", "rate-base growth",
                          "regulatory jurisdiction quality"],
    },
    "media": {
        # Split out of "software" after run 2. M3's 8% revenue floor was
        # calibrated for software, where 3% growth signals decline. Applied to
        # legacy media it is simply the wrong question — the New York Times
        # failed on 6.0% while running a genuinely improving subscription
        # business. Media earns its return from brand and library, not from
        # compounding seat growth, so the growth bar drops and the margin and
        # return bars stay.
        "roic_median_min": 0.12,
        "roic_every_year_min": 0.05,
        "roic_every_year_lookback": 10,
        "revenue_cagr_min": 0.02,
        "growth_measure": "calendar",
        "capitalise_rnd": False,
    },
    "fee_business": {
        # Asset managers, exchanges, advisers, insurance brokers. §M4 says these
        # do not belong on the bank track: the balance sheet is not the product,
        # so ROE-on-equity and capital ratios measure the wrong thing. Run 2
        # routed them to the consumer module instead, which was worse — its
        # signature gross-profitability gate cannot be computed for a business
        # with no cost of revenue, and 81 of 108 came back unevaluable.
        #
        # What actually distinguishes a good fee business: it earns a high
        # return on the little capital it uses, it holds a fat operating margin
        # through a market cycle, it grows fee revenue, and it carries no debt
        # because it has nothing to finance.
        "roic_median_min": 0.20,
        "roic_every_year_min": 0.10,
        "roic_every_year_lookback": 10,
        "roe_median_min": 0.15,
        "operating_margin_min": 0.20,      # M-FEE-MARGIN
        "revenue_cagr_min": 0.04,
        "growth_measure": "calendar",
        "capitalise_rnd": False,
        # M-FEE-BS. Two caps, not one, because "fee business" covers two
        # different animals. An asset manager's revenue moves with markets and
        # debt against it is genuinely dangerous. A ratings or data business
        # sells contractual subscriptions and can carry leverage safely —
        # Moody's has done so for two decades while compounding.
        #
        # Rather than guess which is which from SIC codes, the test is
        # BEHAVIOURAL: a fee business whose revenue has never fallen materially
        # in ten years has demonstrated it can support debt, and gets the
        # higher cap. Run 6 excluded Moody's at 0.59x on the single 0.50x cap,
        # despite 70.9% median ROE, a 41.7% operating margin and no loss in
        # nineteen years — a threshold I set with no calibration behind it.
        "max_net_debt_to_revenue": 0.50,
        "max_net_debt_to_revenue_if_stable": 1.25,
        "revenue_stability_max_decline": 0.05,
        "max_loss_years_in_20": 1,
    },
    "reit": {
        # XBRL-computable proxies. AFFO and same-store NOI are not GAAP tags, so
        # the real §M6 tests stay manual; these keep REITs in the funnel instead
        # of crashing the gate engine, which is what happened before.
        "roic_median_min": 0.04,          # REIT ROIC is structurally low
        "roic_every_year_min": 0.0,
        "roic_every_year_lookback": 10,
        "ffo_per_share_cagr_min": 0.03,   # net income + D&A per share, an FFO proxy
        "max_debt_to_assets": 0.60,       # BOOK LTV. §M6's 40% is on market value;
                                          # book runs higher, so this is the
                                          # equivalent, not a loosening
        "min_fixed_charge_cover": 2.5,
        "growth_measure": "calendar",
        "capitalise_rnd": False,
        "manual_checks": ["AFFO per share", "same-store NOI", "WALE", "tenant credit"],
    },
    "financials": {
        # Balance-sheet businesses only. Fee businesses (asset managers,
        # exchanges, brokers, advisers) run the operating-company gates — §M4
        # says so, and SIC routing in universe.py now enforces it.
        "roe_median_min": 0.12,
        "max_loss_years_in_20": 0,        # §M4: a clean record through 2008-09
        "roa_min_banks": 0.010,
        "tbvps_cagr_min": 0.08,
        "no_loss_years_lookback": 20,
        "min_cet1_buffer": 0.025,
        "max_loans_to_deposits": 0.90,
        "max_cost_income": 0.60,
        "insurer_max_combined_ratio": 1.00,
        "insurer_min_years_under_100": 8,
    },
}

# ------------------------------------------------------- valuation hurdle --
# §7 Lens A. (min_roic, reinvestment_band) -> required owner-earnings yield.
# Ordered most demanding quality first; first match wins.
QUALITY_ADJUSTED_HURDLE = [
    # (roic_floor, reinvestment: 'high'|'moderate'|'low'|'any', required_yield)
    (0.25, "high",     0.040),
    (0.25, "any",      0.060),
    (0.18, "high",     0.045),
    (0.18, "any",      0.055),
    (0.15, "any",      0.065),
    (0.12, "any",      0.080),
]
HURDLE_DEFAULT_YIELD = 0.080

# Hard limits on the quality credit (§7).
MAX_EV_TO_OWNER_EARNINGS = 30.0        # absolute ceiling, never breached
QUALITY_CREDIT_MAX_EARNINGS_DECLINE = 0.20   # no year worse than -20%
QUALITY_CREDIT_REQUIRES_WIDE_MOAT = True

# Reinvestment runway classification
REINVEST_HIGH_SHARE = 0.50     # >50% of earnings redeployed
REINVEST_LOW_SHARE = 0.25      # <25% redeployed
REINVEST_HIGH_MIN_ROIC = 0.20  # ...at >=20% incremental ROIC

# Margin of safety by moat classification (§7 Lens B).
# Retained for the manual override path only — moats.csv now overrides the
# computed score rather than being the only source of it. See moat.py.
MARGIN_OF_SAFETY = {"wide": 0.30, "narrow": 0.40, "uncertain": 0.50}

# --- Computed moat durability (moat.py) -----------------------------------
# The margin of safety is now a continuous function of the durability score
# rather than three buckets, so a name near a boundary does not jump ten
# percentage points of required discount on a noisy measurement.
#
# Calibration note: a score of 50 lands exactly on 0.40, the old "narrow"
# default that every unlisted company received. So the change moves genuinely
# durable businesses down toward 0.30 and fragile ones up toward 0.50, without
# silently re-pricing the middle of the distribution.
MOS_AT_FULL_DURABILITY = 0.30    # score 100
MOS_AT_ZERO_DURABILITY = 0.50    # score 0

# Labels are descriptive only — the score drives the price. These are also the
# thresholds the hurdle's quality credit tests.
# Recalibrated against run 5, which came out 33 wide of 40 — the bar was far
# too low. Root cause was not only the threshold: persistence and gross
# profitability were near-free points, because the C-gates that create this
# cohort already require returns above the cost of capital and a GP/assets
# floor. Both band sets were tightened in moat.py alongside these thresholds.
# Re-scoring run 5's own components under the new settings gives 9 wide, 25
# narrow, 6 uncertain — about a fifth of an already quality-screened list,
# which is what "wide" ought to mean.
DURABILITY_WIDE_MIN = 80
DURABILITY_NARROW_MIN = 55

# Below this many points of the 100 actually measurable, the score is not
# trusted: the label falls to "uncertain", the quality credit is withheld, and
# the margin of safety blends back toward the widest setting. Not knowing much
# about a business is a reason to demand a bigger discount, not a neutral fact.
DURABILITY_MIN_COVERAGE = 50

# Lens C veto
LENS_C_MAX_STDEV_ABOVE_MEDIAN = 1.0

# ---------------------------------------------------------------- scoring --
SCORE_WEIGHTS = {
    "quality_moat": 30,
    "capital_allocation": 20,
    "financial_strength": 15,
    "predictability": 15,
    "valuation": 20,
}
SCORE_RESEARCH_THRESHOLD = 70
SCORE_WATCHLIST_THRESHOLD = 55
