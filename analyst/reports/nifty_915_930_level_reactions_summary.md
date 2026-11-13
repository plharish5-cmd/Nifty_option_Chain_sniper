=============================================================================================================================
   NIFTY 50 OPENING 15-MINUTE (09:15-09:30 IST) PRE-MARKET LEVEL REACTION & CONFLUENCE BACKTESTER
=============================================================================================================================
Session Coverage        : 1263 completed trading sessions (2021-10-06 to 2026-11-12)
Opening Trade Activity  : 830 sessions touched a pre-market level (65.7%)
Touch Tolerance Zone    : 5.0 Spot points
Breakout Invalidation SL: 20.0 Spot points beyond level
Confluence Proximity    : 15.0 Spot points
Lot Size & Cost Model   : Lot Size = 65 | ATM Delta = 0.55 | Total Charges + Slippage = ₹110.50 / trade
Break-Even Hurdle       : 36.5% Win Rate for 40pt Target vs 20pt SL
Ambiguity Guard         : 128 bars hit both 20pt SL and +25pt Target in the same minute
  - Pessimistic Win >=40: 192 (23.1%) [Breakout Fail counted first]
  - Optimistic Win >=40 : 254 (30.6%) [Favorable move counted first]

===========================================================================================================================================================================================================
                                                                       TABLE 1 — LEVEL-BY-LEVEL PERFORMANCE MATRIX (PHASE 1 BASELINE)                                                                      
===========================================================================================================================================================================================================
Level Name           | Approach   | Total Touches | Ambiguous Bars | Win >=25 Pts % | Win >=40 Pts % | Win >=50 Pts % | <25 Pts Stall % | Breakout Fail % | Avg MFE (Pts) | Avg MAE (Pts) | Est Net ₹/Trade
---------------------+------------+---------------+----------------+----------------+----------------+----------------+-----------------+-----------------+---------------+---------------+----------------
PDH                  | Resistance | 133           | 11             | 42.9           | 29.3           | 25.6           | 20.3            | 36.8            | 39.3          | 16.6          | 111.26         
PDH                  | Support    | 136           | 18             | 31.6           | 17.6           | 8.1            | 9.6             | 58.8            | 34.9          | 21.8          | -214.06        
PDL                  | Resistance | 65            | 8              | 23.1           | 20.0           | 16.9           | 16.9            | 60.0            | 39.3          | 29.3          | -228.39        
PDL                  | Support    | 149           | 32             | 24.2           | 20.1           | 16.1           | 12.1            | 63.8            | 42.4          | 26.9          | -234.01        
PDC                  | Resistance | 165           | 29             | 35.8           | 29.7           | 22.4           | 8.5             | 55.8            | 44.6          | 23.5          | -56.89         
PDC                  | Support    | 262           | 31             | 36.6           | 26.3           | 17.2           | 12.6            | 50.8            | 37.2          | 21.1          | -61.38         
PDO                  | Resistance | 101           | 9              | 38.6           | 28.7           | 21.8           | 15.8            | 45.5            | 40.5          | 19.0          | 49.74          
PDO                  | Support    | 114           | 19             | 38.6           | 25.4           | 20.2           | 11.4            | 50.0            | 42.8          | 20.0          | -25.96         
PREV_1H_8EMA         | Resistance | 153           | 16             | 36.6           | 26.8           | 22.9           | 13.7            | 49.7            | 41.9          | 20.0          | -29.55         
PREV_1H_8EMA         | Support    | 202           | 31             | 35.1           | 21.8           | 17.8           | 9.4             | 55.4            | 38.7          | 23.1          | -160.06        
DOUBLE_BOTTOM_3D     | Support    | 120           | 24             | 25.8           | 15.8           | 11.7           | 10.8            | 63.3            | 38.3          | 25.2          | -283.77        
DOUBLE_TOP_3D        | Resistance | 95            | 9              | 46.3           | 29.5           | 23.2           | 17.9            | 35.8            | 38.5          | 14.4          | 140.86         
ALL_LEVELS_AGGREGATE | Combined   | 1695          | 237            | 34.9           | 24.4           | 18.5           | 12.7            | 52.4            | 39.8          | 21.7          | -85.52         
===========================================================================================================================================================================================================


