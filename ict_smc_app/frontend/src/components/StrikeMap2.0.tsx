// StrikeMap2.0.tsx — Visual Multi-Tab Strike Map 2.0 (Institutional Open Interest Matrix)
// Matches and surpasses StockMojo's visual multi-tab Open Interest chart architecture with:
//   - Tab Switcher: [ 📊 OI Change + Total ] | [ ⚡ OI Change Only ] | [ 🌊 Total OI Only ] | [ 📋 Buildup Matrix Table ]
//   - Institutional colors: Put Support = Emerald Green #10b981, Call Resistance = Crimson Red #ef4444
//   - Grouped dual-bar charts with fresh intraday ΔOI hatched overlay
//   - Spot Price & Max Pain vertical target lines
//   - Glassmorphic hover inspector card
import OIMatrixSuite, {
  type OIMatrixSuiteProps,
  type StrikeData,
  type OIMatrixPayload,
  InteractiveStrikeBarChart,
  SentimentSpeedometer,
  BuildupTag,
} from "./OIMatrixSuite";

export {
  OIMatrixSuite,
  InteractiveStrikeBarChart,
  SentimentSpeedometer,
  BuildupTag,
};
export type { OIMatrixSuiteProps, StrikeData, OIMatrixPayload };

export const StrikeMap2_0 = OIMatrixSuite;
export default OIMatrixSuite;
