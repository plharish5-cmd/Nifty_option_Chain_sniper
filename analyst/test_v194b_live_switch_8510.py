"""analyst/test_v194b_live_switch_8510.py — the live arm switch cannot misfire.

WHAT THESE TESTS ARE FOR
------------------------
v1.9.4b adds a UI switch that can place real orders with real money. The only
thing standing between an algo signal and Harish's capital is this gate, so the
gate is tested from the direction of an attacker rather than a user: every test
below asks "can a real order happen when it should not?"

THE CENTRAL INVARIANT
---------------------
The routing gate in execute_order_with_chaser can only ever DOWNGRADE. It
reassigns `mode` to PAPER_ONLY and never to BOTH or ALGO_LIVE_ONLY. So no guard
failure, no missing ledger, no unparseable config and no absent broker can
cause a real order -- the worst case is paper. Several tests here exist purely
to confirm that the failure direction is safe.

WHAT IS DELIBERATELY NOT TESTED
-------------------------------
Nothing here proves the signals are profitable, and nothing here authorises
live trading. live_validation_status stays NO_GO. A passing suite means the
switch is safe to ship, not that it is safe to turn on.

All config and ledger writes go to tmp_path. Tests in this repo have previously
rewritten the live analyst/terminal8510_config.json -- one run committed
whitelisted_ip 127.0.0.1 and broke Dhan connect -- so not one assertion here
touches the shipped file.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.terminal8510_backend import QuantTerminalBackend  # noqa: E402

HTML = ROOT / "Algorithmic_Trading_Workspace_8510.html"


def make_backend(tmp_path, **safety):
    """A backend with a real ledger in tmp_path and nothing else wired."""
    b = object.__new__(QuantTerminalBackend)
    base = {'live_validation_status': 'NO_GO', 'typed_confirmation': 'LIVE',
            # Matches the shipped config: dry-run is opt-in, not forced.
            'live_dry_run': False, 'dry_run_exit_min_orders': 0,
            'live_entry_window_start': '09:15', 'live_entry_window_end': '15:20',
            'max_live_trades_per_day': 0, 'max_live_loss_rupees_per_day': 0.0}
    base.update(safety)
    b.cfg = {'execution_safety_v194': base, 'starting_paper_capital': 100000.0}
    b.config_path = tmp_path / 'config.json'
    b.ledger_db = tmp_path / 'ledger.sqlite3'
    b._live_disarm_reason = None
    b._init_sqlite()
    return b


def approved(tmp_path, **kw):
    kw.setdefault('live_validation_status', 'APPROVED')
    return make_backend(tmp_path, **kw)


class _ConnectedBroker:
    """A broker that passes the credential guard, so tests can reach the
    guards that sit BEHIND it. Credentials are checked first on purpose -- an
    order cannot be sent without them -- which means a fixture with no broker
    never exercises the trade-count or loss limits."""
    def is_configured(self):
        return True

    def get_status(self):
        return {'connected': True, 'token_expired': False, 'token_valid_till': '2099-01-01T00:00:00'}


class _ExpiredTokenBroker(_ConnectedBroker):
    def get_status(self):
        return {'connected': True, 'token_expired': True}


def seed_dry_runs(b, n):
    with sqlite3.connect(b.ledger_db) as c:
        for i in range(n):
            c.execute("INSERT OR REPLACE INTO dry_run_orders "
                      "(order_id, ts, side, qty, signal_ltp, bid, ask, spread_pts, mode) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      ("D_%d" % i, "2026-11-06T10:00:00", "BUY", 65,
                       200.0, 199.75, 200.25, 0.5, "DRY_RUN"))
        c.commit()


# ─────────────────────────────────────────────────────────────────────────────
# 1. ARMING — the gate itself
# ─────────────────────────────────────────────────────────────────────────────

def test_default_state_is_not_armed(tmp_path):
    b = make_backend(tmp_path)
    assert b.execution_mode == 'PAPER_ONLY'
    assert b.get_live_status()['live_armed'] is False


def test_no_go_verdict_still_refuses_arming(tmp_path):
    """NO_GO must keep blocking, or the flag means nothing.

    The shipped config is now USER_CONTROLLED at the desk owner's instruction,
    but an explicit NO_GO must still refuse -- otherwise there is no way to
    disable arming at all. Only APPROVED and USER_CONTROLLED permit it."""
    b = make_backend(tmp_path)                      # fixture is NO_GO
    res = b.configure_real_money(True, 'LIVE')
    assert res['ok'] is False
    assert 'NO_GO' in res['error'] or 'NO-GO' in res['error']
    assert b.execution_mode == 'PAPER_ONLY'


def test_user_controlled_permits_arming(tmp_path):
    """The owner's documented decision: he accepts real-money risk without
    requiring a backtest verdict. His own guards still apply."""
    b = make_backend(tmp_path, live_validation_status='USER_CONTROLLED')
    res = b.configure_real_money(True, 'LIVE')
    assert res['ok'] is True
    assert res['dry_run'] is False, "dry-run must not force itself on"


def test_an_unknown_status_does_not_permit_arming(tmp_path):
    for bad in ('', 'YES', 'GO', 'approved', 'user_controlled', None):
        b = make_backend(tmp_path, live_validation_status=bad)
        assert b.configure_real_money(True, 'LIVE')['ok'] is False, bad


@pytest.mark.parametrize("typed", ['', 'live', 'LIVE ', 'ENABLE REAL MONEY', 'yes', None])
def test_wrong_confirmation_refuses_arming(tmp_path, typed):
    b = approved(tmp_path)
    res = b.configure_real_money(True, typed if typed is not None else '')
    assert res['ok'] is False, "accepted confirmation {!r}".format(typed)
    assert b.execution_mode == 'PAPER_ONLY'


def test_non_boolean_enabled_is_refused(tmp_path):
    b = approved(tmp_path)
    for bad in ('true', 1, None, [], {}):
        assert b.configure_real_money(bad, 'LIVE')['ok'] is False


def test_correct_confirmation_arms_for_real_orders(tmp_path):
    """With dry-run opt-in and off, a correct confirmation arms LIVE.

    This previously asserted that arming could only reach DRY_RUN. That gate
    was removed at the desk owner's instruction; dry-run is now opt-in."""
    b = approved(tmp_path, live_entry_window_start='00:00',
                 live_entry_window_end='23:59')
    b.broker = _ConnectedBroker()
    res = b.configure_real_money(True, 'LIVE')
    assert res['ok'] is True
    assert res['dry_run'] is False
    assert b.live_execution_decision()['route'] == 'LIVE'


