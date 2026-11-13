=============================================================================================================================
   NIFTY 50 OPENING 15-MINUTE (09:15-09:30 IST) PRE-MARKET LEVEL REACTION & CONFLUENCE BACKTESTER
=============================================================================================================================
Session Coverage        : 1264 completed trading sessions (2021-10-06 to 2026-11-13)
Opening Trade Activity  : 831 sessions touched a pre-market level (65.7%)
Touch Tolerance Zone    : 5.0 Spot points
Breakout Invalidation SL: 20.0 Spot points beyond level
Confluence Proximity    : 15.0 Spot points
Lot Size & Cost Model   : Lot Size = 65 | ATM Delta = 0.55 | Total Charges + Slippage = ₹110.50 / trade
Break-Even Hurdle       : 36.5% Win Rate for 40pt Target vs 20pt SL
Ambiguity Guard         : 129 bars hit both 20pt SL and +25pt Target in the same minute
  - Pessimistic Win >=40: 192 (23.1%) [Breakout Fail counted first]
  - Optimistic Win >=40 : 255 (30.7%) [Favorable move counted first]

===========================================================================================================================================================================================================
                                                                       TABLE 1 — LEVEL-BY-LEVEL PERFORMANCE MATRIX (PHASE 1 BASELINE)                                                                      
===========================================================================================================================================================================================================
Level Name           | Approach   | Total Touches | Ambiguous Bars | Win >=25 Pts % | Win >=40 Pts % | Win >=50 Pts % | <25 Pts Stall % | Breakout Fail % | Avg MFE (Pts) | Avg MAE (Pts) | Est Net ₹/Trade
---------------------+------------+---------------+----------------+----------------+----------------+----------------+-----------------+-----------------+---------------+---------------+----------------
PDH                  | Resistance | 133           | 11             | 42.9           | 29.3           | 25.6           | 20.3            | 36.8            | 39.3          | 16.6          | 111.26         
PDH                  | Support    | 136           | 18             | 31.6           | 17.6           | 8.1            | 9.6             | 58.8            | 34.9          | 21.8          | -214.06        
PDL                  | Resistance | 65            | 8              | 23.1           | 20.0           | 16.9           | 16.9            | 60.0            | 39.3          | 29.3          | -228.39        
PDL                  | Support    | 150           | 33             | 24.0           | 20.0           | 16.0           | 12.0            | 64.0            | 42.5          | 26.9          | -237.95        
PDC                  | Resistance | 165           | 29             | 35.8           | 29.7           | 22.4           | 8.5             | 55.8            | 44.6          | 23.5          | -56.89         
PDC                  | Support    | 262           | 31             | 36.6           | 26.3           | 17.2           | 12.6            | 50.8            | 37.2          | 21.1          | -61.38         
PDO                  | Resistance | 101           | 9              | 38.6           | 28.7           | 21.8           | 15.8            | 45.5            | 40.5          | 19.0          | 49.74          
PDO                  | Support    | 114           | 19             | 38.6           | 25.4           | 20.2           | 11.4            | 50.0            | 42.8          | 20.0          | -25.96         
PREV_1H_8EMA         | Resistance | 153           | 16             | 36.6           | 26.8           | 22.9           | 13.7            | 49.7            | 41.9          | 20.0          | -29.55         
PREV_1H_8EMA         | Support    | 202           | 31             | 35.1           | 21.8           | 17.8           | 9.4             | 55.4            | 38.7          | 23.1          | -160.06        
DOUBLE_BOTTOM_3D     | Support    | 120           | 24             | 25.8           | 15.8           | 11.7           | 10.8            | 63.3            | 38.3          | 25.2          | -283.77        
DOUBLE_TOP_3D        | Resistance | 95            | 9              | 46.3           | 29.5           | 23.2           | 17.9            | 35.8            | 38.5          | 14.4          | 140.86         
ALL_LEVELS_AGGREGATE | Combined   | 1696          | 238            | 34.8           | 24.4           | 18.5           | 12.7            | 52.5            | 39.8          | 21.7          | -85.96         
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
== Tier 1: Single Level (1 Factor) ==                          | ALL       | 128     | 29.7           | 21.9           | 18.0           | 5.5         | 64.8            | 40.3          | 27.1          | -222.69        
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
3a. PDL Support Bounce (Phase 1 Blind Touch - Failed 63.8%)      | 280 | 22.9           | 19.3           | 64.3            | 20.8           | 41.7          | -361.15          | -262.1           | -5.98           
3b. PDL Breakdown Short (Phase 2 All Regimes)                    | 112 | 37.5           | 25.0           | 39.3            | 36.2           | 33.9          | -167.8           | -82.38           | -2.53           
4a. BELOW_1H_8EMA + PDL Support Bounce (Phase 1 Blind Touch)     | 280 | 22.9           | 19.3           | 64.3            | 20.8           | 41.7          | -361.15          | -262.1           | -5.98           
4b. BELOW_1H_8EMA + PDL Breakdown Short (Phase 2 Regime-Aligned) | 98  | 38.8           | 24.5           | 41.8            | 34.1           | 32.7          | -196.54          | -120.81          | -2.47           
5a. Touch PDL + DOUBLE_BOTTOM_3D Long (Phase 1 Blind Touch)      | 110 | 21.8           | 18.2           | 66.4            | 20.7           | 40.7          | -391.32          | -300.06          | -3.99           
5b. Touch PDL + DOUBLE_BOTTOM_3D Breakdown Short (Phase 2)       | 82  | 36.6           | 24.4           | 36.6            | 36.7           | 33.2          | -145.97          | -63.13           | -2.28           
====================================================================================================================================================================================================================


