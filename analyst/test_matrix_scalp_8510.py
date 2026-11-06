"""Offline tests, no app construction, broker, credentials or runtime writes."""
import unittest
from analyst.matrix_scalp_8510 import read_rules, locked_points, path_result, advance, simulate


class MatrixScalpTests(unittest.TestCase):
    def setUp(self):
        self.r=read_rules()

    def test_exact_user_ladder(self):
        for peak,stop in [(0,-15),(14.99,-15),(15,10),(19.99,10),(20,15),(25,20),(30,25),(40,35)]:
            self.assertEqual(locked_points(peak,self.r),stop)

    def test_trail_never_loosens(self):
        r=path_result([0,30,27],0,-15,self.r)
        self.assertIsNone(r['exit']);self.assertEqual(r['stop'],25)
        self.assertEqual(path_result([27,24],r['peak'],r['stop'],self.r)['exit'],25)

    def test_initial_gap_fills_worse_than_stop(self):
        self.assertEqual(path_result([-23,-25,-20],0,-15,self.r)['exit'],-23)

    def test_intrabar_order_is_not_a_fabricated_win(self):
        p=dict(direction='CE',entry=100,peak=0,stop_points=-15,last_ts=0,status='OPEN')
        r=advance(p,dict(ts=60,o=100,h=120,l=100,c=118),self.r)
        self.assertEqual(r['status'],'AMBIGUOUS');self.assertIsNone(r['points'])

    def test_short_is_symmetric(self):
        a=dict(direction='CE',entry=100,peak=20,stop_points=15,last_ts=0,status='OPEN')
        b={**a,'direction':'PE'}
        x=advance(a,dict(ts=60,o=118,h=119,l=114,c=116),self.r)
        y=advance(b,dict(ts=60,o=82,h=86,l=81,c=84),self.r)
        self.assertEqual(x['points'],15);self.assertEqual(y['points'],15)

    def test_missing_minute_is_unresolved(self):
        p=dict(direction='CE',entry=100,peak=0,stop_points=-15,last_ts=0,status='OPEN')
        self.assertEqual(advance(p,dict(ts=120,o=100,h=100,l=100,c=100),self.r)['status'],'DATA_GAP')

    def test_no_same_signal_candle_fill_and_no_tier_one_entry(self):
        from datetime import datetime
        from analyst.matrix_scalp_8510 import IST
        t=int(datetime(2026,11,6,10,tzinfo=IST).timestamp())
        b=[dict(ts=t,o=100,h=100,l=100,c=100)]
        signal=dict(ts=t,ema_4=101,ema_8=100,proximity_state=self.r['entry_tier'])
        self.assertEqual(simulate('2026-11-06',b,[signal],self.r),[])
        b.append(dict(ts=t+60,o=102,h=102,l=102,c=102))
        result=simulate('2026-11-06',b,[signal],self.r)
        self.assertEqual(result[0]['entry'],102)
        signal['proximity_state']='TIER_1_APPROACHING'
        self.assertEqual(simulate('2026-11-06',b,[signal],self.r),[])


if __name__=='__main__':
    unittest.main()