def test_dry_run_still_works_when_opted_into(tmp_path):
    """The instrument must remain available for whenever it is wanted."""
    b = approved(tmp_path, live_dry_run=True)
    res = b.configure_real_money(True, 'LIVE')
    assert res['ok'] is True and res['dry_run'] is True
    assert b.live_execution_decision()['route'] == 'DRY_RUN'


def test_arming_persists_off_so_restart_never_rearms(tmp_path):
    b = approved(tmp_path)
    b.configure_real_money(True, 'LIVE')
    saved = json.loads(b.config_path.read_text())
    assert saved['real_money_enabled'] is False
    assert saved['execution_mode'] == 'PAPER_ONLY'
    assert saved['paper_execution_enabled'] is True
    assert saved['algo_only_execution'] is True


def test_paper_and_algo_only_cannot_be_turned_off(tmp_path):
    """Read-only properties; the setters only log."""
    b = make_backend(tmp_path)
    b.paper_execution_enabled = False
    b.algo_only_execution = False
    assert b.paper_execution_enabled is True
    assert b.algo_only_execution is True


def test_legacy_execution_mode_assignment_cannot_arm(tmp_path):
    b = make_backend(tmp_path)
    for attempt in ('BOTH', 'ALGO_LIVE_ONLY', 'LIVE'):
        b.execution_mode = attempt
        assert b.execution_mode == 'PAPER_ONLY', attempt


def test_disarming_is_always_allowed(tmp_path):
    b = approved(tmp_path)
    assert b.configure_real_money(False, '')['ok'] is True
    assert b.execution_mode == 'PAPER_ONLY'


