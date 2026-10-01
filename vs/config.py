"""App paths and preferences.toml access."""

import os
import tomllib
from pathlib import Path

import tomlkit

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
EXAMPLES_DIR = DATA_DIR / "examples"
RAW_DIR = DATA_DIR / "raw"
LOG_DIR = DATA_DIR / "logs"
OUTPUT_DIR = APP_DIR / "output"
DB_PATH = DATA_DIR / "app.db"
SIGNALS_PATH = DATA_DIR / "signals.jsonl"
PREFS_PATH = Path(os.environ.get("VS_PREFERENCES", APP_DIR / "preferences.toml"))

for d in (DATA_DIR, EXAMPLES_DIR, RAW_DIR, LOG_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)


_last_good: dict | None = None
prefs_error: str | None = None


def prefs() -> dict:
    """Preferences, re-read every call so edits apply without a restart. If
    the file has a typo, the last valid version is used (and prefs_error set)
    so a running refresh never crashes on a half-saved edit."""
    global _last_good, prefs_error
    if not PREFS_PATH.exists():
        raise FileNotFoundError(
            f"{PREFS_PATH.name} not found. Copy preferences.example.toml to preferences.toml and fill it in "
            "(INSTALL_FOR_AI.md explains each setting).")
    try:
        with open(PREFS_PATH, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        prefs_error = f"preferences.toml has an error ({e}); using the last valid version"
        if _last_good is None:
            raise
        return _last_good
    _last_good, prefs_error = data, None
    from vs.sheet import apply_to_prefs  # the Google Sheet is the master list of sources
    return apply_to_prefs(data)


def update_prefs(mutate) -> None:
    """Edit preferences.toml keeping comments and layout (tomlkit)."""
    doc = tomlkit.parse(PREFS_PATH.read_text(encoding="utf-8"))
    mutate(doc)
    PREFS_PATH.write_text(tomlkit.dumps(doc), encoding="utf-8")


def portfolio_dir() -> Path:
    raw = os.environ.get("PORTFOLIO_DIR") or prefs().get("portfolio", {}).get("dir", "../website")
    path = Path(raw)
    return (path if path.is_absolute() else APP_DIR / path).resolve()


def user_agent() -> str:
    email = prefs().get("me", {}).get("contact_email", "")
    contact = f"; {email}" if email else ""
    return f"vacancies-searcher/0.1 (personal, non-commercial{contact})"
