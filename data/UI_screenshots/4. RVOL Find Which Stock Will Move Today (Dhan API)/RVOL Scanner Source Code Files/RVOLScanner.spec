# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[('rvolscan/ui/scan.html', 'rvolscan/ui'),
           ('rvolscan/ui/setup.html', 'rvolscan/ui'),
           ('pkg/docs', 'docs')],
    hiddenimports=[],
    excludes=['pandas','numpy','pyarrow','matplotlib','scipy','tkinter','PIL',
              'dhanhq','websocket','duckdb'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='RVOLScanner',
    debug=False, bootloader_ignore_signals=False, strip=False,
    upx=True, upx_exclude=[], runtime_tmpdir=None,
    console=False, disable_windowed_traceback=False,
    argv_emulation=False, target_arch=None, codesign_identity=None,
    entitlements_file=None,
)
