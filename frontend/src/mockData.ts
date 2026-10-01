/**
 * frontend/src/mockData.ts
 * Real-time institutional dataset with strict financial terminology.
 */

import { TerminalSnapshot } from './types/terminal';

export const mockTerminalSnapshot: TerminalSnapshot = {
  spotPrice: 24158.4,
  timestamp: new Date().toISOString(),
  fourPillars: {
    compositeScore: 2.8,
    biasLabel: 'MODERATE BULLISH',
    badgeVariant: 'EMERALD',
    algoDirective: 'BUY-THE-DIP PLAYBOOK: Long Bull Put Spreads / Futures with trailing stops. Seek sniper entries at Discount Zone and Put Wall confluence; Avoid chasing breakouts at extreme Premium.',
    timestamp: new Date().toISOString(),
    pillars: [
      {
        name: 'VIX_REGIME',
        title: 'VIX Volatility',
        rawMetric: '12.85 (-2.4%)',
        score: 1.0,
        regime: 'COMPRESSING (RISK-ON)',
        detail: 'VIX at 12.85 (-2.4%), IV crush favoring long gamma calls / option sellers',
        statusColor: 'EMERALD'
      },
      {
        name: 'FII_DII',
        title: 'Institutional Net',
        rawMetric: '+1,470 Cr',
        score: 1.0,
        regime: 'AGGRESSIVE INFLOWS',
        detail: 'FII: +850 Cr | DII: +620 Cr. Heavy net institutional absorption.',
        statusColor: 'EMERALD'
      },
      {
        name: 'MARKET_BREADTH',
        title: 'Market Breadth',
        rawMetric: '34A / 16D',
        score: 0.5,
        regime: 'POSITIVE SPREAD',
        detail: '34 Advances / 16 Declines (2.1:1 ratio). Positive underlying participation.',
        statusColor: 'EMERALD'
      },
      {
        name: 'GLOBAL_CUES',
        title: 'Global Cues',
        rawMetric: '+45 pts',
        score: 0.3,
        regime: 'MILD TAILWINDS',
        detail: 'GIFT Nifty +45 pts | S&P +0.65% | DXY 103.8. Stable risk appetite.',
        statusColor: 'EMERALD'
      }
    ]
  },
  gammaProfile: {
    spotPrice: 24158.4,
    zeroGammaLevel: 24175.0,
    netGexCrores: 342.0,
    currentRegime: 'STICKY_PIN_ZONE',
    dealerPosition: 'LONG_GAMMA',
    stickyStrikes: [24000, 24100, 24200, 24300],
    squeezeStrikes: [23850, 24450],
    callWallStrike: 24300,
    putWallStrike: 24000,
    rationale: 'Dealers in positive gamma (+342 Cr GEX). Dynamic hedging acts as volatility damper, pinning price between Put Wall 24,000 and Call Wall 24,300.'
  },
  dealingRange: {
    high: 24360.0,
    low: 23820.0,
    equilibrium: 24090.0,
    ote705: 24200.7,
    ote790: 24246.6,
    currentPrice: 24158.4,
    currentRegime: 'PREMIUM',
    discountDepthPct: 62.7
  },
  divergentOiChart: [
    { strike: 23900, isAtm: false, callOiChgRaw: -12400, callOiChgLeft: 12400, putOiChgRaw: 68500, putOiChgRight: 68500, ceState: 'SHORT COVERING', peState: 'SHORT BUILD-UP', ceOi: 145000, peOi: 540000 },
    { strike: 23950, isAtm: false, callOiChgRaw: -8200, callOiChgLeft: 8200, putOiChgRaw: 41200, putOiChgRight: 41200, ceState: 'SHORT COVERING', peState: 'SHORT BUILD-UP', ceOi: 98000, peOi: 320000 },
    { strike: 24000, isAtm: false, callOiChgRaw: -34500, callOiChgLeft: 34500, putOiChgRaw: 184500, putOiChgRight: 184500, ceState: 'SHORT COVERING', peState: 'SHORT BUILD-UP', ceOi: 420000, peOi: 1680000 },
    { strike: 24050, isAtm: false, callOiChgRaw: 15600, callOiChgLeft: -15600, putOiChgRaw: 88000, putOiChgRight: 88000, ceState: 'SHORT BUILD-UP', peState: 'SHORT BUILD-UP', ceOi: 210000, peOi: 640000 },
    { strike: 24100, isAtm: false, callOiChgRaw: 34200, callOiChgLeft: -34200, putOiChgRaw: 92400, putOiChgRight: 92400, ceState: 'SHORT BUILD-UP', peState: 'LONG BUILD-UP', ceOi: 480000, peOi: 890000 },
    { strike: 24150, isAtm: true,  callOiChgRaw: 42500, callOiChgLeft: -42500, putOiChgRaw: 48200, putOiChgRight: 48200, ceState: 'SHORT BUILD-UP', peState: 'LONG BUILD-UP', ceOi: 720000, peOi: 710000 },
    { strike: 24200, isAtm: false, callOiChgRaw: 86400, callOiChgLeft: -86400, putOiChgRaw: -21300, putOiChgRight: -21300, ceState: 'SHORT BUILD-UP', peState: 'LONG UNWINDING', ceOi: 1120000, peOi: 540000 },
    { strike: 24250, isAtm: false, callOiChgRaw: 54100, callOiChgLeft: -54100, putOiChgRaw: -14200, putOiChgRight: -14200, ceState: 'SHORT BUILD-UP', peState: 'LONG UNWINDING', ceOi: 640000, peOi: 280000 },
    { strike: 24300, isAtm: false, callOiChgRaw: 142000, callOiChgLeft: -142000, putOiChgRaw: -38500, putOiChgRight: -38500, ceState: 'SHORT BUILD-UP', peState: 'LONG UNWINDING', ceOi: 1850000, peOi: 310000 },
    { strike: 24350, isAtm: false, callOiChgRaw: 48900, callOiChgLeft: -48900, putOiChgRaw: -8100, putOiChgRight: -8100, ceState: 'SHORT BUILD-UP', peState: 'LONG UNWINDING', ceOi: 520000, peOi: 140000 },
    { strike: 24400, isAtm: false, callOiChgRaw: 96500, callOiChgLeft: -96500, putOiChgRaw: -4500, putOiChgRight: -4500, ceState: 'SHORT BUILD-UP', peState: 'LONG UNWINDING', ceOi: 1240000, peOi: 120000 }
  ],
  strikeEvidenceTable: [
    { strike: 24000, ceLtp: 215.4, ceLtpChg: -12.4, ceOi: 420000, ceOiChg: -34500, ceOiPct: -7.6, ceState: 'SHORT COVERING', peLtp: 58.2, peLtpChg: +8.4, peOi: 1680000, peOiChg: 184500, peOiPct: +12.3, peState: 'SHORT BUILD-UP', isAtm: false, distFromAtm: -158.4 },
    { strike: 24050, ceLtp: 172.0, ceLtpChg: -10.1, ceOi: 210000, ceOiChg: 15600, ceOiPct: +8.0, ceState: 'SHORT BUILD-UP', peLtp: 72.8, peLtpChg: +6.2, peOi: 640000, peOiChg: 88000, peOiPct: +15.9, peState: 'SHORT BUILD-UP', isAtm: false, distFromAtm: -108.4 },
    { strike: 24100, ceLtp: 134.5, ceLtpChg: -8.5, ceOi: 480000, ceOiChg: 34200, ceOiPct: +7.7, ceState: 'SHORT BUILD-UP', peLtp: 92.4, peLtpChg: +4.1, peOi: 890000, peOiChg: 92400, peOiPct: +11.6, peState: 'LONG BUILD-UP', isAtm: false, distFromAtm: -58.4 },
    { strike: 24150, ceLtp: 101.2, ceLtpChg: -6.0, ceOi: 720000, ceOiChg: 42500, ceOiPct: +6.3, ceState: 'SHORT BUILD-UP', peLtp: 114.6, peLtpChg: +2.3, peOi: 710000, peOiChg: 48200, peOiPct: +7.3, peState: 'LONG BUILD-UP', isAtm: true, distFromAtm: -8.4 },
    { strike: 24200, ceLtp: 74.8, ceLtpChg: -4.2, ceOi: 1120000, ceOiChg: 86400, ceOiPct: +8.4, ceState: 'SHORT BUILD-UP', peLtp: 142.0, peLtpChg: -3.5, peOi: 540000, peOiChg: -21300, peOiPct: -3.8, peState: 'LONG UNWINDING', isAtm: false, distFromAtm: 41.6 },
    { strike: 24250, ceLtp: 52.3, ceLtpChg: -3.1, ceOi: 640000, ceOiChg: 54100, ceOiPct: +9.2, ceState: 'SHORT BUILD-UP', peLtp: 176.5, peLtpChg: -5.8, peOi: 280000, peOiChg: -14200, peOiPct: -4.8, peState: 'LONG UNWINDING', isAtm: false, distFromAtm: 91.6 },
    { strike: 24300, ceLtp: 35.6, ceLtpChg: -2.4, ceOi: 1850000, ceOiChg: 142000, ceOiPct: +8.3, ceState: 'SHORT BUILD-UP', peLtp: 218.4, peLtpChg: -8.2, peOi: 310000, peOiChg: -38500, peOiPct: -11.0, peState: 'LONG UNWINDING', isAtm: false, distFromAtm: 141.6 }
  ],
  activeSetups: [
    {
      setupName: 'Dragon Fruit Combo Detected',
      direction: 'BEARISH',
      timeframe: '15m',
      conviction: 'TIER 1 SNIPER',
      logic: '15m Imbalance overlaps with Bearish OB and 24250 Call Short Build-up. Premium Zone (62.7% depth).',
      action: 'Look for SFP (Swing Failure) on 1m chart for short entry. Limit order at Mean Threshold 24,215.',
      triggerLevel: 24215.0,
      stopLoss: 24258.0,
      targetLevel: 24090.0,
      riskRewardRatio: '1:2.9',
      smcPillar: '15m FVG + Bearish OB Overlap',
      optionsPillar: 'Strike 24250 Heavy Call Writing',
      timestamp: new Date().toISOString()
    },
    {
      setupName: 'SFP Sweep of PDH Reversal',
      direction: 'BEARISH',
      timeframe: '5m',
      conviction: 'HIGH PROBABILITY',
      logic: 'Wick pierced Previous Day High (PDH 24,242) by 14 pts but closed back inside range at 24,218.',
      action: 'Initiate short fade on candle close below swept level. Target 50% Equilibrium at 24,090.',
      triggerLevel: 24218.0,
      stopLoss: 24262.0,
      targetLevel: 24090.0,
      riskRewardRatio: '1:2.9',
      smcPillar: 'Liquidity Hunt above PDH',
      optionsPillar: 'Gamma Squeeze rejection outside 24300 Call Wall',
      timestamp: new Date().toISOString()
    },
    {
      setupName: 'Strong Bullish MSB Entry Zone',
      direction: 'BULLISH',
      timeframe: '15m',
      conviction: 'HIGH PROBABILITY',
      logic: 'Full-bodied candle close broke final LH 24,080. Pullback to Discount Zone 24,010.',
      action: 'Wait for retracement into Demand OB [23,990 - 24,030]. Enter on 1m micro-break.',
      triggerLevel: 24020.0,
      stopLoss: 23970.0,
      targetLevel: 24220.0,
      riskRewardRatio: '1:4.0',
      smcPillar: 'Strong MSB out of deepest LL',
      optionsPillar: 'Heavy 24000 Put Wall Defence (+18.4L PE OI)',
      timestamp: new Date().toISOString()
    }
  ],
  zoneMatrix: [
    {
      level: 24300.0,
      label: 'R1: Heavy Call Wall (24300)',
      zoneType: 'RESISTANCE',
      oiEvidence: '18.5L CE OI (+8.3% ΔOI, SHORT BUILD-UP)',
      smcEvidence: '15m Bearish Breaker Block + EQH Pool',
      alignment: 'CONFIRMED',
      alignmentReason: 'Heavy call writing defends ceiling (+OI build-up corroborates SMC supply).',
      distancePts: 141.6,
      isImmediate: false
    },
    {
      level: 24215.0,
      label: 'Dragon Fruit Supply Zone',
      zoneType: 'RESISTANCE',
      oiEvidence: '24200 CE (+86K fresh call writing)',
      smcEvidence: '15m Bearish OB [24,200 - 24,235] + FVG',
      alignment: 'CONFIRMED',
      alignmentReason: 'SMC supply block aligns directly with Call writing cluster.',
      distancePts: 56.6,
      isImmediate: true
    },
    {
      level: 24175.0,
      label: 'Zero Gamma Level (Flip)',
      zoneType: 'MAGNET',
      oiEvidence: 'GEX Inversion boundary (Net GEX +342 Cr)',
      smcEvidence: '5m Fair Value Gap midpoint',
      alignment: 'CONTESTED',
      alignmentReason: 'Transition zone between market-maker volatility dampening and acceleration.',
      distancePts: 16.6,
      isImmediate: true
    },
    {
      level: 24090.0,
      label: '50% Range Equilibrium (EQ)',
      zoneType: 'SUPPORT',
      oiEvidence: '24100 Straddle balance point',
      smcEvidence: 'Major Dealing Range 50% Equilibrium',
      alignment: 'CONFIRMED',
      alignmentReason: 'Fair value balance level; magnet for mean-reversion algo strategies.',
      distancePts: -68.4,
      isImmediate: false
    },
    {
      level: 24000.0,
      label: 'S1: Heavy Put Wall (24000)',
      zoneType: 'SUPPORT',
      oiEvidence: '16.8L PE OI (+12.3% ΔOI, SHORT BUILD-UP)',
      smcEvidence: '15m Bullish Demand OB + EQL Liquidity Pool',
      alignment: 'CONFIRMED',
      alignmentReason: 'Institutional put writers anchoring structural floor backed by SMC demand.',
      distancePts: -158.4,
      isImmediate: false
    }
  ]
};
