/**
 * frontend/src/types/terminal.ts
 * TypeScript type definitions for the Institutional Prop-Desk Trading Terminal.
 */

export type SignalDirection = 'BULLISH' | 'BEARISH' | 'NEUTRAL';
export type ConvictionTier = 'TIER 1 SNIPER' | 'HIGH PROBABILITY' | 'TACTICAL';

export type PillarStatusColor = 'EMERALD' | 'ROSE' | 'SLATE' | 'AMBER';

export interface PillarScore {
  name: string;
  title: string;
  rawMetric: string;
  score: number;       // -1.0 to +1.0
  regime: string;
  detail: string;
  statusColor: PillarStatusColor;
}

export interface FourPillarsReport {
  pillars: PillarScore[];
  compositeScore: number;    // -4.0 to +4.0
  biasLabel: string;         // 'EXTREME BULLISH' | 'MODERATE BULLISH' | 'NEUTRAL' | etc.
  badgeVariant: 'EMERALD' | 'ROSE' | 'SLATE';
  algoDirective: string;
  timestamp: string;
}

export type StrikeMigrationState = 
  | 'LONG BUILD-UP' 
  | 'SHORT BUILD-UP' 
  | 'SHORT COVERING' 
  | 'LONG UNWINDING' 
  | 'FLAT';

export interface StrikeOIClassification {
  strike: number;
  ceLtp: number;
  ceLtpChg: number;
  ceOi: number;
  ceOiChg: number;
  ceOiPct: number;
  ceState: StrikeMigrationState;
  peLtp: number;
  peLtpChg: number;
  peOi: number;
  peOiChg: number;
  peOiPct: number;
  peState: StrikeMigrationState;
  isAtm: boolean;
  distFromAtm: number;
}

export interface DivergentOIBarItem {
  strike: number;
  isAtm: boolean;
  callOiChgRaw: number;
  callOiChgLeft: number;   // negative value for leftward bar
  putOiChgRaw: number;
  putOiChgRight: number;   // positive value for rightward bar
  ceState: StrikeMigrationState;
  peState: StrikeMigrationState;
  ceOi: number;
  peOi: number;
}

export interface GammaProfile {
  spotPrice: number;
  zeroGammaLevel: number;
  netGexCrores: number;
  currentRegime: 'STICKY_PIN_ZONE' | 'GAMMA_SQUEEZE_ZONE' | 'TRANSITION';
  dealerPosition: 'LONG_GAMMA' | 'SHORT_GAMMA';
  stickyStrikes: number[];
  squeezeStrikes: number[];
  callWallStrike: number;
  putWallStrike: number;
  rationale: string;
}

export type ZoneAlignmentStatus = 'CONFIRMED' | 'CONTESTED' | 'WEAKENING';

export interface ZoneMatrixRow {
  level: number;
  label: string;
  zoneType: 'SUPPORT' | 'RESISTANCE' | 'MAGNET';
  oiEvidence: string;
  smcEvidence: string;
  alignment: ZoneAlignmentStatus;
  alignmentReason: string;
  distancePts: number;
  isImmediate: boolean;
}

export interface ActiveSetupCard {
  setupName: string;
  direction: SignalDirection;
  timeframe: string;
  conviction: ConvictionTier;
  logic: string;
  action: string;
  triggerLevel: number;
  stopLoss: number;
  targetLevel: number;
  riskRewardRatio: string;
  smcPillar: string;
  optionsPillar: string;
  timestamp: string;
}

export interface DealingRange {
  high: number;
  low: number;
  equilibrium: number;
  ote705: number;
  ote790: number;
  currentPrice: number;
  currentRegime: 'PREMIUM' | 'DISCOUNT' | 'EQUILIBRIUM';
  discountDepthPct: number;
}

export interface TerminalSnapshot {
  spotPrice: number;
  timestamp: string;
  fourPillars: FourPillarsReport;
  gammaProfile: GammaProfile;
  divergentOiChart: DivergentOIBarItem[];
  strikeEvidenceTable: StrikeOIClassification[];
  activeSetups: ActiveSetupCard[];
  zoneMatrix: ZoneMatrixRow[];
  dealingRange: DealingRange;
}
