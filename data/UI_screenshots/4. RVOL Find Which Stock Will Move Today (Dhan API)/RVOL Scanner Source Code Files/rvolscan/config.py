# -*- coding: utf-8 -*-
"""Settings live in config.json next to the program. The Dhan token never
leaves this machine except to Dhan's own API."""
import base64, datetime, json, os, sys, threading

_LOCK = threading.Lock()

DEFAULT = {
    "dhan": {"client_id": "", "access_token": ""},
    "scan": {
        "base_days": 10,            # trailing completed sessions in the baseline
        "min_turnover_cr": 5,       # rupee floor, in crore, at the cutoff
        "default_cutoff": "09:45",
        "universe": "nifty50",      # nifty50 | custom
        "custom": []                # user's own NSE trading symbols
    },
    "server": {"port": 7710, "open_browser": True}
}


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ".w")
        open(p, "w").write("x")
        os.remove(p)
        return True
    except Exception:
        return False


def app_dir():
    c = getattr(app_dir, "_c", None)
    if c:
        return c
    here = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    if not _writable(here):
        here = os.path.join(os.environ.get("LOCALAPPDATA") or
                            os.path.expanduser("~"), "RVOLScanner")
        os.makedirs(here, exist_ok=True)
    app_dir._c = here
    return here


def config_path():
    return os.path.join(app_dir(), "config.json")


def cache_dir():
    d = os.path.join(app_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load():
    p = config_path()
    if not os.path.exists(p):
        save(DEFAULT)
        return json.loads(json.dumps(DEFAULT))
    try:
        with open(p, encoding="utf-8") as f:
            return _merge(DEFAULT, json.load(f))
    except Exception:
        return json.loads(json.dumps(DEFAULT))


def save(cfg):
    with _LOCK:
        p = config_path()
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(p + ".tmp", p)
    return cfg


def token_info(cfg):
    """Decode the JWT's expiry so the page can warn BEFORE a scan fails.

    An expired token is the single most common reason this stops working, and
    Dhan's error for it is not obvious, so we surface it up front.
    """
    tok = (cfg.get("dhan") or {}).get("access_token") or ""
    if not tok:
        return {"set": False}
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        j = json.loads(base64.urlsafe_b64decode(p))
        exp = datetime.datetime.fromtimestamp(j["exp"])
        return {"set": True, "expires": exp.strftime("%d %b %Y %H:%M"),
                "expired": datetime.datetime.now() >= exp,
                "client_id": str(j.get("dhanClientId") or "")}
    except Exception:
        return {"set": True, "expires": "unreadable", "expired": False,
                "client_id": ""}


def validate(cfg):
    out = []
    d = cfg.get("dhan") or {}
    if not d.get("access_token"):
        out.append("Dhan access token is empty. Open Setup and paste it in.")
    ti = token_info(cfg)
    if ti.get("set") and ti.get("expired"):
        out.append("The Dhan access token expired on %s. Generate a new one and "
                   "paste it in Setup." % ti.get("expires"))
    return out