=========================================================================================================================================================================================================================
                                                                            TABLE 2 — MULTI-FACTOR CONFLUENCE COMBINATIONS (PHASE 1 BASELINE)                                                                            
=========================================================================================================================================================================================================================
Confluence Setup / Tier                                        | Direction | Touches | Win >=25 Pts % | Win >=40 Pts % | Win >=50 Pts % | Stall <25 % | Breakout Fail % | Avg MFE (Pts) | Avg MAE (Pts) | Est Net ₹/Trade
---------------------------------------------------------------+-----------+---------+----------------+----------------+----------------+-------------+-----------------+---------------+---------------+----------------
BELOW_1H_8EMA + Touch PDH                                      | SHORT     | 21      | 38.1           | 23.8           | 23.8           | 28.6        | 33.3            | 39.3          | 14.7          | 143.24         
BELOW_1H_8EMA + Touch PDC                                      | SHORT     | 121     | 33.9           | 28.1           | 21.5           | 9.9         | 56.2            | 45.4          | 23.9          | -75.9          
ABOVE_1H_8EMA + Touch PDL + DOUBLE_BOTTOM_3D                   | LONG      | 20      | 20.0           | 20.0           | 20.0           | 25.0        | 55.0            | 42.0          | 32.6          | -234.46        
ABOVE_1H_8EMA + Touch PREV_1H_8EMA Support + PDL within 15 pts | LONG      | 5       | 40.0           | 20.0           | 20.0           | 20.0        | 40.0            | 34.3          | 11.9          | 150.5          
Single Level Touch (PDO / PDC alone with 0 confluence)         | BASELINE  | 110     | 30.9           | 23.6           | 19.1           | 12.7        | 56.4            | 42.0          | 22.2          | -121.36        
BELOW_1H_8EMA + Touch PREV_1H_8EMA Resistance                  | SHORT     | 153     | 36.6           | 26.8           | 22.9           | 13.7        | 49.7            | 41.9          | 20.0          | -29.55         
ABOVE_1H_8EMA + Touch PREV_1H_8EMA Support                     | LONG      | 202     | 35.1           | 21.8           | 17.8           | 9.4         | 55.4            | 38.7          | 23.1          | -160.06        
Touch PDH + DOUBLE_TOP_3D                                      | SHORT     | 109     | 44.0           | 30.3           | 25.7           | 21.1        | 34.9            | 38.2          | 16.3          | 125.4          
Touch PDL + DOUBLE_BOTTOM_3D                                   | LONG      | 110     | 21.8           | 18.2           | 15.5           | 11.8        | 66.4            | 40.7          | 27.3          | -300.06        
== Tier 1: Single Level (1 Factor) ==                          | ALL       | 127     | 29.9           | 22.0           | 18.1           | 5.5         | 64.6            | 40.1          | 27.2          | -217.95        
== Tier 2: Double Confluence (2 Factors) ==                    | ALL       | 365     | 34.5           | 26.3           | 21.9           | 6.3         | 59.2            | 40.1          | 24.4          | -143.27        
== Tier 3: Triple+ Confluence (3+ Factors) ==                  | ALL       | 338     | 30.5           | 20.1           | 12.4           | 11.2        | 58.3            | 35.8          | 23.3          | -182.91        
=========================================================================================================================================================================================================================


====================================================================================================================================================================================================================
                                                          TABLE 4 — PHASE 2 HEAD-TO-HEAD COMPARISON (BLIND TOUCH vs. WICK CONFIRMATION vs. PDL BREAKDOWN)                                                           
