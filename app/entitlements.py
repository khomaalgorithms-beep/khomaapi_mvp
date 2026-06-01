"""Plan → entitlement logic for KhomaAPI (pure, no DB / no I/O).

Single source of truth for *what each plan can do*. Kept import-light and
side-effect free so every access rule is unit-testable in isolation — the place
bugs would either lock out paying customers or let non-payers in.

Whop is the runtime source of truth for *whether* a subscription is active
(see main.py resolver); this module only maps a resolved plan/tier to features
and limits.
"""

import os
from datetime import datetime, timezone

# ---- Feature keys -------------------------------------------------------

# Always available to ANY active subscriber (never gate safety / core trading).
RISK_ENGINE = "risk_engine"
ECONOMIC_CALENDAR = "economic_calendar"
JOURNAL = "journal"
TRADING = "trading"          # dashboard, broker connect, webhooks, order routing
ALWAYS_ON = frozenset({RISK_ENGINE, ECONOMIC_CALENDAR, JOURNAL, TRADING})

# Gated to Pro / Elite / Founder.
COPY_TRADING = "copy_trading"
EVAL_FUNDED = "eval_funded"  # eval→funded phase tracking + all prop presets
EMAIL_DIGESTS = "email_digests"
GATED = frozenset({COPY_TRADING, EVAL_FUNDED, EMAIL_DIGESTS})

ALL_FEATURES = ALWAYS_ON | GATED

UNLIMITED = None  # max_accounts sentinel

# ---- Tier definitions ---------------------------------------------------

TIERS = {
    "solo":    {"max_accounts": 2,         "gated": frozenset()},
    "pro":     {"max_accounts": 10,        "gated": GATED},
    "elite":   {"max_accounts": UNLIMITED, "gated": GATED},
    "founder": {"max_accounts": 10,        "gated": GATED},
}
VALID_TIERS = frozenset(TIERS.keys())

# Subscription lifecycle states (Whop). "canceled" is handled specially:
# access continues until current_period_end.
ACTIVE_STATUSES = frozenset({"active", "trialing", "completed", "valid"})
CANCELED_STATUSES = frozenset({"canceled", "cancelled"})


# ---- Plan id ↔ tier mapping (ids live in env, never hardcoded) ----------

def plan_env_map():
    """Map every configured Whop plan id → tier. Empty/unset ids are ignored."""
    pairs = [
        ("WHOP_PLAN_SOLO_M", "solo"),
        ("WHOP_PLAN_SOLO_Y", "solo"),
        ("WHOP_PLAN_PRO_M", "pro"),
        ("WHOP_PLAN_PRO_Y", "pro"),
        ("WHOP_PLAN_ELITE_M", "elite"),
        ("WHOP_PLAN_ELITE_Y", "elite"),
        ("WHOP_PLAN_FOUNDER", "founder"),
    ]
    out = {}
    for env_name, tier in pairs:
        pid = (os.getenv(env_name) or "").strip()
        if pid:
            out[pid] = tier
    # ATS-program free plans → Elite (comma-separated plan ids; comp access that
    # comes bundled with the high-ticket ATS program, granted via a $0 Whop plan).
    for pid in (os.getenv("WHOP_PLAN_ATS") or "").split(","):
        pid = pid.strip()
        if pid:
            out[pid] = "elite"
    return out


def tier_for_plan_id(plan_id):
    """Resolve a Whop plan id to a tier, or None if unknown/unconfigured."""
    if not plan_id:
        return None
    return plan_env_map().get(str(plan_id).strip())


# ---- Core entitlement queries -------------------------------------------

def normalize_tier(tier):
    t = (tier or "").lower().strip()
    return t if t in VALID_TIERS else None


def max_accounts(tier):
    """Connected-account cap for a tier. None = unlimited. 0 = no access."""
    t = normalize_tier(tier)
    if t is None:
        return 0
    return TIERS[t]["max_accounts"]


def within_account_cap(tier, current_count):
    """True if a tier may hold `current_count` connected accounts."""
    cap = max_accounts(tier)
    if cap is UNLIMITED:
        return True
    return current_count <= cap


def can_add_account(tier, current_count):
    """True if a tier may connect ONE MORE account beyond current_count."""
    return within_account_cap(tier, current_count + 1)


def has_feature(tier, feature):
    """Does this tier include `feature`? (assumes the subscription is active —
    active-ness is decided separately by subscription_active)."""
    t = normalize_tier(tier)
    if t is None:
        return False
    if feature in ALWAYS_ON:
        return True
    return feature in TIERS[t]["gated"]


# ---- Subscription active-ness -------------------------------------------

def _parse_dt(value):
    """Parse an ISO string or unix timestamp (int/float/str) → aware UTC dt."""
    if value is None or value == "":
        return None
    try:
        # numeric unix seconds
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def subscription_active(status, current_period_end=None, now=None):
    """True if a subscription with this status is currently entitled.

    - active/trialing/completed/valid → active.
    - canceled → active only until current_period_end (paid through the period).
    - anything else (expired/refunded/unpaid/past_due/empty) → inactive.
    """
    s = (status or "").lower().strip()
    if s in ACTIVE_STATUSES:
        return True
    if s in CANCELED_STATUSES:
        end = _parse_dt(current_period_end)
        if end is None:
            return False
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return end > now
    return False
