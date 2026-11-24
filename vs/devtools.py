"""Developer view: what the app and the machine are doing, for whoever runs
it with start-dev.bat (VS_DEV=1). Nobody else sees any of it: the panel is
not rendered and every /dev route answers 404 unless VS_DEV is set."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from vs import db, llm
from vs.config import APP_DIR, DATA_DIR, DB_PATH, LOG_DIR, RAW_DIR

DEV = os.environ.get("VS_DEV") == "1"

UPDATE_SCRIPT = APP_DIR / "scripts" / "update_ai.ps1"
UPDATE_LOG = LOG_DIR / "update-ai.log"
# The start .bat files send Ollama's output here; the tray app writes server.log.
OLLAMA_LOGS = [APP_DIR / "data" / "logs" / "ollama.log", LOG_DIR / "ollama.log",
               Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama" / "server.log"]

_update_proc: subprocess.Popen | None = None
_nvidia: tuple[float, dict | None] = (0.0, None)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def human(n: float | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def nvidia() -> dict | None:
    """The graphics card as nvidia-smi sees it (asked at most every 10 s).
    None when there is no NVIDIA card or driver."""
    global _nvidia
    if time.monotonic() - _nvidia[0] < 10:
        return _nvidia[1]
    info = None
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run([exe, "--query-gpu=name,driver_version,memory.used,memory.total,utilization.gpu,temperature.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=False,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
            name, driver, used, total, util, temp = [x.strip() for x in out.splitlines()[0].split(",")]
            num = lambda v: round(float(v)) if v.replace(".", "", 1).isdigit() else None  # noqa: E731  ("47.19", "[N/A]")
            info = {"name": name, "driver": driver, "mem_used": num(used), "mem_total": num(total),
                    "util": num(util), "temp": num(temp)}
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            info = None
    _nvidia = (time.monotonic(), info)
    return info


def ollama() -> dict:
    """Ollama's version and the models it has loaded, with how much of each is on the GPU."""
    out: dict = {"host": llm.host(), "up": False, "version": None, "loaded": []}
    try:
        out["version"] = httpx.get(f"{llm.host()}/api/version", timeout=3).json().get("version")
        out["up"] = True
        for m in httpx.get(f"{llm.host()}/api/ps", timeout=3).json().get("models", []):
            size = m.get("size") or 0
            out["loaded"].append({"name": m.get("name"), "size": size, "context": m.get("context_length"),
                                  "gpu": (m.get("size_vram") or 0) / size if size else None,
                                  "until": (m.get("expires_at") or "")[11:19]})
    except (httpx.HTTPError, ValueError):
        pass
    return out


def git() -> str:
    try:
        r = subprocess.run(["git", "-C", str(APP_DIR), "log", "-1", "--format=%h %s"], capture_output=True, text=True, check=False,
                           timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        b = subprocess.run(["git", "-C", str(APP_DIR), "branch", "--show-current"], capture_output=True, text=True, check=False,
                           timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return f"{b.stdout.strip()} · {r.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        return "?"


def storage() -> dict:
    with db.session() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("opportunity", "match", "embedding", "http_cache", "page_snapshot", "run", "chat")}
        open_n = conn.execute("SELECT COUNT(*) FROM opportunity WHERE status='open'").fetchone()[0]
    wal = DB_PATH.with_name(DB_PATH.name + "-wal")
    return {"data_dir": str(DATA_DIR), "db": human(_size(DB_PATH) + _size(wal)), "raw": human(_size(RAW_DIR)),
            "counts": counts, "open": open_n}


def system() -> dict:
    return {"ollama": ollama(), "gpu": nvidia(), "gpu_state": dict(llm.gpu_state), "storage": storage(),
            "python": sys.version.split()[0], "os": f"{platform.system()} {platform.release()}", "git": git(),
            "speed": llm.live_speed()}


def recent_runs(n: int = 8) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute("SELECT id, started, ended, status, counts FROM run ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [db.row_dict(r) for r in rows]


def tail(path: Path, lines: int = 60) -> str:
    """The last lines of a text file, without reading a 25 MB log whole."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 64_000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def ollama_log() -> tuple[Path | None, str]:
    found = [p for p in OLLAMA_LOGS if p.exists()]
    if not found:
        return None, ""
    newest = max(found, key=lambda p: p.stat().st_mtime)
    return newest, tail(newest)


def last_run_log(lines: int = 40) -> str:
    with db.session() as conn:
        row = conn.execute("SELECT log FROM run ORDER BY id DESC LIMIT 1").fetchone()
    return "\n".join((row["log"] or "").splitlines()[-lines:]) if row else ""


# ---------------------------------------------------------------- actions

def update_running() -> bool:
    return _update_proc is not None and _update_proc.poll() is None


def start_update() -> str:
    """Run scripts/update_ai.ps1 in its own console window (you see what it
    does and can close it), writing to data/logs/update-ai.log."""
    global _update_proc
    if update_running():
        return "The update is already running."
    if not UPDATE_SCRIPT.exists():
        return f"Missing {UPDATE_SCRIPT}."
    if os.name != "nt":
        return "The update script is for Windows."
    _update_proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(UPDATE_SCRIPT),
         "-LogFile", str(UPDATE_LOG), "-NoPause"],
        cwd=APP_DIR, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return "Update started in a new window. Ollama stops for a minute: don't refresh meanwhile."


def clear_http_cache() -> str:
    with db.session() as conn:
        n = conn.execute("DELETE FROM http_cache").rowcount
    return f"Removed {n} saved pages: the next refresh downloads every page again."


def reload_sheet() -> str:
    from vs import sheet
    from vs.config import prefs
    sheet._cache["at"] = 0.0
    rows, error = sheet.rows(prefs().get("sheet", {}))
    return f"Sheet read again: {len(rows or [])} rows." + (f" Problem: {error}" if error else "")


def check_gpu() -> str:
    try:
        warning = llm.check_gpu(llm.extract_model())
    except llm.LLMError as e:
        return str(e)
    share = llm.gpu_state.get("share")
    return warning or (f"{llm.gpu_state.get('model')}: {share:.0%} on the GPU." if share is not None else "No model loaded.")