# ─────────────────────────────────────────────────────────────────────────────
# 2. DRY-RUN — the evidence gate
# ─────────────────────────────────────────────────────────────────────────────

def test_dry_run_is_forced_while_flag_is_on(tmp_path):
    b = approved(tmp_path, live_dry_run=True)
    seed_dry_runs(b, 500)                      # far past the count
    assert b.dry_run_gate_state()['dry_run'] is True, \
        "live_dry_run=true must override the count"


def test_below_the_order_threshold_stays_in_dry_run(tmp_path):
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=30)
    seed_dry_runs(b, 29)
    g = b.dry_run_gate_state()
    assert g['dry_run'] is True and g['recorded'] == 29


def test_threshold_met_leaves_dry_run(tmp_path):
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=30)
    seed_dry_runs(b, 30)
    assert b.dry_run_gate_state()['dry_run'] is False


def test_gate_is_orders_not_days(tmp_path):
    """A day count measures nothing about spread; only rows do."""
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=5)
    assert b.dry_run_gate_state()['dry_run'] is True
    seed_dry_runs(b, 5)
    assert b.dry_run_gate_state()['dry_run'] is False


def test_missing_ledger_counts_as_zero_evidence(tmp_path):
    """Absent evidence must keep the desk in dry-run, never fail open."""
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=30)
    b.ledger_db = tmp_path / 'nonexistent-dir' / 'ledger.sqlite3'
    assert b.count_dry_run_orders() == 0
    assert b.dry_run_gate_state()['dry_run'] is True


def test_guards_survive_a_backend_with_no_ledger_at_all(tmp_path):
    """Unit fixtures build backends with object.__new__ and no ledger_db."""
    b = object.__new__(QuantTerminalBackend)
    b.cfg = {'execution_safety_v194': {'live_validation_status': 'NO_GO'}}
    b.config_path = tmp_path / 'c.json'
    assert b.count_dry_run_orders() == 0
    assert b.live_trades_today() == 0
    assert b.live_pnl_today() == 0.0
    assert b.live_execution_decision()['route'] == 'PAPER_ONLY'


def test_dry_run_row_records_the_real_spread(tmp_path):
    b = approved(tmp_path)
    row = b._record_dry_run_order(
        {'symbol': 'NIFTY 24600 CE', 'security_id': '43215'},
        'BUY', 65, 200.0, 'Stage 2 8 EMA', 'ORD_1',
        {'bid': 199.75, 'ask': 200.25})
    assert row['spread_pts'] == pytest.approx(0.5)
    assert row['bid'] == 199.75 and row['ask'] == 200.25
    assert b.count_dry_run_orders() == 1


def test_missing_depth_is_recorded_as_null_not_invented(tmp_path):
    """A missing measurement must be visible, never faked."""
    b = approved(tmp_path)
    row = b._record_dry_run_order(
        {'symbol': 'NIFTY 24600 CE', 'security_id': '43215'},
        'BUY', 65, 200.0, 'Stage 2', 'ORD_2', {})
    assert row['bid'] is None and row['ask'] is None and row['spread_pts'] is None
    with sqlite3.connect(b.ledger_db) as c:
        got = c.execute("SELECT bid, ask, spread_pts FROM dry_run_orders").fetchone()
    assert got == (None, None, None)


def test_recording_a_dry_run_never_raises(tmp_path):
    b = approved(tmp_path)
    b.ledger_db = tmp_path / 'gone' / 'ledger.sqlite3'
    row = b._record_dry_run_order({'symbol': 'X'}, 'BUY', 65, 200.0, 'S', 'ORD_3', {})
    assert row['order_id'] == 'ORD_3'       # returned despite the write failing


# ─────────────────────────────────────────────────────────────────────────────
# 3. RUNTIME GUARDS — each one must downgrade, and only downgrade
# ─────────────────────────────────────────────────────────────────────────────

def test_the_entry_window_is_the_owners_choice(tmp_path):
    """09:15-15:20: market open to the EOD square-off, as the owner specified.
    A previous 09:20-15:05 narrowing was mine and was reverted at his
    instruction."""
    b = make_backend(tmp_path)
    win = b.live_entry_window()
    assert win['start'] == '09:15'
    assert win['end'] == '15:20'


