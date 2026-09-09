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

# Margin of safety by moat classification (§7 Lens B)
MARGIN_OF_SAFETY = {"wide": 0.30, "narrow": 0.40, "uncertain": 0.50}

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
