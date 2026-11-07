import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from analyst.ledger_provenance_8510 import category,install,record,stamp_exit,enrich,hide_tests,restore_tests,algo_session


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.sqlite3'
        with sqlite3.connect(self.path) as c:
            c.executescript('''CREATE TABLE orders(order_id TEXT PRIMARY KEY,created_at TEXT,fill_price REAL,
                qty INTEGER,symbol TEXT,security_id TEXT,mode TEXT,order_status TEXT,dhan_order_id TEXT,
                side TEXT,position_id TEXT,correlation_id TEXT);
                CREATE TABLE positions(position_id TEXT PRIMARY KEY,entry_price REAL,entry_time TEXT,
                qty INTEGER,symbol TEXT,security_id TEXT,mode TEXT,status TEXT,exit_price REAL,exit_time TEXT,
                net_pnl REAL,gross_pnl REAL,exit_reason TEXT,duration TEXT,sl_order_id TEXT,target_order_id TEXT);''')
            install(c.cursor())

    def add(self,key,stamp='2026-11-06 16:45:00',mode='PAPER_ONLY',status='CLOSED'):
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO positions(position_id,entry_price,entry_time,qty,symbol,security_id,mode,status) VALUES(?,242,?,65,?,?,?,?)',
                      (key,stamp,key,'123',mode,status))
            c.execute('INSERT INTO orders VALUES(?,?,242,65,?,?,?,\'FILLED\',NULL,\'BUY\',?,?)',
                      ('O'+key,stamp,key,'123',mode,key,key))
            record(c.cursor(),'positions',key,mode,stamp,entry_premium=242,entry_spot=24500,price_basis='OBSERVED_QUOTE_PAPER_ENTRY')
            record(c.cursor(),'orders','O'+key,mode,stamp,parent=key,entry_premium=242,entry_spot=24500,price_basis='OBSERVED_QUOTE_PAPER_ENTRY')

    def test_session_boundaries(self):
        for t in ['09:15','15:30','15:45']:self.assertEqual(category('PAPER_ONLY','2026-11-06 '+t),'MARKET_SESSION')
        self.assertEqual(category('PAPER_ONLY','2026-11-06 15:46'),'OFF_MARKET_TEST')
        self.assertEqual(category('PAPER_ONLY','bad date'),'LEGACY_UNVERIFIED')

    def test_algo_only_session_excludes_manual_test_and_after_hours(self):
        row=dict(source='AUTO ALGO',strategy_setup='Stage 2')
        self.assertTrue(algo_session(row,'2026-11-06 10:00'))
        self.assertFalse(algo_session(row,'2026-11-06 16:45'))
        self.assertFalse(algo_session({**row,'source':'MANUAL MATRIX'},'2026-11-06 10:00'))
        for setup in ['Stage 2 TEST','Replay Stage 2','BENCHMARK Stage 2','BACKTESTING']:
            self.assertFalse(algo_session({**row,'strategy_setup':setup},'2026-11-06 10:00'))
        self.assertFalse(algo_session({**row,'source':None},'2026-11-06 10:00'))
        self.assertFalse(algo_session({**row,'test_hidden':True},'2026-11-06 10:00'))

    def test_legacy_late_exit_uses_original_algo_entry_session(self):
        self.add('session','2026-11-06 10:00')
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row;cur=c.cursor()
            raw=dict(order_id='late',mode='PAPER_ONLY',side='SELL',created_at='2026-11-06 17:00',correlation_id='session',position_id='session',source='AUTO ALGO',strategy_setup='Stage 2')
            r=enrich(cur,'orders',raw)
            self.assertTrue(r['is_algo_session']);self.assertEqual(r['execution_book'],'PAPER')

    def test_explicit_manual_origin_cannot_be_promoted_by_auto_label(self):
        with sqlite3.connect(self.path) as c:
            cur=c.cursor()
            record(cur,'orders','M','PAPER_ONLY','2026-11-06 10:00',is_algo=False)
            row=enrich(cur,'orders',dict(order_id='M',created_at='2026-11-06 10:00',side='BUY',mode='PAPER_ONLY',source='AUTO ALGO',strategy_setup='Stage 2'))
            self.assertFalse(row['is_algo_session'])

    def test_ui_filters_and_exports_use_same_algo_view(self):
        import re,subprocess
        page=Path(__file__).resolve().parents[1]/'Algorithmic_Trading_Workspace_8510.html'
        html=page.read_text()
        fn=re.search(r'function algoViewRows\(rows\)\{.*?\n        \}',html,re.S).group()
        js=fn+'''
const rows=[{id:'algo',is_algo_session:true,execution_book:'PAPER'},
{id:'manual',is_algo_session:false,execution_book:'PAPER'},
{id:'test',is_algo_session:true,test_hidden:true,execution_book:'PAPER'},
{id:'real',is_algo_session:true,execution_book:'REAL'}];
let currentMoneyFilter='PAPER';
if(JSON.stringify(algoViewRows(rows).map(r=>r.id))!=='["algo"]')throw Error('paper leak');
currentMoneyFilter='REAL';
if(JSON.stringify(algoViewRows(rows).map(r=>r.id))!=='["real"]')throw Error('real leak');
'''
        subprocess.run(['node','-e',js],check=True,capture_output=True)
        self.assertTrue('algoViewRows(rows).map(algoPositionValues)' in html)
        self.assertTrue('algoViewRows(rows).map(algoOrderValues)' in html)
        self.assertTrue('algoViewRows(await posRes.json())' in html)
        for name in ['handleSquareOffClick','saveSuperTrailConfig','quickActionAllPositions','modifyPosition']:
            self.assertTrue(('async function '+name+'(') in html,name+' action missing')

    def test_real_or_mixed_is_never_disposable(self):
        for mode in ['BOTH','ALGO_LIVE_ONLY','LIVE']:self.assertEqual(category(mode,'2026-11-06 17:00'),'REAL_OR_MIXED')

    def test_cleanup_is_reversible_and_keeps_protected_records(self):
        self.add('test');self.add('session','2026-11-06 10:00');self.add('live',mode='BOTH');self.add('open',status='OPEN')
        result=hide_tests(self.path)
        self.assertEqual(result['hidden_orders'],1);self.assertEqual(result['hidden_positions'],1)
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],4)
            self.assertEqual(c.execute('SELECT count(*) FROM positions').fetchone()[0],4)
        self.assertEqual(restore_tests(self.path)['restored'],2)

    def test_delete_and_closed_market_price_edit_are_rejected(self):
        self.add('session','2026-11-06 10:00')
        for sql in ["DELETE FROM orders","DELETE FROM positions","UPDATE orders SET fill_price=220","UPDATE positions SET entry_price=220","UPDATE positions SET exit_price=224"]:
            with self.assertRaises(sqlite3.IntegrityError):
                with sqlite3.connect(self.path) as c:c.execute(sql)

    def test_replace_cannot_bypass_immutability(self):
        self.add('session','2026-11-06 10:00')
        for sql in ["INSERT OR REPLACE INTO orders(order_id,fill_price) VALUES('Osession',220)","INSERT OR REPLACE INTO positions(position_id,entry_price) VALUES('session',220)"]:
            with self.assertRaises(sqlite3.IntegrityError):
                with sqlite3.connect(self.path) as c:c.execute(sql)

    def test_real_backend_migration_and_no_quote_position_does_not_exit(self):
        from analyst.terminal8510_backend import QuantTerminalBackend
        from types import SimpleNamespace
        b=QuantTerminalBackend.__new__(QuantTerminalBackend)
        b.ledger_db=Path(self.tmp.name)/'real-schema.sqlite3';b.cfg={}
        b._init_sqlite()
        with sqlite3.connect(b.ledger_db) as c:
            self.assertGreater(c.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0],0)
            c.execute("INSERT INTO positions(position_id,symbol,security_id,qty,entry_price,current_ltp,entry_time,status,mode) VALUES('P','NIFTY TEST','123',65,242,276,'2026-11-06 10:00:00','OPEN','PAPER_ONLY')")
            record(c.cursor(),'positions','P','PAPER_ONLY','2026-11-06 10:00:00',entry_spot=24500,entry_premium=242)
        rows=b.get_active_positions({})
        self.assertEqual(len(rows),1);self.assertFalse(rows[0]['valuation_available'])
        self.assertEqual(rows[0]['observed_entry_premium'],242)
        with sqlite3.connect(b.ledger_db) as c:self.assertEqual(c.execute('SELECT status FROM positions').fetchone()[0],'OPEN')

    def test_real_order_mtm_uses_broker_fill_not_paper_parent_price(self):
        import time
        from types import SimpleNamespace
        from analyst.terminal8510_backend import QuantTerminalBackend
        b=QuantTerminalBackend.__new__(QuantTerminalBackend)
        b.ledger_db=Path(self.tmp.name)/'real-mark.sqlite3';b.cfg={};b._init_sqlite()
        b.broker=SimpleNamespace(_ltp_cache={'123':276},_cache_timestamps={'123':time.time()})
        with sqlite3.connect(b.ledger_db) as c:
            cur=c.cursor()
            cur.execute("INSERT INTO positions(position_id,symbol,security_id,qty,entry_price,entry_time,status,mode) VALUES('P','NIFTY 09 NOV 24300 CALL','123',65,220,'2026-11-06 10:00','OPEN','BOTH')")
            cur.execute("INSERT INTO orders(order_id,position_id,symbol,security_id,qty,created_at,mode,order_status,side,source,strategy_setup,dhan_order_id) VALUES('R','P','NIFTY 09 NOV 24300 CALL','123',65,'2026-11-06 10:00','BOTH','FILLED','BUY','AUTO ALGO','Stage 2','BROKER123')")
            record(cur,'positions','P','BOTH','2026-11-06 10:00',entry_premium=220,is_algo=True)
            record(cur,'orders','R','BOTH','2026-11-06 10:00',parent='P',entry_premium=242,broker_order_id='BROKER123',is_algo=True,price_basis='BROKER_CONFIRMED_FILL')
        row=b.get_order_book()[0]
        self.assertTrue(row['is_algo_session']);self.assertEqual(row['execution_book'],'REAL')
        self.assertEqual(row['observed_entry_premium'],242)
        self.assertEqual(row['verified_live_mtm'],round(34*65-b.calculate_dhan_charges(242,276,65),2))

    def test_late_exit_inherits_market_protection(self):
        self.add('session','2026-11-06 10:00')
        with sqlite3.connect(self.path) as c:
            record(c.cursor(),'orders','late','PAPER_ONLY','2026-11-06 17:00',parent='session',exit_premium=276)
            self.assertEqual(c.execute("SELECT category FROM execution_provenance WHERE id='late'").fetchone()[0],'MARKET_SESSION')

    def test_exact_observed_premiums_and_index_prices_are_preserved(self):
        self.add('session','2026-11-06 10:00')
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row;cur=c.cursor()
            stamp_exit(cur,'session',24535,276);stamp_exit(cur,'session',99999,99999)
            row=enrich(cur,'orders',dict(c.execute('SELECT * FROM orders').fetchone()))
            self.assertEqual((row['observed_entry_premium'],row['observed_exit_premium']),(242,276))
            self.assertEqual((row['recorded_entry_spot'],row['recorded_exit_spot']),(24500,24535))

    def test_unconfirmed_broker_price_does_not_inherit_paper_premium(self):
        self.add('mixed',mode='BOTH')
        with sqlite3.connect(self.path) as c:
            record(c.cursor(),'orders','unconfirmed','BOTH','2026-11-06 10:00',parent='mixed',price_basis='BROKER_FILL_UNCONFIRMED',broker_order_id='REAL123')
            d=json.loads(c.execute("SELECT payload FROM execution_provenance WHERE id='unconfirmed'").fetchone()[0])
            self.assertIsNone(d['entry_premium'])

    def test_legacy_paper_test_can_be_hidden_without_inventing_prices(self):
        self.add('legacy')
        with sqlite3.connect(self.path) as c:c.execute('DELETE FROM execution_provenance')
        result=hide_tests(self.path);self.assertEqual(result['hidden_orders'],1)
        with sqlite3.connect(self.path) as c:
            d=json.loads(c.execute("SELECT payload FROM execution_provenance WHERE entity='orders'").fetchone()[0])
            self.assertNotIn('entry_premium',d)


if __name__=='__main__':unittest.main()
