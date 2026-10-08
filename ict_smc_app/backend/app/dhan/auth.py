"""
dhan/auth.py — access-token housekeeping.

Dhan access tokens last 24 hours (SEBI rule). Three ways to get a fresh one:
  1. MANUAL (default, safest): web.dhan.co → My Profile → Access DhanHQ APIs → generate token, then
        sed -i 's/^DHAN_ACCESS_TOKEN=.*/DHAN_ACCESS_TOKEN=<new token>/' .env
     The backend notices the .env change within seconds — no restart.
  2. RENEW (AUTO_RENEW_TOKEN=1 in .env): at 08:35 GET /v2/RenewToken extends a still-valid WEB-generated token by
     24 h. It cannot revive an expired token — if the laptop was off for a day you are back to option 1.
  3. TOTP (your OC app's dhan_client.refresh_token): needs DHAN_PIN + DHAN_TOTP_SECRET in .env, i.e. both factors in
     one file. Not wired in here on purpose; use your existing script if you accept that trade-off.

Never commit .env. `chmod 600 .env`.
"""
from __future__ import annotations

import re

from .. import config


async def renew_into_env(rest) -> str:
    try:
        new = await rest.renew_token()
    except Exception as e:                                   # noqa: BLE001
        return f"renew failed: {str(e)[:120]}"
    if not new:
        return "renew returned no token"
    p = config.env_file()
    txt = p.read_text(encoding="utf-8")
    if re.search(r"^DHAN_ACCESS_TOKEN=.*$", txt, flags=re.M):
        txt = re.sub(r"^DHAN_ACCESS_TOKEN=.*$", f"DHAN_ACCESS_TOKEN={new}", txt, flags=re.M)
    else:
        txt += f"\nDHAN_ACCESS_TOKEN={new}\n"
    p.write_text(txt, encoding="utf-8")
    config.load_env(force=True)
    return "renewed for 24 h"
