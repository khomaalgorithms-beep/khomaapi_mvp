"""Access-control logic tests — the rules that gate paid features.

These are intentionally exhaustive: a wrong answer here either locks out a
paying customer or lets a non-payer trade.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from app import entitlements as ent


# --------------------------------------------------------------------------
# Account caps
# --------------------------------------------------------------------------

def test_max_accounts_per_tier():
    assert ent.max_accounts("solo") == 2
    assert ent.max_accounts("pro") == 10
    assert ent.max_accounts("elite") is ent.UNLIMITED   # unlimited
    assert ent.max_accounts("founder") == 10
    assert ent.max_accounts(None) == 0                  # no plan = no accounts
    assert ent.max_accounts("bogus") == 0


def test_solo_blocked_from_third_account():
    # Solo cap is 2.
    assert ent.can_add_account("solo", 0) is True
    assert ent.can_add_account("solo", 1) is True
    assert ent.can_add_account("solo", 2) is False      # the 3rd is blocked
    assert ent.within_account_cap("solo", 2) is True
    assert ent.within_account_cap("solo", 3) is False


def test_elite_unlimited_accounts():
    assert ent.can_add_account("elite", 999) is True
    assert ent.within_account_cap("elite", 100000) is True


def test_pro_account_cap():
    assert ent.can_add_account("pro", 9) is True
    assert ent.can_add_account("pro", 10) is False


# --------------------------------------------------------------------------
# Feature gating
# --------------------------------------------------------------------------

def test_safety_features_on_every_plan():
    for tier in ("solo", "pro", "elite", "founder"):
        assert ent.has_feature(tier, ent.RISK_ENGINE) is True
        assert ent.has_feature(tier, ent.ECONOMIC_CALENDAR) is True
        assert ent.has_feature(tier, ent.JOURNAL) is True
        assert ent.has_feature(tier, ent.TRADING) is True


def test_solo_blocked_from_premium_features():
    assert ent.has_feature("solo", ent.COPY_TRADING) is False
    assert ent.has_feature("solo", ent.EVAL_FUNDED) is False
    assert ent.has_feature("solo", ent.EMAIL_DIGESTS) is False


@pytest.mark.parametrize("tier", ["pro", "elite", "founder"])
def test_premium_tiers_get_premium_features(tier):
    assert ent.has_feature(tier, ent.COPY_TRADING) is True
    assert ent.has_feature(tier, ent.EVAL_FUNDED) is True
    assert ent.has_feature(tier, ent.EMAIL_DIGESTS) is True


def test_no_tier_has_nothing():
    for feature in ent.ALL_FEATURES:
        assert ent.has_feature(None, feature) is False
        assert ent.has_feature("", feature) is False
        assert ent.has_feature("bogus", feature) is False


# --------------------------------------------------------------------------
# Subscription active-ness (canceled-but-paid-through, expired, refunded …)
# --------------------------------------------------------------------------

def test_active_status_is_active():
    for s in ("active", "trialing", "completed", "valid", "ACTIVE"):
        assert ent.subscription_active(s) is True


def test_dead_statuses_are_inactive():
    for s in ("expired", "refunded", "unpaid", "past_due", "", None, "deleted"):
        assert ent.subscription_active(s) is False


def test_canceled_active_until_period_end():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    future = (now + timedelta(days=5)).isoformat()
    past = (now - timedelta(days=1)).isoformat()
    # canceled but still inside the paid period → still active
    assert ent.subscription_active("canceled", future, now=now) is True
    # canceled and period elapsed → revoked
    assert ent.subscription_active("canceled", past, now=now) is False
    # canceled with no end date → revoked
    assert ent.subscription_active("canceled", None, now=now) is False


def test_canceled_accepts_unix_timestamp():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    future_ts = int((now + timedelta(days=3)).timestamp())
    assert ent.subscription_active("canceled", future_ts, now=now) is True
    assert ent.subscription_active("canceled", str(future_ts), now=now) is True


# --------------------------------------------------------------------------
# Plan id → tier mapping (driven by env, never hardcoded)
# --------------------------------------------------------------------------

def test_plan_id_to_tier_from_env(monkeypatch):
    monkeypatch.setenv("WHOP_PLAN_SOLO_M", "plan_solo_m")
    monkeypatch.setenv("WHOP_PLAN_SOLO_Y", "plan_solo_y")
    monkeypatch.setenv("WHOP_PLAN_PRO_M", "plan_pro_m")
    monkeypatch.setenv("WHOP_PLAN_ELITE_Y", "plan_elite_y")
    monkeypatch.setenv("WHOP_PLAN_FOUNDER", "plan_founder")
    assert ent.tier_for_plan_id("plan_solo_m") == "solo"
    assert ent.tier_for_plan_id("plan_solo_y") == "solo"
    assert ent.tier_for_plan_id("plan_pro_m") == "pro"
    assert ent.tier_for_plan_id("plan_elite_y") == "elite"
    assert ent.tier_for_plan_id("plan_founder") == "founder"
    assert ent.tier_for_plan_id("unknown_plan") is None
    assert ent.tier_for_plan_id("") is None
    assert ent.tier_for_plan_id(None) is None


def test_ats_free_plans_map_to_elite(monkeypatch):
    monkeypatch.setenv("WHOP_PLAN_ATS", "plan_ats_free, plan_ats_two")
    assert ent.tier_for_plan_id("plan_ats_free") == "elite"
    assert ent.tier_for_plan_id("plan_ats_two") == "elite"


def test_unconfigured_plan_ids_are_ignored(monkeypatch):
    for k in ("WHOP_PLAN_SOLO_M", "WHOP_PLAN_SOLO_Y", "WHOP_PLAN_PRO_M",
              "WHOP_PLAN_PRO_Y", "WHOP_PLAN_ELITE_M", "WHOP_PLAN_ELITE_Y",
              "WHOP_PLAN_FOUNDER"):
        monkeypatch.delenv(k, raising=False)
    assert ent.tier_for_plan_id("anything") is None
    assert ent.plan_env_map() == {}
