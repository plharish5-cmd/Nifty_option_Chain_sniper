# -*- coding: utf-8 -*-
"""Local web server. Binds to 127.0.0.1 only — nothing is exposed to the network."""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config as C
from . import core
from .universe import NIFTY50_SIDS


def page(name):
    base = getattr(sys, "_MEIPASS", None)
    cand = [os.path.join(base, "rvolscan", "ui", name)] if base else []
    cand.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", name))
    p = next((c for c in cand if os.path.exists(c)), cand[-1])
    with open(p, encoding="utf-8") as f:
        return f.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _html(self, s):
        b = s.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        cfg = C.load()
        try:
            if p in ("/", "/index.html"):
                return self._html(page("scan.html"))
            if p in ("/setup", "/settings"):
                return self._html(page("setup.html"))

            if p == "/api/config":
                c2 = json.loads(json.dumps(cfg))
                t = c2["dhan"].get("access_token") or ""
                c2["dhan"]["access_token"] = ("set (%d characters)" % len(t)) if t else ""
                c2["_token"] = C.token_info(cfg)
                c2["_problems"] = C.validate(cfg)
                c2["_nifty50"] = sorted(NIFTY50_SIDS.keys())
                c2["_paths"] = {"config": C.config_path(), "cache": C.cache_dir()}
                return self._send(c2)

            if p == "/api/scan":
                probs = C.validate(cfg)
                if probs:
                    return self._send({"error": " ".join(probs)}, 400)
                return self._send(core.scan(cfg, q.get("cutoff"), q.get("date")))

            if p == "/api/curve":
                probs = C.validate(cfg)
                if probs:
                    return self._send({"error": " ".join(probs)}, 400)
                return self._send(core.curve(cfg, (q.get("symbol") or "").upper(),
                                             q.get("date")))
            return self._send({"error": "unknown endpoint", "path": p}, 404)
        except Exception as e:
            return self._send({"error": str(e)[:300]}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8") or "{}")
        except Exception:
            return self._send({"error": "bad json"}, 400)
        if u.path == "/api/config":
            cfg = C.load()
            for sect in ("dhan", "scan", "server"):
                if isinstance(body.get(sect), dict):
                    tok = body[sect].get("access_token")
                    if sect == "dhan" and isinstance(tok, str) and tok.startswith("set ("):
                        body[sect].pop("access_token")     # unchanged placeholder
                    cfg[sect].update(body[sect])
            # a fresh token means the cached candles were fetched with the old
            # one and may be partial — clear so the next scan is clean
            if isinstance(body.get("dhan"), dict) and body["dhan"].get("access_token"):
                for f in os.listdir(C.cache_dir()):
                    try:
                        os.remove(os.path.join(C.cache_dir(), f))
                    except OSError:
                        pass
            C.save(cfg)
            return self._send({"ok": True, "_problems": C.validate(cfg),
                               "_token": C.token_info(cfg)})
        return self._send({"error": "unknown endpoint"}, 404)


def serve(cfg):
    port = int((cfg.get("server") or {}).get("port") or 7710)
    for p in range(port, port + 12):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            return httpd, p
        except OSError:
            continue
    raise RuntimeError("no free port in %d..%d" % (port, port + 12))
