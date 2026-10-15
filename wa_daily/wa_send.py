#!/usr/bin/env python3
"""
wa_send.py — every morning: make a screenshot of the View Building Excel (or any desk page) and send it to a
WhatsApp group through WhatsApp Web, using a saved browser login (scan the QR once with --login).

  python wa_send.py --login          # first time only: opens WhatsApp Web, scan the QR on your phone, then close
  python wa_send.py --test           # makes the image and sends it now (use to check the group name)
  python wa_send.py                  # what cron runs at 7:30

Settings live in wa_config.json next to this file. Nothing else on the laptop is touched.
"""
from __future__ import annotations

import argparse, datetime as dt, json, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "wa_config.json").read_text())
PROFILE = Path(CFG.get("profile_dir", "~/.wa_profile")).expanduser()
LOG = HERE / "wa_send.log"


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with LOG.open("a") as f:
        f.write(line + "\n")


# ── 1. make the image ──────────────────────────────────────────────────────
def excel_to_png(xlsx: Path, out: Path) -> Path:
    """Looks like a LibreOffice screenshot of the sheet: one landscape page down to the TOTAL row, full
    Particulars column, white margins trimmed. Works on a copy — the original file is never touched."""
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    from PIL import Image, ImageChops
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "sheet.xlsx"
    wb = load_workbook(xlsx)
    ws = wb.active
    # rows kept for reference only (e.g. 18 = USD/INR) are dropped from the WhatsApp image
    for r in range(ws.max_row, 0, -1):
        if ws.cell(r, 1).value in CFG.get("hide_rows", [18]):
            ws.delete_rows(r)
    last = next((r for r in range(1, ws.max_row + 1) if str(ws.cell(r, 1).value or "").strip().upper() == "TOTAL"), ws.max_row)
    # bold text everywhere so it reads well on a phone (WhatsApp shrinks the photo)
    from copy import copy as _cp
    for row in ws.iter_rows(min_row=1, max_row=last):
        for cell in row:
            if cell.value is not None:
                f = _cp(cell.font)
                f.b = True
                cell.font = f
    ws.column_dimensions["B"].width = max(ws.column_dimensions["B"].width or 0, 50)
    ws.print_area = f"A1:{get_column_letter(ws.max_column)}{last}"
    # Size every row to its wrapped text, so long observations never spill into the next row.
    import math
    from openpyxl.styles import Alignment
    for r in range(1, last + 1):
        if not isinstance(ws.cell(r, 1).value, (int, float)):   # only the numbered parameter rows
            continue
        lines = 1
        for c in range(1, ws.max_column + 1):
            cell = ws.cell(r, c)
            v = cell.value
            if v is None:
                continue
            width = ws.column_dimensions[get_column_letter(c)].width or 10
            sz = (cell.font.sz if cell.font and cell.font.sz else 11)
            per_line = max(1, int(width * 1.05 * 11 / sz))   # bold is a little wider          # ~chars that fit across the column
            n = sum(max(1, math.ceil(len(part) / per_line)) for part in str(v).split("\n"))
            if n > 1:
                a = cell.alignment
                cell.alignment = Alignment(horizontal=a.horizontal, vertical=a.vertical or "center", wrap_text=True)
            lines = max(lines, n)
        if lines > 1:
            ws.row_dimensions[r].height = max(ws.row_dimensions[r].height or 0, 15.5 * lines + 3)
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    for k in ("left", "right", "top", "bottom", "header", "footer"):
        setattr(ws.page_margins, k, 0.1)
    wb.save(src)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(src)],
                   check=True, timeout=120, capture_output=True)
    subprocess.run(["pdftoppm", "-png", "-r", str(CFG.get("dpi", 200)), "-singlefile", str(tmp / "sheet.pdf"),
                    str(tmp / "page")], check=True, timeout=60)
    im = Image.open(tmp / "page.png").convert("RGB")
    bb = ImageChops.difference(im, Image.new("RGB", im.size, "white")).getbbox()
    if bb:
        im = im.crop((max(0, bb[0] - 8), max(0, bb[1] - 8), min(im.width, bb[2] + 8), min(im.height, bb[3] + 8)))
    im.save(out)
    return out


def page_to_png(page, url: str, out: Path) -> Path:
    p = page.context.new_page()
    p.set_viewport_size({"width": CFG.get("width", 1600), "height": CFG.get("height", 1000)})
    p.goto(url, wait_until="networkidle", timeout=90_000)
    p.wait_for_timeout(int(CFG.get("settle_ms", 6000)))
    p.screenshot(path=str(out), full_page=bool(CFG.get("full_page", False)))
    p.close()
    return out


