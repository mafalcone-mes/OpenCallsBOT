"""The beta's limits, only in the compiled copy given to testers
(vs/build_info.py: BETA = True; scripts/build_beta.py). Running from the
code nothing here is ever active.

A beta copy stops working (it never deletes anything):

1. DAYS days after it was first opened on that computer. The first opening is
   written in two places (data/beta.json and one outside the app's folder:
   the registry on Windows, a small file on macOS/Linux) and the earliest
   wins, so deleting the data folder doesn't start the count again;
2. BUILD_CAP_DAYS days after the build, whatever happened on the computer;
3. when the shared Google Sheet has a row `kind = beta`, `enabled = false`
   (the owner's switch to end every copy at once).

The time used is the latest of the computer's clock, the time already seen,
and the `Date` of a web reply when online, so setting the clock back
doesn't help."""

from __future__ import annotations

import json
import os
import sys
import time
import tomllib
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from vs import build_info

DAYS = 5
BUILD_CAP_DAYS = 30
CHECK_EVERY_S = 60  # the status is worked out at most once a minute
REG_KEY = r"Software\Oppo\Beta"

_cache: dict = {"at": 0.0, "status": None}


def active() -> bool:
    return bool(build_info.BETA)


def feedback_url() -> str:
    return build_info.FEEDBACK_URL


# ---------------------------------------------------------------- stored times

def _data_file() -> Path:
    from vs.config import DATA_DIR
    return DATA_DIR / "beta.json"


def _outside_file() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "com.oppo.beta.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "oppo-beta.json"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _read_registry() -> dict:
    if sys.platform != "win32":
        return {}
    import winreg
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            for name in ("first_run", "last_seen"):
                try:
                    out[name] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    pass
    except OSError:
        pass
    return out


def _write_registry(data: dict) -> None:
    if sys.platform != "win32":
        return
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            for name, value in data.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    except OSError:
        pass


def _stores() -> list[tuple]:
    """(read, write) for every place the times are kept."""
    out = [(lambda: _read_json(_data_file()), lambda d: _write_json(_data_file(), d))]
    if sys.platform == "win32":
        out.append((_read_registry, _write_registry))
    else:
        out.append((lambda: _read_json(_outside_file()), lambda d: _write_json(_outside_file(), d)))
    return out


def _parse(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- the time

def web_time(timeout: float = 4.0) -> datetime | None:
    """The time from a web reply's Date header (None offline)."""
    try:
        import httpx
        r = httpx.head("https://www.google.com/generate_204", timeout=timeout)
        return parsedate_to_datetime(r.headers["date"]).astimezone(timezone.utc)
    except Exception:
        return None


def remember(now: datetime | None = None, web: datetime | None = None) -> tuple[datetime, datetime]:
    """Record this opening; returns (first opening, current time)."""
    stored = [read() for read, _ in _stores()]
    firsts = [d for s in stored if (d := _parse(s.get("first_run", "")))]
    seens = [d for s in stored if (d := _parse(s.get("last_seen", "")))]
    clock = now or datetime.now(timezone.utc)
    current = max([clock] + seens + ([web] if web else []))
    first = min(firsts) if firsts else current
    for _, write in _stores():
        write({"first_run": _iso(first), "last_seen": _iso(current)})
    return first, current


# ---------------------------------------------------------------- the owner's switch

def switched_off() -> bool:
    """A row `kind = beta` with `enabled = false` in the shared sheet."""
    if os.environ.get("VS_NO_SHEET"):
        return False
    from vs import config, sheet
    try:
        path = config.PREFS_PATH if config.PREFS_PATH.exists() else config.EXAMPLE_PREFS
        with open(path, "rb") as f:
            cfg = tomllib.load(f).get("sheet", {})
    except (OSError, tomllib.TOMLDecodeError):
        return False
    if not cfg.get("url"):
        return False
    rows, _ = sheet.rows(cfg)
    return switch_in(rows or [])


def switch_in(rows: list[dict]) -> bool:
    from vs.sheet import _truthy
    return any(r.get("kind", "").lower() == "beta" and not _truthy(r.get("enabled")) for r in rows)


# ---------------------------------------------------------------- status

def evaluate(first: datetime, current: datetime, build_date: str, off: bool) -> dict:
    """Pure: what the beta's state is at `current`."""
    ends = first + timedelta(days=DAYS)
    try:
        cap = datetime.combine(date.fromisoformat(build_date), datetime.min.time(), timezone.utc) + timedelta(days=BUILD_CAP_DAYS)
        ends = min(ends, cap)
    except ValueError:
        pass
    over = "off" if off else "time" if current >= ends else ""
    left = max(0.0, (ends - current).total_seconds())
    return {"active": True, "over": over, "ends": ends, "days_left": int(left // 86400), "hours_left": int(left // 3600)}


def status(force: bool = False) -> dict:
    """The beta's state, worked out at most once a minute."""
    if not active():
        return {"active": False, "over": ""}
    if not force and _cache["status"] and time.time() - _cache["at"] < CHECK_EVERY_S:
        return _cache["status"]
    first, current = remember(web=web_time())
    st = evaluate(first, current, build_info.BUILD_DATE, switched_off())
    _cache.update(at=time.time(), status=st)
    return st


def locked() -> str:
    """'' while the beta runs, else why it stopped (for the command line)."""
    st = status()
    if not st["over"]:
        return ""
    return "The Oppo beta has ended. Thank you for trying it!" + (f" Feedback: {feedback_url()}" if feedback_url() else "")