def test_the_window_check_can_be_removed_entirely(tmp_path):
    """Blanking either bound disables it, so the owner can turn it off."""
    b = make_backend(tmp_path, live_entry_window_start='', live_entry_window_end='')
    win = b.live_entry_window()
    assert win['open'] is True and win['disabled'] is True


def test_zero_means_no_daily_cap(tmp_path):
    """Both caps are OFF in the shipped config at the owner's instruction.
    0 must mean unlimited, not "block everything"."""
    b = approved(tmp_path, max_live_trades_per_day=0,
                 max_live_loss_rupees_per_day=0.0,
                 live_entry_window_start='', live_entry_window_end='')
    b.broker = _ConnectedBroker()
    b.configure_real_money(True, 'LIVE')
    today = __import__('datetime').datetime.now(__import__(
        'analyst.terminal8510_backend', fromlist=['IST']).IST).strftime('%Y-%m-%d')
    with sqlite3.connect(b.ledger_db) as c:
        for i in range(25):            # far beyond the old 3-trade cap
            c.execute("INSERT OR REPLACE INTO orders (order_id, created_at, side, mode) "
                      "VALUES (?,?,?,?)", ("L%d" % i, today + " 10:00:00", "BUY", "LIVE"))
        c.execute("INSERT OR REPLACE INTO positions (position_id, status, mode, "
                  "exit_time, net_pnl) VALUES ('PX','CLOSED_SL','LIVE',?,?)",
                  (today + " 11:00:00", -99000.0))
        c.commit()
    d = b.live_execution_decision()
    assert d['route'] == 'LIVE', \
        "with caps at 0 neither 25 trades nor -Rs99,000 may block a signal: " + d['reason']


def test_trade_count_guard_trips_and_disarms(tmp_path):
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=0,
                 max_live_trades_per_day=3,
                 live_entry_window_start='00:00', live_entry_window_end='23:59')
    b.broker = _ConnectedBroker()
    seed_dry_runs(b, 1)
    b.configure_real_money(True, 'LIVE')
    # Must match the implementation's clock exactly: live_trades_today() and
    # live_pnl_today() both key on datetime.now(IST). Using a naive now()
    # here made the test fail whenever the container's UTC date and the IST
    # date disagreed -- i.e. every evening after 18:30 UTC.
    from analyst.terminal8510_backend import IST
    today = __import__('datetime').datetime.now(IST).strftime('%Y-%m-%d')
    with sqlite3.connect(b.ledger_db) as c:
        for i in range(3):
            c.execute("INSERT OR REPLACE INTO orders (order_id, created_at, side, mode) "
                      "VALUES (?,?,?,?)", ("L_%d" % i, today + " 10:00:00", "BUY", "LIVE"))
        c.commit()
    assert b.live_trades_today() == 3
    d = b.live_execution_decision()
    assert d['route'] == 'PAPER_ONLY' and 'Daily Live Limit' in d['reason']


def test_loss_guard_trips_and_disarms(tmp_path):
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=0,
                 max_live_loss_rupees_per_day=5000.0,
                 live_entry_window_start='00:00', live_entry_window_end='23:59')
    b.broker = _ConnectedBroker()
    b.configure_real_money(True, 'LIVE')
    # Must match the implementation's clock exactly: live_trades_today() and
    # live_pnl_today() both key on datetime.now(IST). Using a naive now()
    # here made the test fail whenever the container's UTC date and the IST
    # date disagreed -- i.e. every evening after 18:30 UTC.
    from analyst.terminal8510_backend import IST
    today = __import__('datetime').datetime.now(IST).strftime('%Y-%m-%d')
    with sqlite3.connect(b.ledger_db) as c:
        c.execute("INSERT OR REPLACE INTO positions (position_id, status, mode, "
                  "exit_time, net_pnl) VALUES ('P1','CLOSED_SL','LIVE',?,?)",
                  (today + " 11:00:00", -5200.0))
        c.commit()
    assert b.live_pnl_today() == pytest.approx(-5200.0)
    d = b.live_execution_decision()
    assert d['route'] == 'PAPER_ONLY' and 'Loss Limit' in d['reason']


