"""First opening without preferences.toml: the Welcome page (/setup) asks a
few questions, writes preferences.toml from preferences.example.toml, and
downloads the local models through Ollama. This is the install for people
who don't use a terminal (the beta for testers); INSTALL_FOR_AI.md is the
long way for an AI assistant."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import tomlkit

from vs import config

OLLAMA = "http://127.0.0.1:11434"
OLLAMA_DOWNLOAD = {"win32": "https://ollama.com/download/windows", "darwin": "https://ollama.com/download/mac"}
SENIORITY = ["", "student", "junior", "mid-level", "senior"]
OPEN_TO = ["jobs", "internships", "freelance work", "residencies", "open calls", "grants", "research positions"]


# ---------------------------------------------------------------- the machine

def ram_gb() -> float:
    try:
        if sys.platform == "win32":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MemoryStatus()
            m.dwLength = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 2**30
        if sys.platform == "darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=5)) / 2**30
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except Exception:
        return 16.0


def models_for(gb: float) -> dict:
    """The table in INSTALL_FOR_AI.md step 1, by memory."""
    if gb >= 15:
        return {"model": "qwen3.5:9b", "fallback_model": "qwen3:8b", "embed_model": "bge-m3",
                "num_ctx": 8192 if gb >= 30 else 6144}
    return {"model": "qwen3:4b", "fallback_model": "qwen3:1.7b", "embed_model": "qwen3-embedding:0.6b", "num_ctx": 4096}


def ollama_running() -> bool:
    import httpx
    try:
        return httpx.get(f"{OLLAMA}/api/version", timeout=2).status_code == 200
    except Exception:
        return False


def start_ollama() -> None:
    """Start Ollama if it is installed but not running (as start.bat does)."""
    if ollama_running():
        return
    try:
        if sys.platform == "win32":
            exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
            if exe.exists():
                subprocess.Popen([str(exe), "serve"], creationflags=subprocess.CREATE_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-g", "-a", "Ollama"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def ollama_download_url() -> str:
    return OLLAMA_DOWNLOAD.get(sys.platform, "https://ollama.com/download")


# ---------------------------------------------------------------- preferences.toml

def _words(text: str) -> list[str]:
    return [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]


def write_prefs(answers: dict, path: Path | None = None, gb: float | None = None) -> Path:
    """preferences.toml from the example and the Welcome page's answers.
    answers: email, roles, avoid, countries, residence, citizenships,
    languages, seniority, open_to (list), folder (a folder-format portfolio)."""
    path = path or config.PREFS_PATH
    doc = tomlkit.parse(config.EXAMPLE_PREFS.read_text(encoding="utf-8"))
    folder = (answers.get("folder") or "").strip()
    if folder:
        doc["portfolio"]["format"], doc["portfolio"]["dir"] = "folder", str(Path(folder).expanduser())
    else:  # the PDFs uploaded on the Welcome page, kept in data/profile/
        doc["portfolio"]["format"], doc["portfolio"]["dir"] = "pdf", str(config.DATA_DIR / "profile")
    me = doc["me"]
    me["contact_email"] = (answers.get("email") or "").strip()
    me["residence_country"] = (answers.get("residence") or "").strip().upper()
    me["citizenships"] = [c.upper() for c in _words(answers.get("citizenships", ""))]
    me["languages"] = {code.lower(): "fluent" for code in _words(answers.get("languages", ""))}
    if answers.get("seniority") in SENIORITY:
        me["seniority"] = answers["seniority"]
    me["open_to"] = [o for o in answers.get("open_to", []) if o in OPEN_TO]
    roles = _words(answers.get("roles", ""))
    doc["taste"]["keywords_plus"] = roles
    doc["taste"]["keywords_minus"] = _words(answers.get("avoid", ""))
    doc["search"]["queries"] = roles
    doc["search"]["countries"] = [c.lower() for c in _words(answers.get("countries", ""))]
    for k, v in models_for(ram_gb() if gb is None else gb).items():
        doc["llm"][k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    tmp.replace(path)
    return path


# ---------------------------------------------------------------- model download

pulls: dict[str, dict] = {}  # model: {status, completed, total, error, done}
_lock = threading.Lock()


def needed_models() -> list[str]:
    cfg = config.prefs().get("llm", {})
    return [m for m in (cfg.get("model"), cfg.get("embed_model")) if m]


def _pull(model: str) -> None:
    import httpx
    st = pulls[model]
    try:
        with httpx.stream("POST", f"{OLLAMA}/api/pull", json={"model": model, "stream": True}, timeout=None) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    st["error"] = msg["error"]
                    break
                st["status"] = msg.get("status", st["status"])
                if msg.get("total"):
                    st["total"], st["completed"] = msg["total"], msg.get("completed", 0)
        st["done"] = not st["error"]
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {e}"
    finally:
        st["running"] = False


def start_pulls() -> None:
    """Download the models preferences.toml names (Ollama skips what it has)."""
    with _lock:
        for m in needed_models():
            st = pulls.get(m)
            if st and (st["running"] or st["done"]):
                continue
            pulls[m] = {"status": "starting", "completed": 0, "total": 0, "error": "", "done": False, "running": True}
            threading.Thread(target=_pull, args=(m,), daemon=True).start()


def pull_state() -> list[dict]:
    out = []
    for m in needed_models():
        st = pulls.get(m) or {"status": "waiting", "completed": 0, "total": 0, "error": "", "done": False, "running": False}
        pct = round(100 * st["completed"] / st["total"]) if st["total"] else 0
        out.append({"model": m, **st, "pct": 100 if st["done"] else pct,
                    "gb": round(st["total"] / 1e9, 1) if st["total"] else None})
    return out
