"""Scoped25/55% graduation and actual-tariff tests; no global8510settings changed."""
import pytest
from algo_execution_engine.stage3_sandbox import SandboxTrade,Stage3GraduationTracker
from algo_execution_engine.order_gateway import DhanOrderGateway,validate_1lot_cap
from algo_execution_engine.strategy_setup1 import load_config
from paper_trading import calculate_trade_charges

def seed(tracker,n=25,wins=15):
 for i in range(n):
  t=SandboxTrade(trade_id=f'T{i}',session_date='2026-11-06',entry_ts=1000+i,entry_time='10:00:00',spot_entry=25000,option_strike=25000,option_entry_ltp=200,bid_ask_spread_pts=.5,delta_est=.6,initial_sl_spot=24960,target_spot=25040,confluence_snapshot={})
  tracker.record_entry(t);tracker.record_exit(t.trade_id,25040 if i<wins else 24960,224 if i<wins else 180,'Target Hit' if i<wins else 'Stop-Loss Hit',slippage_pts=0)

def test_actual_tariff_and_65_quantity(tmp_path):
 t=Stage3GraduationTracker(str(tmp_path/'book.db'));seed(t,1,1);row=t.get_completed_trades()[0]
 assert row['gross_option_pnl_inr']==1560;assert row['brokerage_inr']==calculate_trade_charges(200,224,65,side='BUY')['total_charges']
 assert row['net_pnl_inr']==round(1560-row['brokerage_inr'],2)
 assert calculate_trade_charges(200,240,65,side='BUY')['total_charges']==83.01

def test_25_trades_55percent_positive_expectancy(tmp_path):
 t=Stage3GraduationTracker(str(tmp_path/'book.db'));seed(t);s=t.evaluate_graduation_status()
 assert s['graduation_ready'];assert s['completed_trades']==25;assert s['rolling_win_rate_pct']==60;assert s['net_expectancy_inr']>=100

def test_insufficient_and_losing_not_graduated(tmp_path):
 t=Stage3GraduationTracker(str(tmp_path/'book.db'));seed(t,24,24);assert not t.evaluate_graduation_status()['graduation_ready']
 t=Stage3GraduationTracker(str(tmp_path/'loss.db'));seed(t,25,10);assert not t.evaluate_graduation_status()['graduation_ready']

def test_cap_and_user_scoped_limits():
 assert validate_1lot_cap(130,65)==65
 with pytest.raises(ValueError):validate_1lot_cap(0,65)
 g=DhanOrderGateway(load_config());assert not g.check_circuit_breakers(2,0)[0];assert not g.check_circuit_breakers(0,-3000)[0];assert g.check_circuit_breakers(1,0)[0]