====================================================================================================================================================================================================================
Strategy Variant                                                 | N   | Win >=25 Pts % | Win >=40 Pts % | Breakout Fail % | Avg Risk (Pts) | Avg MFE (Pts) | Net ₹ (25pt Tgt) | Net ₹ (40pt Tgt) | t-stat (40pt BE)
-----------------------------------------------------------------+-----+----------------+----------------+-----------------+----------------+---------------+------------------+------------------+-----------------
1a. PDH Short (Phase 1 Blind Touch)                              | 263 | 43.0           | 29.3           | 35.7            | 21.1           | 38.8          | 16.65            | 119.67           | -2.43           
1b. PDH Short (Phase 2 Confirmed 1M Rejection Close)             | 105 | 38.1           | 19.0           | 18.1            | 37.1           | 25.2          | -44.48           | -80.96           | -3.71           
2a. Touch PDH + DOUBLE_TOP_3D (Phase 1 Blind Touch)              | 109 | 44.0           | 30.3           | 34.9            | 21.0           | 38.2          | 24.0             | 125.4            | -1.35           
2b. Touch PDH + DOUBLE_TOP_3D (Phase 2 Wick Confirmed)           | 87  | 36.8           | 18.4           | 18.4            | 35.8           | 23.5          | -101.48          | -100.06          | -3.51           
3a. PDL Support Bounce (Phase 1 Blind Touch - Failed 63.8%)      | 279 | 22.9           | 19.4           | 64.2            | 20.8           | 41.7          | -359.48          | -260.08          | -5.95           
3b. PDL Breakdown Short (Phase 2 All Regimes)                    | 111 | 36.9           | 24.3           | 39.6            | 36.4           | 33.7          | -176.36          | -95.01           | -2.66           
4a. BELOW_1H_8EMA + PDL Support Bounce (Phase 1 Blind Touch)     | 279 | 22.9           | 19.4           | 64.2            | 20.8           | 41.7          | -359.48          | -260.08          | -5.95           
4b. BELOW_1H_8EMA + PDL Breakdown Short (Phase 2 Regime-Aligned) | 97  | 38.1           | 23.7           | 42.3            | 34.3           | 32.4          | -206.64          | -135.65          | -2.62           
5a. Touch PDL + DOUBLE_BOTTOM_3D Long (Phase 1 Blind Touch)      | 110 | 21.8           | 18.2           | 66.4            | 20.7           | 40.7          | -391.32          | -300.06          | -3.99           
5b. Touch PDL + DOUBLE_BOTTOM_3D Breakdown Short (Phase 2)       | 82  | 36.6           | 24.4           | 36.6            | 36.7           | 33.2          | -145.97          | -63.13           | -2.28           
====================================================================================================================================================================================================================


===========================================================================================================================================================================================================================================
                                                                          TABLE 5 — PHASE 3 HORIZON (09:30 vs 09:45 vs 10:00) & BREAKEVEN-TRAIL EXPECTANCY MATRIX                                                                          
