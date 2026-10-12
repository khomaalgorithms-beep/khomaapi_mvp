# Holdout pre-registration (written 2026-10-11, BEFORE any 2024-2026 statistic was viewed)

Search: 26,883 trials (27 families x params x 25 Lucid-tradable futures), 2008-2023 only.
Forward fold (discover 2008-19 -> validate 2020-23): 60 first-gate survivors, 0 pass Holm.
Reverse fold (discover 2020-23 -> validate 2008-19): 0 pass Holm; discoveries are dominated by one
family — NQ intraday momentum — which is positive in 100% of its 256 variants in 2020-23 (median
+0.075R) but only 18% in 2008-19. Interpretation (hypothesis): a regime edge driven by dealer-gamma /
leveraged-ETF / 0DTE hedging flows concentrated in mega-cap tech since ~2017.

## Primary hypothesis (one spec, fixed now)
Spec A, NQ (traded as MNQ): at 10:30 ET, side = sign(price at 10:29 close - 09:30 open); take it only if it
agrees with the 20-day trend (prior close vs 20-day SMA, roll-free series); market entry at the 10:30 bar
open; stop = 0.3 x ATR14 (main-session range); exit at 16:00 (no target, no trail). One trade per day.
Costs: $1.00 RT commission per MNQ + 1 tick slippage per side (stop exits fill through gaps).

PASS on holdout (2024-01-01 .. 2026-10-09) iff: mean net R > 0 AND t >= 1.65 (one-sided 5%) AND
profit factor > 1.10.

## Secondary (family-level, guards against having picked a lucky spec)
Share of all 256 NQ momentum variants (tod_mom + noise_area grids) with positive holdout mean R.
Expect > 70% if the regime edge persists.

## What would kill it
Holdout mean R <= 0, or edge present only at 1x slippage (negative at 2x), or PF < 1.10.