def test_two_losses_exhaust_the_rupee_cap_before_the_trade_cap(tmp_path):
    """ARITHMETIC: one 40-pt stop on lot 65 is ~Rs2,678 with charges, so a
    Rs5,000 cap halts after two losses and the 3-trade cap is unreachable.
    This records that the two configured limits are not equivalent."""
    one_loss = 40 * 65 + 78.0
    assert one_loss == pytest.approx(2678.0, abs=1.0)
    assert 2 * one_loss > 5000.0, "two losses must already breach the rupee cap"
    assert 40 * 65 == 2600, "40 pts x 65 is Rs2,600, not Rs5,000"


def test_guard_failure_direction_is_always_safe(tmp_path):
    """Garbage config must downgrade to paper, never upgrade to live."""
    for bad in ({'max_live_trades_per_day': 'three'},
                {'max_live_loss_rupees_per_day': None},
                {'live_entry_window_end': ''},
                {'dry_run_exit_min_orders': 'lots'}):
        bad = dict(bad)
        bad.setdefault('live_entry_window_start', '00:00')
        bad.setdefault('live_entry_window_end', '23:59')
        b = approved(tmp_path, live_dry_run=False, **bad)
        b.broker = _ConnectedBroker()
        try:
            route = b.live_execution_decision()['route']
        except Exception as exc:
            pytest.fail("guard raised on {!r}: {}".format(bad, exc))
        assert route in ('PAPER_ONLY', 'DRY_RUN'), \
            "bad config {!r} produced route {}".format(bad, route)


def test_not_armed_always_routes_to_paper(tmp_path):
    b = make_backend(tmp_path)
    d = b.live_execution_decision()
    assert d['route'] == 'PAPER_ONLY' and d['reason'] == 'Live not armed'


# ─────────────────────────────────────────────────────────────────────────────
# 4. STOP LIVE — disarm without squaring off behind the trader's back
# ─────────────────────────────────────────────────────────────────────────────

class _NoBroker:
    def is_configured(self):
        return False


def test_stop_live_disarms_and_does_not_square_off(tmp_path):
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=0)
    b.broker = _NoBroker()
    b.configure_real_money(True, 'LIVE')
    with sqlite3.connect(b.ledger_db) as c:
        c.execute("INSERT OR REPLACE INTO positions (position_id, status, mode) "
                  "VALUES ('P1','OPEN','LIVE')")
        c.commit()
    res = asyncio.run(b.stop_live_trading())
    assert res['ok'] is True
    assert res['live_armed'] is False
    assert res['squared_off'] is False, "must not square off without being asked"
    assert res['open_live_positions'] == 1
    assert res['needs_square_off_decision'] is True
    with sqlite3.connect(b.ledger_db) as c:
        still = c.execute("SELECT status FROM positions WHERE position_id='P1'").fetchone()
    assert still[0] == 'OPEN'


def test_stop_live_with_no_open_positions_asks_nothing(tmp_path):
    b = approved(tmp_path)
    b.broker = _NoBroker()
    res = asyncio.run(b.stop_live_trading())
    assert res['needs_square_off_decision'] is False and res['prompt'] == ''


def test_stop_live_blocks_further_live_routing(tmp_path):
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=0)
    b.broker = _NoBroker()
    b.configure_real_money(True, 'LIVE')
    asyncio.run(b.stop_live_trading())
    assert b.live_execution_decision()['route'] == 'PAPER_ONLY'


def test_eod_square_off_time_is_independent_of_the_live_switch(tmp_path):
    """An unanswered prompt must not leave a live position open overnight on an
    unattended desk, so 15:20 is not conditional on live state."""
    b = make_backend(tmp_path)
    b.cfg['eod_square_off_time'] = '15:20'
    b.broker = _NoBroker()
    asyncio.run(b.stop_live_trading())
    assert b.cfg['eod_square_off_time'] == '15:20'


