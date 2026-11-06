"""Apply only Harish's approved watchlist change; preserve all other settings.
Run after stopping the 8510 daemon, since an old daemon can rewrite its config.
No broker/API calls, no live approval or partial-exit changes.
"""
from pathlib import Path
import json,os,tempfile

def apply(path):
    path=Path(path);cfg=json.loads(path.read_text())
    cfg.setdefault('algo_strike_selection',{})['LONG_CE_ITM']=False
    cfg['algo_strike_selection_evidence']='Harish explicitly requested LONG_CE_ITM disabled on 2026-11-07; other strike selection entries unchanged.'
    fd,name=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(cfg,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
    return path

if __name__=='__main__':
    print('LONG_CE_ITM disabled; all other runtime settings preserved: '+str(apply(Path(__file__).with_name('terminal8510_config.json'))))