===========================================================================================================================================================================================================================================
Variant                                          | Hold Until | Sample (N) | Win >= 25 Pts % | Win >= 40 Pts % | Win >= 50 Pts % | BE Scratch % | SL Fail % | Avg Risk (Pts) | Est Net ₹/Lot (25pt Tgt) | Est Net ₹/Lot (40pt Tgt) | t-stat
-------------------------------------------------+------------+------------+-----------------+-----------------+-----------------+--------------+-----------+----------------+--------------------------+--------------------------+-------
1. PDH Short (Blind Touch)                       | 09:30 IST  | 133        | 55.6            | 34.6            | 27.1            | 30.8         | 25.6      | 21.1           | 265.47                   | 271.85                   | 3.66  
1. PDH Short (Blind Touch)                       | 09:45 IST  | 133        | 63.2            | 39.1            | 30.1            | 40.6         | 28.6      | 21.1           | 297.33                   | 302.48                   | 3.9   
1. PDH Short (Blind Touch)                       | 10:00 IST  | 133        | 63.2            | 41.4            | 31.6            | 46.6         | 29.3      | 21.1           | 300.44                   | 307.94                   | 3.89  
2. Touch PDH + DOUBLE_TOP_3D                     | 09:30 IST  | 109        | 56.9            | 35.8            | 26.6            | 32.1         | 22.9      | 21.0           | 285.34                   | 301.71                   | 3.74  
2. Touch PDH + DOUBLE_TOP_3D                     | 09:45 IST  | 109        | 63.3            | 39.4            | 28.4            | 43.1         | 26.6      | 21.0           | 311.5                    | 316.29                   | 3.76  
2. Touch PDH + DOUBLE_TOP_3D                     | 10:00 IST  | 109        | 63.3            | 41.3            | 29.4            | 47.7         | 27.5      | 21.0           | 315.28                   | 329.35                   | 3.83  
3. PDH Short (P3-A Max Risk <=22pts)             | 09:30 IST  | 67         | 28.4            | 11.9            | 6.0             | 14.9         | 35.8      | 16.4           | -59.12                   | -60.61                   | -0.71 
3. PDH Short (P3-A Max Risk <=22pts)             | 09:45 IST  | 67         | 41.8            | 20.9            | 14.9            | 26.9         | 44.8      | 16.4           | 4.19                     | -26.25                   | -0.27 
3. PDH Short (P3-A Max Risk <=22pts)             | 10:00 IST  | 67         | 41.8            | 25.4            | 19.4            | 34.3         | 44.8      | 16.4           | 1.31                     | -9.36                    | -0.09 
4. PDH Short (P3-B 50% Wick Retest Limit)        | 09:30 IST  | 66         | 25.8            | 12.1            | 4.5             | 13.6         | 48.5      | 11.0           | -27.7                    | -10.04                   | -0.13 
4. PDH Short (P3-B 50% Wick Retest Limit)        | 09:45 IST  | 66         | 33.3            | 18.2            | 12.1            | 19.7         | 57.6      | 11.0           | -6.96                    | -2.81                    | -0.03 
4. PDH Short (P3-B 50% Wick Retest Limit)        | 10:00 IST  | 66         | 33.3            | 22.7            | 15.2            | 24.2         | 59.1      | 11.0           | -13.94                   | 3.39                     | 0.04  
5. PDL Breakdown Short (50% Retest/<=10pt Close) | 09:30 IST  | 109        | 42.2            | 21.1            | 15.6            | 20.2         | 33.0      | 32.4           | 40.62                    | 49.68                    | 0.58  
5. PDL Breakdown Short (50% Retest/<=10pt Close) | 09:45 IST  | 109        | 48.6            | 31.2            | 22.9            | 30.3         | 39.4      | 32.4           | 14.24                    | 56.41                    | 0.61  
5. PDL Breakdown Short (50% Retest/<=10pt Close) | 10:00 IST  | 109        | 49.5            | 33.0            | 25.7            | 34.9         | 39.4      | 32.4           | 13.54                    | 63.57                    | 0.66  
===========================================================================================================================================================================================================================================

=============================================================================================================================
                    STATISTICAL EDGE VERDICT & SETUP RANKINGS (PHASE 1 & 2)
=============================================================================================================================

