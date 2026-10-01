# -*- coding: utf-8 -*-
"""Windows shell: starts the local server, opens the browser, writes a log."""
import os, sys, time, threading, traceback, webbrowser

from . import config as C
from . import server

LOG = None


def _res(*parts):
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if LOG:
            LOG.write(line + "\n")
            LOG.flush()
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def first_run():
    """Drop the docs beside the program once, so the folder explains itself."""
    root = C.app_dir()
    for name in ("README.txt", "INSTALL.txt"):
        dst, src = os.path.join(root, name), _res("docs", name)
        if not os.path.exists(dst) and os.path.exists(src):
            try:
                with open(src, "rb") as a, open(dst, "wb") as b:
                    b.write(a.read())
            except Exception:
                pass


def main():
    global LOG
    try:
        LOG = open(os.path.join(C.app_dir(), "rvolscanner.log"), "a", encoding="utf-8")
    except Exception:
        LOG = None
    try:
        first_run()
        cfg = C.load()
        httpd, port = server.serve(cfg)
        url = "http://127.0.0.1:%d/" % port
        problems = C.validate(cfg)
        log("RVOL Scanner up on %s" % url)
        if problems:
            log("setup needed: " + " ".join(problems))
            url += "setup"
        if (cfg.get("server") or {}).get("open_browser", True):
            threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)),
                             daemon=True).start()
        httpd.serve_forever()
    except Exception:
        log("FATAL\n" + traceback.format_exc())
        return 1
    return 0