==================================================================================================================================================================================================================================
                                                                             TABLE 5B — PHASE 3B MUTUALLY EXCLUSIVE (100.0%-SUM) CAUSAL AUDIT MATRIX                                                                              
==================================================================================================================================================================================================================================
Variant                                          | Target                          | Hold Until | N   | Target Hit % | BE Scratch / Profit-Lock % | SL Fail % | Time Exit % | Sum % (Must = 100.0%) | True Net ₹/Lot | True t-stat
-------------------------------------------------+---------------------------------+------------+-----+--------------+----------------------------+-----------+-------------+-----------------------+----------------+------------
1. PDH Short (Blind Touch)                       | 25pt Target                     | 09:30 IST  | 133 | 42.9         | 3.0                        | 33.8      | 20.3        | 100.0                 | 32.36          | 0.51       
1. PDH Short (Blind Touch)                       | 25pt Target                     | 09:45 IST  | 133 | 50.4         | 6.0                        | 36.8      | 6.8         | 100.0                 | 65.28          | 0.99       
1. PDH Short (Blind Touch)                       | 25pt Target                     | 10:00 IST  | 133 | 50.4         | 6.0                        | 38.3      | 5.3         | 100.0                 | 63.6           | 0.95       
1. PDH Short (Blind Touch)                       | 40pt Target                     | 09:30 IST  | 133 | 29.3         | 9.0                        | 33.8      | 27.9        | 100.0                 | 118.15         | 1.53       
1. PDH Short (Blind Touch)                       | 40pt Target                     | 09:45 IST  | 133 | 35.3         | 15.8                       | 36.8      | 12.1        | 100.0                 | 157.79         | 1.93       
1. PDH Short (Blind Touch)                       | 40pt Target                     | 10:00 IST  | 133 | 37.6         | 18.0                       | 38.3      | 6.1         | 100.0                 | 158.46         | 1.9        
1. PDH Short (Blind Touch)                       | Partial (25pt/40pt Profit-Lock) | 09:30 IST  | 133 | 24.1         | 18.8                       | 33.8      | 23.3        | 100.0                 | 68.43          | 1.01       
1. PDH Short (Blind Touch)                       | Partial (25pt/40pt Profit-Lock) | 09:45 IST  | 133 | 27.1         | 27.8                       | 36.8      | 8.3         | 100.0                 | 100.28         | 1.43       
1. PDH Short (Blind Touch)                       | Partial (25pt/40pt Profit-Lock) | 10:00 IST  | 133 | 28.6         | 27.8                       | 38.3      | 5.3         | 100.0                 | 101.23         | 1.42       
2. Touch PDH + DOUBLE_TOP_3D                     | 25pt Target                     | 09:30 IST  | 109 | 44.0         | 2.8                        | 32.1      | 21.1        | 100.0                 | 45.19          | 0.65       
2. Touch PDH + DOUBLE_TOP_3D                     | 25pt Target                     | 09:45 IST  | 109 | 50.5         | 5.5                        | 35.8      | 8.2         | 100.0                 | 72.63          | 1.0        
2. Touch PDH + DOUBLE_TOP_3D                     | 25pt Target                     | 10:00 IST  | 109 | 50.5         | 5.5                        | 37.6      | 6.4         | 100.0                 | 70.58          | 0.96       
2. Touch PDH + DOUBLE_TOP_3D                     | 40pt Target                     | 09:30 IST  | 109 | 31.2         | 7.3                        | 32.1      | 29.4        | 100.0                 | 146.96         | 1.71       
2. Touch PDH + DOUBLE_TOP_3D                     | 40pt Target                     | 09:45 IST  | 109 | 36.7         | 14.7                       | 35.8      | 12.8        | 100.0                 | 172.53         | 1.91       
2. Touch PDH + DOUBLE_TOP_3D                     | 40pt Target                     | 10:00 IST  | 109 | 38.5         | 16.5                       | 37.6      | 7.4         | 100.0                 | 179.76         | 1.94       
2. Touch PDH + DOUBLE_TOP_3D                     | Partial (25pt/40pt Profit-Lock) | 09:30 IST  | 109 | 24.8         | 19.3                       | 32.1      | 23.8        | 100.0                 | 80.36          | 1.08       
2. Touch PDH + DOUBLE_TOP_3D                     | Partial (25pt/40pt Profit-Lock) | 09:45 IST  | 109 | 26.6         | 28.4                       | 35.8      | 9.2         | 100.0                 | 103.06         | 1.35       
2. Touch PDH + DOUBLE_TOP_3D                     | Partial (25pt/40pt Profit-Lock) | 10:00 IST  | 109 | 27.5         | 28.4                       | 37.6      | 6.5         | 100.0                 | 103.38         | 1.32       
3. PDH Short (P3-A Max Risk <=22pts)             | 25pt Target                     | 09:30 IST  | 67  | 28.4         | 6.0                        | 35.8      | 29.8        | 100.0                 | -59.12         | -0.78      
3. PDH Short (P3-A Max Risk <=22pts)             | 25pt Target                     | 09:45 IST  | 67  | 41.8         | 7.5                        | 44.8      | 5.9         | 100.0                 | 4.19           | 0.05       
3. PDH Short (P3-A Max Risk <=22pts)             | 25pt Target                     | 10:00 IST  | 67  | 41.8         | 9.0                        | 44.8      | 4.4         | 100.0                 | 1.31           | 0.02       
3. PDH Short (P3-A Max Risk <=22pts)             | 40pt Target                     | 09:30 IST  | 67  | 11.9         | 11.9                       | 35.8      | 40.4        | 100.0                 | -60.61         | -0.71      
3. PDH Short (P3-A Max Risk <=22pts)             | 40pt Target                     | 09:45 IST  | 67  | 20.9         | 20.9                       | 44.8      | 13.4        | 100.0                 | -26.25         | -0.27      
3. PDH Short (P3-A Max Risk <=22pts)             | 40pt Target                     | 10:00 IST  | 67  | 25.4         | 25.4                       | 44.8      | 4.4         | 100.0                 | -9.36          | -0.09      
3. PDH Short (P3-A Max Risk <=22pts)             | Partial (25pt/40pt Profit-Lock) | 09:30 IST  | 67  | 10.4         | 20.9                       | 35.8      | 32.9        | 100.0                 | -59.22         | -0.77      
3. PDH Short (P3-A Max Risk <=22pts)             | Partial (25pt/40pt Profit-Lock) | 09:45 IST  | 67  | 14.9         | 31.3                       | 44.8      | 9.0         | 100.0                 | 2.23           | 0.03       
3. PDH Short (P3-A Max Risk <=22pts)             | Partial (25pt/40pt Profit-Lock) | 10:00 IST  | 67  | 16.4         | 34.3                       | 44.8      | 4.5         | 100.0                 | -0.02          | -0.0       
4. PDH Short (P3-B 50% Wick Retest Limit)        | 25pt Target                     | 09:30 IST  | 66  | 28.8         | 3.0                        | 50.0      | 18.2        | 100.0                 | -18.9          | -0.27      
4. PDH Short (P3-B 50% Wick Retest Limit)        | 25pt Target                     | 09:45 IST  | 66  | 36.4         | 3.0                        | 59.1      | 1.5         | 100.0                 | 1.85           | 0.02       
4. PDH Short (P3-B 50% Wick Retest Limit)        | 25pt Target                     | 10:00 IST  | 66  | 36.4         | 3.0                        | 60.6      | 0.0         | 100.0                 | -5.14          | -0.07      
4. PDH Short (P3-B 50% Wick Retest Limit)        | 40pt Target                     | 09:30 IST  | 66  | 15.2         | 6.1                        | 50.0      | 28.7        | 100.0                 | 24.76          | 0.29       
4. PDH Short (P3-B 50% Wick Retest Limit)        | 40pt Target                     | 09:45 IST  | 66  | 21.2         | 9.1                        | 59.1      | 10.6        | 100.0                 | 31.99          | 0.35       
4. PDH Short (P3-B 50% Wick Retest Limit)        | 40pt Target                     | 10:00 IST  | 66  | 25.8         | 13.6                       | 60.6      | 0.0         | 100.0                 | 38.19          | 0.4        
4. PDH Short (P3-B 50% Wick Retest Limit)        | Partial (25pt/40pt Profit-Lock) | 09:30 IST  | 66  | 9.1          | 18.2                       | 50.0      | 22.7        | 100.0                 | -18.27         | -0.26      
4. PDH Short (P3-B 50% Wick Retest Limit)        | Partial (25pt/40pt Profit-Lock) | 09:45 IST  | 66  | 12.1         | 25.8                       | 59.1      | 3.0         | 100.0                 | -3.87          | -0.05      
4. PDH Short (P3-B 50% Wick Retest Limit)        | Partial (25pt/40pt Profit-Lock) | 10:00 IST  | 66  | 13.6         | 25.8                       | 60.6      | 0.0         | 100.0                 | -9.2           | -0.12      
5. PDL Breakdown Short (50% Retest/<=10pt Close) | 25pt Target                     | 09:30 IST  | 110 | 39.1         | 2.7                        | 35.5      | 22.7        | 100.0                 | -36.75         | -0.44      
5. PDL Breakdown Short (50% Retest/<=10pt Close) | 25pt Target                     | 09:45 IST  | 110 | 45.5         | 8.2                        | 42.7      | 3.6         | 100.0                 | -73.21         | -0.81      
5. PDL Breakdown Short (50% Retest/<=10pt Close) | 25pt Target                     | 10:00 IST  | 110 | 46.4         | 8.2                        | 42.7      | 2.7         | 100.0                 | -73.9          | -0.8       
5. PDL Breakdown Short (50% Retest/<=10pt Close) | 40pt Target                     | 09:30 IST  | 110 | 25.5         | 7.3                        | 35.5      | 31.7        | 100.0                 | 63.02          | 0.65       
5. PDL Breakdown Short (50% Retest/<=10pt Close) | 40pt Target                     | 09:45 IST  | 110 | 35.5         | 13.6                       | 42.7      | 8.2         | 100.0                 | 59.37          | 0.56       
5. PDL Breakdown Short (50% Retest/<=10pt Close) | 40pt Target                     | 10:00 IST  | 110 | 37.3         | 14.5                       | 42.7      | 5.5         | 100.0                 | 66.46          | 0.61       
5. PDL Breakdown Short (50% Retest/<=10pt Close) | Partial (25pt/40pt Profit-Lock) | 09:30 IST  | 110 | 20.9         | 13.6                       | 35.5      | 30.0        | 100.0                 | 1.88           | 0.02       
5. PDL Breakdown Short (50% Retest/<=10pt Close) | Partial (25pt/40pt Profit-Lock) | 09:45 IST  | 110 | 27.3         | 25.5                       | 42.7      | 4.5         | 100.0                 | -31.42         | -0.33      
5. PDL Breakdown Short (50% Retest/<=10pt Close) | Partial (25pt/40pt Profit-Lock) | 10:00 IST  | 110 | 28.2         | 26.4                       | 42.7      | 2.7         | 100.0                 | -30.84         | -0.32      
==================================================================================================================================================================================================================================

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
         PHASE 3B STATISTICAL EDGE VERDICT (True t-stat >= 2.5 & True Net ₹/Lot >= +₹250)
