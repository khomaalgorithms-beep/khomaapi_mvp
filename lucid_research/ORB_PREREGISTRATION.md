# Opening-range (ORB) study — pre-registration (written 2026-10-12, before any ORB-lab run)

Data: FirstRate 1-min unadjusted continuous futures (roll-adjusted in harness), 2008-03 .. 2026-10.
Markets (micro-tradeable, liquid): NQ, ES, YM, RTY, GC, SI, CL, E6.
Costs: each market's spec in harness.SPEC (commission + 1 tick slippage per side; 2 ticks where noted).
Intrabar: entry bar judged by its close only; same-bar stop & target -> stop first.

## Periods
- SELECT: 2016-01-01 .. 2023-12-31 (all choices are made here only).
- BACKWARD check: 2008-03 .. 2015-12 (never used for selection).
- HOLDOUT: 2024-01-01 .. 2026-10 (locked; opened once, at the end, for the final systems).

## Stage 1 — base ORB per market (24 configs/market)
OR length n in {5, 15, 30, 60} min from the session open; entry in {touch: stop order 1 tick beyond the
range edge, close: bar CLOSES beyond the edge -> enter next bar open}; entries allowed for {60, 120, 240}
min after the range ends; first breakout only (one trade/day); stop = opposite side of the range;
exit at the session close. Rank by SELECT mean R (t-stat tie-break).

## Stage 2 — confluences (one at a time) on each market's top-3 Stage-1 configs
trend20, trend50 (trade only with the n-day trend); gap aligned (today's open vs prior close in the trade
direction); breakout also beyond the prior-day high/low; breakout also beyond the overnight high/low;
narrow OR (< 0.25 x ATR14); wide OR (> 0.5 x ATR14); NR7; inside day; high-vol regime (vol pct > 50%);
low-vol regime (< 50%); first 5-min bar direction aligned. A filter is KEPT only if it raises SELECT mean R
AND keeps >= 40 trades/year. Then the best 2 kept filters are tested together.

## Stage 3 — exits on each market's best Stage-2 system
stop in {OR opposite, OR mid, 0.1, 0.2, 0.3, 0.5 x ATR14}; target in {close, 1R, 1.5R, 2R, 3R};
breakeven at 1R {off, on}; trailing {off, chandelier trigger 1R / distance 1R}. Rank by SELECT total R
per unit of max drawdown (return/DD), tie-break PF.

## Stage 4 — hard tests (final 1-3 systems only)
A system PASSES only if ALL hold:
1. SELECT PF > 1.10 and t > 2.
2. BACKWARD (2008-15) PF > 1.0.
3. HOLDOUT (2024-26) PF > 1.0 (opened once).
4. Neighbourhood: >= 60% of adjacent settings (one parameter step away) have SELECT PF > 1.0.
5. Beats a random-direction baseline (same days/stops/exits) by > 2 sigma.
6. Survives 2x slippage with PF > 1.0 over 2016-26.
7. Deflated for the number of configurations tried (reported).
Then: LucidFlex 50K evaluation and funded probabilities (same simulator as the live product), and a
comparison against the live 10:30 momentum rule.

# Addendum — parameter optimization without overfitting (written 2026-10-12, before running)
Grid around the chosen NQ system: n in {3,5,10,15}; entry in {touch, close}; filter in {pdl, pdl+gap, pdl+onl};
stop in {0.10,0.15,0.20,0.25,0.30} x ATR14; breakeven at {off, 0.5, 0.75, 1.0, 1.5} R; target in {close, 3R, 5R}
(1,800 configurations). Metric = Sharpe proxy of daily R (all sessions, 0 on no-trade days).
1. PLATEAU score = mean metric of a config and all its one-step neighbours (a lone spike cannot win).
2. WALK-FORWARD: for each year Y = 2012..2026, choose the config with the best plateau score on ALL prior years
   (2008..Y-1) and trade year Y; concatenate. Compare with the fixed system (n5 touch pdl 0.2 BE1.0 close).
3. PBO by CSCV: 16 time blocks, all 8-vs-8 splits (12,870), on the full grid.
4. DECISION: change the production parameters ONLY if walk-forward beats the fixed system on Sharpe AND
   max drawdown; otherwise keep the fixed system and report the plateau as confirmation.
