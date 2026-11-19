"""Owner-approved: set Prototype touch cadence to 3s only."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def apply(path: Path) -> bool:
    path = Path(path)
    original = path.read_bytes()
    cfg = json.loads(original.decode("utf-8"))
    block = cfg.setdefault("scanner", {}).setdefault("ema8_prototype", {})
    if block.get("e8_refresh_seconds") == 3.0:
        return False
    backup_dir = path.parent.parent / "data" / "terminal8510" / "config_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"pre_v240_cadence_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(original)
    block["e8_refresh_seconds"] = 3.0
    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(mode="w", dir=str(path.parent), prefix=".v240-", delete=False) as stream:
        temp = Path(stream.name)
        json.dump(cfg, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    try:
        os.chmod(temp, mode)
        if path.read_bytes() != original:
            raise RuntimeError("Config changed concurrently; no update applied")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return True


if __name__ == "__main__":
    changed = apply(ROOT / "analyst" / "terminal8510_config.json")
    print(f"Prototype e8_refresh_seconds -> 3.0; changed={changed}. Restart engine on 8510.")