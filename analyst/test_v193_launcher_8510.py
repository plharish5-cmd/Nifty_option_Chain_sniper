"""Launcher port ownership tests; no host network or processes are touched."""
import json,os,shutil,subprocess,sys,time
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
def sandbox(tmp_path,relative,allowed,payload):
 script=tmp_path/relative;script.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/relative,script)
 binary=tmp_path/'.venv/bin';binary.mkdir(parents=True)
 python=binary/'python';python.write_text('#!/bin/sh\nPYTHONPATH="$FAKE_HOOK" exec "$TEST_PYTHON" "$@"\n');python.chmod(0o755)
 hook=tmp_path/'hooks';hook.mkdir()
 (hook/'sitecustomize.py').write_text('''import urllib.request,urllib.error,io,os
from pathlib import Path
class Reply(io.BytesIO):status=200
def local_only(url,*args,**kwargs):
    if '127.0.0.1:'+os.environ['FAKE_ALLOWED']+'/' not in url:raise urllib.error.URLError('not running')
    marker=Path(os.environ['OPENED_URL']+'.warming')
    if os.environ.get('FAKE_WAIT') and not marker.exists():
        marker.touch();raise urllib.error.URLError('starting')
    return Reply(os.environ['FAKE_PAYLOAD'].encode())
urllib.request.urlopen=local_only
''')
 tools=tmp_path/'tools';tools.mkdir()
 for name,body in {'ss':'printf "State\\nLISTEN\\n"','date':'case "$1" in +%u) echo 6;; +%H%M) echo 2000;; *) exit 91;; esac','xdg-open':'printf "%s" "$1" > "$OPENED_URL"','nohup':'exit 92','sleep':'exit 0'}.items():
  p=tools/name;p.write_text('#!/bin/sh\n'+body+'\n');p.chmod(0o755)
 env={**os.environ,'PATH':str(tools)+':'+os.environ['PATH'],'FAKE_HOOK':str(hook),'TEST_PYTHON':sys.executable,'FAKE_ALLOWED':str(allowed),'FAKE_PAYLOAD':payload,'OPENED_URL':str(tmp_path/'opened')}
 assert subprocess.run(['bash','-n',str(script)],capture_output=True).returncode==0
 return script,env
def opened(tmp_path):
 p=tmp_path/'opened'
 deadline=time.monotonic()+2
 while not p.exists() and time.monotonic()<deadline:time.sleep(.01)
 return p.read_text()
@pytest.mark.parametrize('valid',[True,False])
def test_algo_identity(tmp_path,valid):
 payload=json.dumps({'port':8510,'status':'ONLINE','execution_mode':'PAPER_ONLY','algo_only_execution':True}) if valid else '<html>Streamlit Paper Desk</html>'
 script,env=sandbox(tmp_path,Path('scripts/desk_launch.sh'),8510,payload)
 result=subprocess.run(['bash',str(script),'8510'],env=env,capture_output=True,text=True,timeout=5)
 if valid:
  assert result.returncode==0 and opened(tmp_path)=='http://127.0.0.1:8510/'
  (tmp_path/'opened').unlink()
  warm=subprocess.run(['bash',str(script),'8510'],env={**env,'FAKE_WAIT':'1'},capture_output=True,text=True,timeout=5)
  assert warm.returncode==0 and opened(tmp_path)=='http://127.0.0.1:8510/'
  assert 'without launching a duplicate' in warm.stdout
 else:
  assert result.returncode==1 and not (tmp_path/'opened').exists()
  assert 'belongs to another app' in result.stdout and 'No process was stopped' in result.stdout
@pytest.mark.parametrize('who,port',[('harish',8520),('wife',8511)])
def test_afterhours_ports(tmp_path,who,port):
 script,env=sandbox(tmp_path,Path('desk_launch.sh'),port,'ok')
 result=subprocess.run(['bash',str(script),who],env=env,capture_output=True,text=True,timeout=5)
 assert result.returncode==0 and opened(tmp_path)=='http://localhost:'+str(port)
