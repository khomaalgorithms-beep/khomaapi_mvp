"""Unit tests of the Lucid simulator against Lucid's own worked numbers. Run: python3 test_lucid_sim.py"""
from lucid_sim import Account, run_eval, run_funded, funded_tier_minis, LOCKED_MLL


def test_mll_starts_48000_and_trails_eod():
    a = Account()
    assert a.mll == 48_000
    a.balance = 51_000; a.eod_update()
    assert a.mll == 49_000
    a.balance = 50_500; a.eod_update()          # never moves down
    assert a.mll == 49_000


def test_mll_locks_at_50100_after_close_above_52100():
    a = Account()
    a.balance = 52_100; a.eod_update()
    assert a.mll == 50_100                       # 52,100 - 2,000 = 50,100 (same number)
    a.balance = 52_150; a.eod_update()
    assert a.locked and a.mll == LOCKED_MLL
    a.balance = 55_000; a.eod_update()
    assert a.mll == LOCKED_MLL                   # stays locked


def test_intraday_highs_do_not_raise_mll():
    # +1500 intraday then giving back to +200 at close: MLL follows close, not the high
    a = Account()
    a.apply_trade(1_500, 0); a.apply_trade(-1_300, -1_300); a.eod_update()
    assert a.mll == 48_200


def test_breach_on_open_equity():
    a = Account()
    assert a.apply_trade(-100, -2_000) is False and a.breached


def test_consistency_25pct_pass():
    # Lucid example: $750 best day at $3,000 profit = 25% -> pass
    days = [[(750, 0)], [(750, 0)], [(750, 0)], [(750, 0)]]
    r = run_eval(days)
    assert r.passed and r.days == 4


def test_consistency_1560_needs_cushion():
    # $1,560 + $1,440 = $3,000 -> 52%: fails at 0.50, passes with Lucid's ~0.52 cushion
    days = [[(1_560, 0)], [(1_440, 0)], [(200, 0)]]
    assert run_eval(days, consistency=0.52).days == 2
    r = run_eval(days, consistency=0.50)
    assert r.passed and r.days == 3              # must keep trading until best day <= 50%


def test_eval_breach():
    r = run_eval([[(-1_000, -1_000)], [(-1_000, -1_000)]])
    assert r.breached and not r.passed


def test_funded_tiers():
    assert funded_tier_minis(0) == 2 and funded_tier_minis(999) == 2
    assert funded_tier_minis(1_000) == 3 and funded_tier_minis(2_000) == 4


def test_payout_mechanics():
    # 8 days of +$500 = $4,000 profit, 8 qualifying days -> payout $2,000, MLL -> 50,100
    days = [[(500, 0)]] * 8 + [[(0, 0)]]
    r = run_funded(days, payout_trigger_profit=4_000)
    assert r.payouts == [2_000.0] and r.payout_days == [8]
    assert abs(r.trader_cash - 1_800) < 1e-9


def test_post_payout_mll_is_50100():
    # after a $2,000 payout at $4,000 profit the cushion is 52,000 - 50,100 = 1,900
    days = [[(500, 0)]] * 8 + [[(-1_850, -1_850)]] + [[(-100, -100)]]
    r = run_funded(days, payout_trigger_profit=4_000)
    assert r.payouts == [2_000.0] and r.breached and r.days == 10


def test_min_payout_leaves_400_cushion():
    # payout at $1,000 profit -> $500 payout -> balance 50,500, only $400 above MLL
    days = [[(200, 0)]] * 5 + [[(-400, -400)]]
    r = run_funded(days, payout_trigger_profit=1_000)
    assert r.payouts == [500.0] and r.breached


def test_five_payouts_graduates():
    days = [[(800, 0)]] * 60
    r = run_funded(days, payout_trigger_profit=4_000)
    assert len(r.payouts) == 5 and r.graduated


if __name__ == "__main__":
    import sys
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
    print(f"{len(fns)} lucid_sim tests passed")
