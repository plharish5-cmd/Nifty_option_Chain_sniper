import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from analyst.ledger_provenance_8510 import category,install,record,stamp_exit,enrich,hide_tests,restore_tests


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