# ── 2. send through WhatsApp Web ───────────────────────────────────────────
SEARCH = ['#side div[contenteditable="true"]', 'div[contenteditable="true"][role="textbox"][aria-label*="earch"]',
          'input[aria-label*="earch"]', 'input[placeholder*="earch"]', 'div[contenteditable="true"][data-tab="3"]', 'div[role="textbox"][aria-label*="Search"]',
          'div[contenteditable="true"][aria-label*="Search"]']
ATTACH = ['span[data-icon="plus-rounded"]', 'span[data-icon="plus"]', 'span[data-icon="attach-menu-plus"]',
          'div[title="Attach"]', 'button[title="Attach"]', '[aria-label="Attach"]']
SEND = ['span[data-icon="wds-ic-send-filled"]', 'span[data-icon="send"]', 'div[aria-label="Send"]', 'button[aria-label="Send"]']


def first(page, sels, timeout=15_000):
    end = time.time() + timeout / 1000
    while time.time() < end:
        for s in sels:
            loc = page.locator(s).first
            if loc.count() and loc.is_visible():
                return loc
        page.wait_for_timeout(400)
    raise RuntimeError(f"WhatsApp Web element not found: {sels[0]} (WhatsApp may have changed its page)")


def send(page, group: str, images: list[Path], caption: str) -> None:
    page.goto("https://web.whatsapp.com", timeout=90_000)
    end = time.time() + 120
    while time.time() < end:                            # wait until either chats or the login QR shows
        if page.locator("#pane-side, #side, div[aria-label='Chat list']").count():
            break
        if page.locator("canvas[aria-label*='can'], div[data-ref]").count():
            raise RuntimeError("WhatsApp Web is NOT logged in — run:  .venv/bin/python wa_send.py --login")
        page.wait_for_timeout(1000)
    page.wait_for_timeout(3000)
    direct = page.locator(f'#pane-side span[title="{group}"], div[aria-label="Chat list"] span[title="{group}"]').first
    if direct.count():                                  # chat already visible in the list → no search needed
        direct.click()
        page.wait_for_timeout(1500)
        return _post(page, group, images, caption)
    box = first(page, SEARCH, timeout=30_000)
    box.click(); box.fill(""); box.type(group, delay=40)
    page.wait_for_timeout(2500)
    hit = page.locator(f'span[title="{group}"]').first
    if not hit.count():
        raise RuntimeError(f'group "{group}" not found — check the exact name in wa_config.json')
    hit.click()
    page.wait_for_timeout(1500)
    return _post(page, group, images, caption)


def _post(page, group: str, images: list, caption: str) -> None:
    if page.get_by_text("Only admins can send messages").count():
        raise RuntimeError(f'"{group}" is admin-only — you cannot post there; send to an admin chat instead')
    for i, img in enumerate(images):
        first(page, ATTACH).click()
        page.wait_for_timeout(800)
        # Click the "Photos & videos" menu item and hand the file to its picker (never the Sticker item)
        item = page.get_by_text("Photos & videos", exact=True)
        if not item.count():
            item = page.get_by_text("Photos & Videos")
        if item.count():
            with page.expect_file_chooser(timeout=15_000) as fc:
                item.first.click()
            fc.value.set_files(str(img))
        else:                                            # older layout: pick the input that also accepts video
            inps = page.locator('input[type="file"]')
            pick = next((inps.nth(k) for k in range(inps.count())
                         if "video" in (inps.nth(k).get_attribute("accept") or "")), None)
            if pick is None:
                raise RuntimeError("Photos & videos option not found in the attach menu")
            pick.set_input_files(str(img))
        page.wait_for_timeout(2500)
        if caption and i == 0:
            page.keyboard.type(caption, delay=10)
        _press_send(page)
    page.wait_for_timeout(int(CFG.get("after_send_ms", 8000)))


def _press_send(page) -> None:
    """Send the attached image and confirm the preview closed (= WhatsApp accepted it)."""
    def preview_open() -> bool:
        return page.locator('span[data-icon="wds-ic-send-filled"], span[data-icon="send"]').count() > 0
    page.keyboard.press("Enter")                          # Enter in the caption box sends
    for _ in range(10):
        page.wait_for_timeout(1000)
        if not preview_open():
            break
    if preview_open():                                    # fallback: click the icon itself (it covers the button)
        try:
            first(page, SEND, timeout=10_000).click(force=True)
        except Exception:                                 # noqa: BLE001
            pass
        for _ in range(15):
            page.wait_for_timeout(1000)
            if not preview_open():
                break
    if preview_open():
        raise RuntimeError("image attached but WhatsApp did not send it (send button not pressed)")
    page.wait_for_timeout(6000)                           # let the upload finish before closing


