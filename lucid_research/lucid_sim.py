"""LucidFlex 50K account state machine (rules snapshot: Lucid help center, Aug-Oct 2026).

Eval  : $2,000 EOD-trailing MLL, locks at $50,100 once an EOD close > $52,100. Pass at balance
        >= $53,000 with largest day <= CONSISTENCY x total profit. Max 4 minis / 40 micros.
Funded: same MLL. Scaling 2/3/4 minis at $0/$1,000/$2,000 profit (EOD). Payout needs 5 days of
        >= $150, amount = min(50% profit, $2,000), min $500; MLL -> $50,100 on request; 90/10 split;
        after payout 5 -> live review (we stop and count it as graduated).
Breach: intraday equity (realized + worst open excursion of the trade) touching the MLL (conservative).

Input is a list of trading days; each day is a list of trades in time order. A trade is a dict-like
tuple (pnl_usd_per_unit, mae_usd_per_unit, units) where the caller has already sized it — see
`size_day` helpers in harness.py. Keeping the account logic free of strategy code makes it testable.
"""
from dataclasses import dataclass, field

START = 50_000.0
MLL_AMT = 2_000.0
TRAIL_LOCK_TRIGGER = 52_100.0
LOCKED_MLL = 50_100.0
TARGET = 3_000.0
QUAL_DAY_MIN = 150.0
QUAL_DAYS = 5
PAYOUT_MIN = 500.0
PAYOUT_MAX = 2_000.0
SPLIT = 0.90
MAX_PAYOUTS = 5


def funded_tier_minis(profit):
    return 2 if profit < 1_000 else (3 if profit < 2_000 else 4)


@dataclass
class Account:
    balance: float = START
    peak_eod: float = START
    mll: float = START - MLL_AMT
    locked: bool = False
    breached: bool = False

    def eod_update(self):
        self.peak_eod = max(self.peak_eod, self.balance)
        if self.locked or self.peak_eod > TRAIL_LOCK_TRIGGER:
            self.locked = True
            self.mll = LOCKED_MLL
        else:
            self.mll = self.peak_eod - MLL_AMT

    def apply_trade(self, pnl, mae):
        """mae <= 0 is the worst open P&L of the trade (incl. entry costs); breach if equity touches MLL."""
        if self.balance + min(mae, pnl) <= self.mll:
            self.balance = self.mll          # account is closed at the limit
            self.breached = True
            return False
        self.balance += pnl
        if self.balance <= self.mll:
            self.breached = True
            return False
        return True


@dataclass
class EvalResult:
    passed: bool
    breached: bool
    days: int
    day_pnls: list = field(default_factory=list)


def run_eval(days, consistency=0.50, day_cap=None, day_stop=None, max_days=None):
    """days: iterable of lists of (pnl, mae) in $ (already sized). Returns EvalResult.
    day_cap / day_stop: self-imposed overlays — stop taking new trades once day P&L >= cap / <= -stop.
    The pass check runs after every trade (Lucid upgrades within minutes of hitting target)."""
    a = Account()
    day_pnls = []
    n = 0
    for trades in days:
        n += 1
        dp = 0.0
        day_open = a.balance
        for pnl, mae in trades:
            if day_cap is not None and dp >= day_cap:
                break
            if day_stop is not None and dp <= -day_stop:
                break
            if not a.apply_trade(pnl, mae):
                day_pnls.append(a.balance - day_open)
                return EvalResult(False, True, n, day_pnls)
            dp += pnl
            profit = a.balance - START
            best = max([dp] + day_pnls) if day_pnls else dp
            if a.balance >= START + TARGET and best <= consistency * profit + 1e-9:
                day_pnls.append(dp)
                return EvalResult(True, False, n, day_pnls)
        day_pnls.append(dp)
        a.eod_update()
        if max_days and n >= max_days:
            break
    return EvalResult(False, False, n, day_pnls)


@dataclass
class FundedResult:
    breached: bool
    days: int
    payouts: list = field(default_factory=list)      # gross payout amounts (before 90/10)
    payout_days: list = field(default_factory=list)
    graduated: bool = False

    @property
    def trader_cash(self):
        return SPLIT * sum(self.payouts)


def run_funded(days, payout_trigger_profit=4_000.0, day_cap=None, day_stop=None, max_days=None,
               tier_scale=None):
    """days: iterable of callables or lists. If tier_scale is given, each day's element is a list
    of (pnl, mae) sized for a 4-mini account and is scaled by tier_scale(minis_allowed)/1 — i.e. the
    caller passes the fraction of base risk allowed at the current tier. Simplest use: tier_scale=None
    (strategy risk is already far below the 2-mini cap, which is true for micro-sized $100-$300 risk)."""
    a = Account()
    qual = 0
    cycle_net = 0.0
    res = FundedResult(False, 0)
    n = 0
    for trades in days:
        n += 1
        scale = 1.0
        if tier_scale is not None:
            scale = tier_scale(funded_tier_minis(a.balance - START))
        dp = 0.0
        for pnl, mae in trades:
            if day_cap is not None and dp >= day_cap:
                break
            if day_stop is not None and dp <= -day_stop:
                break
            if not a.apply_trade(pnl * scale, mae * scale):
                res.breached = True
                res.days = n
                return res
            dp += pnl * scale
        cycle_net += dp
        if dp >= QUAL_DAY_MIN:
            qual += 1
        a.eod_update()
        profit = a.balance - START
        if qual >= QUAL_DAYS and cycle_net > 0 and profit >= payout_trigger_profit:
            amt = min(0.5 * profit, PAYOUT_MAX)
            if amt >= PAYOUT_MIN:
                a.balance -= amt
                a.locked = True
                a.mll = LOCKED_MLL
                a.peak_eod = a.balance
                res.payouts.append(amt)
                res.payout_days.append(n)
                qual = 0
                cycle_net = 0.0
                if len(res.payouts) >= MAX_PAYOUTS:
                    res.graduated = True
                    res.days = n
                    return res
        if max_days and n >= max_days:
            break
    res.days = n
    return res