=============================================================================================================================

[NOTICE]: No individual Phase 3 configuration met both True t-stat >= 2.5 AND True Net ₹ >= +₹250/lot hurdles.

Top performing Phase 3 configurations ranked by True Net ₹/Lot:
  - 2. Touch PDH + DOUBLE_TOP_3D [40pt Target | 10:00 IST]: Net ₹+179.76 / lot (t-stat = +1.94, Hit = 38.5%, SL Fail = 37.6%, Sum = 100.0%)
  - 2. Touch PDH + DOUBLE_TOP_3D [40pt Target | 09:45 IST]: Net ₹+172.53 / lot (t-stat = +1.91, Hit = 36.7%, SL Fail = 35.8%, Sum = 100.0%)
  - 1. PDH Short (Blind Touch) [40pt Target | 10:00 IST]: Net ₹+158.46 / lot (t-stat = +1.90, Hit = 37.6%, SL Fail = 38.3%, Sum = 100.0%)
  - 1. PDH Short (Blind Touch) [40pt Target | 09:45 IST]: Net ₹+157.79 / lot (t-stat = +1.93, Hit = 35.3%, SL Fail = 36.8%, Sum = 100.0%)
  - 2. Touch PDH + DOUBLE_TOP_3D [40pt Target | 09:30 IST]: Net ₹+146.96 / lot (t-stat = +1.71, Hit = 31.2%, SL Fail = 32.1%, Sum = 100.0%)

=============================================================================================================================