def shot(page) -> None:
    try:
        page.screenshot(path=str(HERE / "wa_error.png"))
    except Exception:                                     # noqa: BLE001  browser already gone
        pass


def warmup(pw) -> None:
    """Separate, throw-away browser: open 8501, click View Building, wait for a freshly saved sheet."""
    br = pw.chromium.launch(headless=True, args=["--password-store=basic"])
    try:
        w = br.new_page(viewport={"width": 1600, "height": 1000})
        w.goto(CFG["warmup_url"], timeout=120_000)
        w.wait_for_timeout(15_000)
        lbl = CFG.get("tab_label", "View Building")
        tab = w.get_by_role("button", name=lbl, exact=True)
        if not tab.count():
            tab = w.get_by_text(lbl, exact=True)
        tab.first.click(timeout=60_000)                   # 8501 builds a tab only when it is selected
        xp0 = Path(CFG["sources"][0].get("path", "")).expanduser()
        t0 = time.time()
        while time.time() - t0 < int(CFG.get("warmup_s", 180)):
            if xp0.exists() and xp0.stat().st_mtime >= t0 - 5:
                break
            w.wait_for_timeout(3000)
        w.wait_for_timeout(3000)
    finally:
        br.close()


def wa_context(pw, login: bool = False):
    return pw.chromium.launch_persistent_context(str(PROFILE), headless=bool(CFG.get("headless", False)) and not login,
                                                 viewport={"width": 1400, "height": 900},
                                                 args=["--password-store=basic"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--test-me", action="store_true", help="send to Me even if live is on")
    ap.add_argument("--jitter", default="", help="random wait before starting, e.g. 0-900 (seconds)")
    a = ap.parse_args()
    a.test = a.test or a.test_me
    if a.jitter:
        import random
        lo, hi = (int(x) for x in a.jitter.split("-"))
        wait = random.randint(lo, hi)
        log(f"waiting {wait}s before sending (human-like timing)")
        time.sleep(wait)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        if a.login:
            ctx = wa_context(pw, login=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://web.whatsapp.com")
            print("Scan the QR with WhatsApp on your phone (Linked devices). Wait until chats show, then press Enter here.")
            input()
            ctx.close()
            return 0
        if dt.date.today().weekday() >= 5 and CFG.get("weekdays_only", True) and not a.test:
            log("weekend — skipped"); return 0

        # 1 + 2: fresh sheet → image (no WhatsApp browser open yet)
        try:
            if CFG.get("warmup_url"):
                warmup(pw)
            outdir = HERE / "shots"; outdir.mkdir(exist_ok=True)
            stamp = f"{dt.datetime.now():%Y%m%d_%H%M}"
            imgs: list[Path] = []
            for s in CFG["sources"]:
                if s["type"] != "excel":
                    continue
                xp = Path(s["path"]).expanduser()
                if not xp.exists():
                    raise RuntimeError(f"{xp} not found — is the 8501 app running?")
                age = (time.time() - xp.stat().st_mtime) / 60
                if age > float(CFG.get("max_age_min", 120)):
                    raise RuntimeError(f"{xp.name} is {age:.0f} min old — not sending stale data (is 8501 running?)")
                imgs.append(excel_to_png(xp, outdir / f"{stamp}_{s['name']}.png"))
            log(f"image ready: {', '.join(i.name for i in imgs)}")
        except Exception as e:                            # noqa: BLE001
            import traceback
            log(f"FAILED making the image: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            return 1

        # 3: WhatsApp
        cap = CFG.get("caption", "").replace("{date}", f"{dt.date.today():%d %b %Y}")
        if CFG.get("live") and not a.test_me:
            targets = CFG.get("live_targets") or []
        else:
            targets = CFG.get("send_to") or ["Me"]
        targets = [targets] if isinstance(targets, str) else targets
        rc = 0
        ctx = wa_context(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for tgt in targets:
            chain = tgt if isinstance(tgt, list) else [tgt]      # [group, fallback-if-admin-only, ...]
            for k, name in enumerate(chain):
                try:
                    send(page, name, imgs, cap)
                    log(f"sent {len(imgs)} image(s) to {name}")
                    break
                except Exception as e:                    # noqa: BLE001
                    shot(page)
                    admin_only = "admin-only" in str(e)
                    log(f"{'SKIP' if admin_only else 'FAILED'} {name}: {type(e).__name__}: {e}")
                    if not admin_only or k == len(chain) - 1:
                        rc = 1
                        break                             # real failure → don't spam the fallback
        try:
            ctx.close()
        except Exception:                                 # noqa: BLE001
            pass
        return rc


if __name__ == "__main__":
    sys.exit(main())