def test_emergency_kill_switch_was_not_weakened(tmp_path):
    """trigger_emergency_kill_switch still squares off. Squaring everything off
    IS correct in a real emergency; STOP LIVE is the narrower action."""
    src = (ROOT / 'analyst' / 'terminal8510_backend.py').read_text(encoding='utf-8')
    i = src.index('async def trigger_emergency_kill_switch')
    body = src[i:i + 4000]
    assert "status='CLOSED_KILL_SWITCH'" in body, \
        "the emergency kill switch must still square off"


# ─────────────────────────────────────────────────────────────────────────────
# 5. RECORDING — out of the UI, still recording
# ─────────────────────────────────────────────────────────────────────────────

def test_recording_health_is_gone_from_the_ui():
    html = HTML.read_text(encoding='utf-8')
    for token in ('Recording Health', 'recordingHealth', 'recording_health'):
        assert token not in html, "{} still present in the UI".format(token)


def test_the_coverage_card_and_its_rows_are_gone():
    html = HTML.read_text(encoding='utf-8')
    for token in ('recordingCoverageRows', 'recordingContracts',
                  'recordingWrite', 'recordingSession', 'recordingStatus'):
        assert token not in html, "{} still referenced".format(token)


def test_the_v193_recording_features_survived():
    """The health card and the recording itself are different things; removing
    one must not remove the other."""
    html = HTML.read_text(encoding='utf-8')
    for token in ('recordingHistoryV193', 'recordingSaved', 'recordingEmaRows',
                  'recordingRadarRows', 'recordingDownload'):
        assert token in html, "{} was lost".format(token)


def test_the_counts_line_is_a_count_not_a_verdict():
    html = HTML.read_text(encoding='utf-8')
    i = html.index("recText('recordingSaved'")
    line = html[i:i + 400]
    assert 'candles saved' in line and 'EMA rows saved' in line and 'Tier signals saved' in line
    for verdict in ('coverage_pct', 'coverage_warn_pct', 'coverage_alert_pct'):
        assert verdict not in line, "{} is a health verdict, not a count".format(verdict)


def test_no_bare_except_pass_was_introduced():
    src = (ROOT / 'analyst' / 'terminal8510_backend.py').read_text(encoding='utf-8')
    assert 'except: pass' not in src
    assert 'except Exception: pass' not in src


# ─────────────────────────────────────────────────────────────────────────────
# 6. UI WIRING — the switch cannot show a state the server did not grant
# ─────────────────────────────────────────────────────────────────────────────

def test_the_live_switch_ui_exists():
    html = HTML.read_text(encoding='utf-8')
    for token in ('liveHeaderState', 'stopLiveBtn', 'liveArmLabel',
                  'enableRealMoney', 'liveArmPulse'):
        assert token in html, "{} missing".format(token)


def test_the_ui_asks_the_server_and_does_not_decide():
    html = HTML.read_text(encoding='utf-8')
    assert '/api/live/arm' in html and '/api/live/status' in html and '/api/live/stop' in html
    assert 'function sendLiveArm' in html
    # The checkbox is forced off before every request, so a refusal cannot
    # leave the header claiming LIVE.
    i = html.index('async function sendLiveArm')
    assert 'syncRealMoneyUI(false)' in html[i:i + 200]


def test_the_modal_requires_the_exact_word():
    html = HTML.read_text(encoding='utf-8')
    assert "typed!=='LIVE'" in html
    assert 'Confirm Enable Live' in html
    assert 'YOU ARE ABOUT TO ENABLE LIVE TRADING WITH REAL MONEY' in html


def test_the_armed_warning_never_blinks_to_invisible():
    """A warning you can miss is not a warning."""
    html = HTML.read_text(encoding='utf-8')
    i = html.index('@keyframes liveArmPulse')
    block = html[i:i + 200]
    assert 'opacity: 0.55' in block or 'opacity:0.55' in block


def test_paper_always_on_is_stated_in_the_header():
    html = HTML.read_text(encoding='utf-8')
    assert 'PAPER: ON (Always)' in html


# ─────────────────────────────────────────────────────────────────────────────
# 7. SHIPPED CONFIG — nothing here authorises real money
# ─────────────────────────────────────────────────────────────────────────────