[RANK #1 TOP CONFLUENCE SETUP]: Touch PDH + DOUBLE_TOP_3D (SHORT)
  - Sample Size (N)   : 109 [ENOUGH_SAMPLE (N >= 30)]
  - Win >= 40 Pts Rate: 30.28% (33/109)
  - Win >= 25 Pts Rate: 44.04% (48/109)
  - Breakout Fail Rate: 34.86% (38/109)
  - t-stat vs 36.5% BE: -1.350 (p-value = 0.9115)
  - Est Net PnL (40pt): ₹+125.40 / lot (t_pnl = +1.43)
  - Est Net PnL (25pt): ₹+24.00 / lot

[RANK #2 TOP CONFLUENCE SETUP]: BELOW_1H_8EMA + Touch PDC (SHORT)
  - Sample Size (N)   : 121 [ENOUGH_SAMPLE (N >= 30)]
  - Win >= 40 Pts Rate: 28.1% (34/121)
  - Win >= 25 Pts Rate: 33.88% (41/121)
  - Breakout Fail Rate: 56.2% (68/121)
  - t-stat vs 36.5% BE: -1.919 (p-value = 0.9725)
  - Est Net PnL (40pt): ₹-75.90 / lot (t_pnl = -0.87)
  - Est Net PnL (25pt): ₹-213.14 / lot

[CONTROL BASELINE]: Single Level Touch (PDO / PDC alone with 0 confluence)
  - Sample Size (N)   : 110
  - Win >= 40 Pts Rate: 23.64% | Fail Rate: 56.36%
  - Est Net PnL/Trade : ₹-121.36 / lot

=============================================================================================================================
                PHASE 3 STATISTICAL EDGE VERDICT (t-stat >= 2.5 & Est Net ₹/Lot >= +₹250)
=============================================================================================================================

[QUALIFYING SETUPS ACHIEVING t-stat >= 2.5 AND Est Net ₹ >= +₹250/lot]:

* 1. PDH Short (Blind Touch) | Hold: 09:30 IST (40pt Target):
  - Sample Size (N)       : 133
  - Win >= 40 Pts Rate    : 34.6%
  - Win >= 25 Pts Rate    : 55.6%
  - Breakeven Scratch Rate: 30.8%
  - Initial SL Fail Rate  : 25.6%
  - Est Net ₹/Lot         : ₹+271.85
  - t-statistic           : +3.66

* 1. PDH Short (Blind Touch) | Hold: 09:45 IST (40pt Target):
  - Sample Size (N)       : 133
  - Win >= 40 Pts Rate    : 39.1%
  - Win >= 25 Pts Rate    : 63.2%
  - Breakeven Scratch Rate: 40.6%
  - Initial SL Fail Rate  : 28.6%
  - Est Net ₹/Lot         : ₹+302.48
  - t-statistic           : +3.90

* 1. PDH Short (Blind Touch) | Hold: 10:00 IST (40pt Target):
  - Sample Size (N)       : 133
  - Win >= 40 Pts Rate    : 41.4%
  - Win >= 25 Pts Rate    : 63.2%
  - Breakeven Scratch Rate: 46.6%
  - Initial SL Fail Rate  : 29.3%
  - Est Net ₹/Lot         : ₹+307.94
  - t-statistic           : +3.89

* 2. Touch PDH + DOUBLE_TOP_3D | Hold: 09:30 IST (40pt Target):
  - Sample Size (N)       : 109
  - Win >= 40 Pts Rate    : 35.8%
  - Win >= 25 Pts Rate    : 56.9%
  - Breakeven Scratch Rate: 32.1%
  - Initial SL Fail Rate  : 22.9%
  - Est Net ₹/Lot         : ₹+301.71
  - t-statistic           : +3.74

* 2. Touch PDH + DOUBLE_TOP_3D | Hold: 09:45 IST (40pt Target):
  - Sample Size (N)       : 109
  - Win >= 40 Pts Rate    : 39.4%
  - Win >= 25 Pts Rate    : 63.3%
  - Breakeven Scratch Rate: 43.1%
  - Initial SL Fail Rate  : 26.6%
  - Est Net ₹/Lot         : ₹+316.29
  - t-statistic           : +3.76

* 2. Touch PDH + DOUBLE_TOP_3D | Hold: 10:00 IST (40pt Target):
  - Sample Size (N)       : 109
  - Win >= 40 Pts Rate    : 41.3%
  - Win >= 25 Pts Rate    : 63.3%
  - Breakeven Scratch Rate: 47.7%
  - Initial SL Fail Rate  : 27.5%
  - Est Net ₹/Lot         : ₹+329.35
  - t-statistic           : +3.83

=============================================================================================================================

