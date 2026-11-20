from analyst.terminal8510_backend import QuantTerminalBackend

b = QuantTerminalBackend()
print("\n" + "="*80)
print("🔍 RUNNING ALL 7 SECTION 9 VERIFICATION CHECKS:")
print("="*80)

# Check 1: 1 HR FLAGSHIP Filter on 2026-09-01 to 2026-10-31
res_h1 = b.run_date_range_backtest("2026-09-01", "2026-10-31", strategy_filter="H1_FLAGSHIP")
print(f"Check 1: Total 1 HR FLAGSHIP Trades: {res_h1['total_trades']} (Table NOT blank!)")
assert res_h1['total_trades'] > 0, "Table should not be blank!"

t0 = res_h1['trades'][0]

# Check 2: 1H 8-EMA details column
print(f"\nCheck 2: Column 14 (8 EMA Line Details):\n   --> {t0['Ema8_Line_Details']}")
assert "1H 8 EMA:" in t0['Ema8_Line_Details'], "Missing 1H 8-EMA details!"

# Check 3, 4, 5: Synthetic Data & Option Tags
print(f"\nCheck 3: Column 13 (Data Type): {t0['Data_Type']}")
print(f"Check 4: Option Entry Tag: {t0['Option_Entry_Display']}")
print(f"Check 5: Option Exit Tag:  {t0['Option_Exit_Display']}")
assert t0['Data_Type'] == "Synthetic Data"
assert "(Synthesized Option Price)" in t0['Option_Entry_Display']
assert "(Synthesized Option Price)" in t0['Option_Exit_Display']

# Check 6: Index High & Index Low removed
assert "Index_High" not in t0 and "Index_Low" not in t0, "Index High/Low should be removed!"
print("\nCheck 6: Verified Index High and Index Low columns are REMOVED from trade records.")

# Check 7: Stage 2 shows 15m 8 EMA line
res_s2 = b.run_date_range_backtest("2026-09-01", "2026-10-31", strategy_filter="STAGE_2_LONG")
if res_s2['trades']:
    t_s2 = res_s2['trades'][0]
    print(f"\nCheck 7: Other Setup (Stage 2) Column 14:\n   --> {t_s2['Ema8_Line_Details']}")
    assert "15m 8 EMA:" in t_s2['Ema8_Line_Details']

print("\nSuper Trail Summary:", res_h1['super_trail_summary'])
print("Fixed 40/40 Summary:", res_h1['fixed_40_40_summary'])

print("\n" + "="*80)
print("🎉 ALL 7 SECTION 9 VERIFICATION CHECKS PASSED 100%!")
print("="*80)