def shipped():
    return json.loads((ROOT / 'analyst' / 'terminal8510_config.json').read_text(encoding='utf-8'))


def test_the_shipped_verdict_is_the_owners_choice():
    """USER_CONTROLLED, set 7 Nov 2026 on the desk owner's explicit
    instruction. The evidence note must say so AND must not pretend an edge
    exists."""
    s = shipped()['execution_safety_v194']
    assert s['live_validation_status'] in ('USER_CONTROLLED', 'APPROVED', 'NO_GO')
    if s['live_validation_status'] == 'USER_CONTROLLED':
        ev = s.get('_evidence_live_validation_status', '')
        assert 'instruction' in ev.lower()
        assert 'NOT a claim of an edge' in ev or 'not a claim of an edge' in ev.lower()
        assert '0.828' in ev, "the failed study result must stay on the record"


def test_dry_run_is_opt_in_not_forced():
    """The mandatory 30-order gate was never requested by the desk owner and
    was removed at his instruction. It must remain available, but off by
    default -- it is still the only route to a measured execution cost."""
    s = shipped()['execution_safety_v194']
    assert s['live_dry_run'] is False
    assert 'live_dry_run' in s, "the option must still exist"
    ev = s.get('_evidence_live_dry_run', '')
    assert 'OPT-IN' in ev.upper()


def test_the_typed_confirmation_is_live():
    assert shipped()['execution_safety_v194']['typed_confirmation'] == 'LIVE'


def test_the_shipped_window_and_caps_match_the_owners_instruction():
    s = shipped()['execution_safety_v194']
    assert s['live_entry_window_start'] == '09:15'
    assert s['live_entry_window_end'] == '15:20'
    assert int(s['max_live_trades_per_day']) == 0, "daily trade cap must be off"
    assert float(s['max_live_loss_rupees_per_day']) == 0.0, "daily loss cap must be off"


def test_every_new_threshold_carries_an_evidence_note():
    """AGENTS.md: thresholds live in JSON with their evidence. An unexplained
    number is how invented constants get in."""
    s = shipped()['execution_safety_v194']
    notes = ' '.join(v for k, v in s.items()
                     if k.startswith('_evidence') and isinstance(v, str))
    assert '15:20' in notes
    assert '2,600' in notes or '2600' in notes
    assert 'dry-run' in notes.lower()
    assert 'instruction' in notes.lower(), \
        "a changed safety setting must record whose decision it was"
    assert 'PER LEG' in notes and 'PER ROUND TRIP' in notes, \
        "the per-leg vs per-trade unit correction must be recorded"


def test_part_c_is_an_inert_placeholder():
    """The instruction cut off mid-sentence; nothing was inferred."""
    p = ROOT / 'analyst' / 'trend_capture_study_8510.py'
    src = p.read_text(encoding='utf-8')
    assert 'TODO' in src
    assert '__all__ = []' in src
    assert 'def ' not in src and 'class ' not in src, \
        "the placeholder must have no behaviour"


def test_expired_token_disarms_before_any_order(tmp_path):
    """A real order must never ride on an expired session."""
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=0)
    b.broker = _ExpiredTokenBroker()   # window irrelevant: credentials gate first
    b.configure_real_money(True, 'LIVE')
    d = b.live_execution_decision()
    assert d['route'] == 'PAPER_ONLY'
    assert 'Token Expired' in d['reason']
    assert d['disarm'] is True


def test_disconnected_broker_disarms(tmp_path):
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=0)
    b.broker = _NoBroker()
    b.configure_real_money(True, 'LIVE')
    d = b.live_execution_decision()
    assert d['route'] == 'PAPER_ONLY' and d['disarm'] is True


def test_a_fully_clear_path_does_reach_live(tmp_path):
    """The guards must not be unconditionally closed -- otherwise these tests
    would pass even if the switch could never work at all."""
    b = approved(tmp_path, live_dry_run=False, dry_run_exit_min_orders=0,
                 live_entry_window_start='00:00', live_entry_window_end='23:59')
    b.broker = _ConnectedBroker()
    b.configure_real_money(True, 'LIVE')
    d = b.live_execution_decision()
    assert d['route'] == 'LIVE', d['reason']
