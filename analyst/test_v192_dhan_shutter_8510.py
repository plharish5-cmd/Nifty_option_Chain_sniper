from pathlib import Path
import re,json
from analyst.terminal8510_backend import QuantTerminalBackend
HTML=(Path(__file__).resolve().parents[1]/'Algorithmic_Trading_Workspace_8510.html').read_text()
def test_blank_token_no_saved_eye():
 token=re.search(r'<input[^>]*id="dhanAccessTokenInput"[^>]*>',HTML).group()
 for attr in ('value=""','autocomplete="off"','autocapitalize="off"','spellcheck="false"'):assert attr in token
 assert "toggleApiFieldVisibility('dhanAccessTokenInput'" not in HTML
 assert 'if (requireFresh && !typedToken)' in HTML
 assert 'saveDhanApiConfig(false, true)' in HTML

def test_shutter_native_global_and_persisted():
 assert '<details id="dhanApiConfigCard"' in HTML and 'dhanPanelCollapsed' in HTML
 assert 'localStorage.getItem' in HTML and 'dhanAutoCollapse' in HTML

def test_client_id_locked_and_metadata_never_discloses_token():
 tag=re.search(r'<input[^>]*id="dhanClientIdInput"[^>]*>',HTML).group();assert 'readonly disabled' in tag
 b=object.__new__(QuantTerminalBackend);b.cfg={'dhan':{'client_id':'FIXED'}}
 assert b.update_dhan_config({'client_id':'OTHER'})['ok'] is False
 public=b._public_dhan_status({'masked_token':'****1234','access_token':'SECRET','last_status':{'message':'using ****1234'}})
 assert 'SECRET' not in json.dumps(public) and '1234' not in json.dumps(public)
