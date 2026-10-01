import os
import sys
import tempfile
from pathlib import Path

# Keep tests away from the real database and examples.
_tmp = Path(tempfile.mkdtemp(prefix="vs-test-"))
os.environ.setdefault("VS_TEST_TMP", str(_tmp))
os.environ["VS_NO_SHEET"] = "1"  # tests never read the Google Sheet
# Tests use the shipped template, never anyone's personal preferences.toml
os.environ["VS_PREFERENCES"] = str(Path(__file__).resolve().parent.parent / "preferences.example.toml")

import vs.config as config  # noqa: E402

config.DATA_DIR = _tmp
config.DB_PATH = _tmp / "app.db"
config.EXAMPLES_DIR = _tmp / "examples"
config.SIGNALS_PATH = _tmp / "signals.jsonl"
config.EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)

import vs.db  # noqa: E402
import vs.examples  # noqa: E402

vs.db.DB_PATH = config.DB_PATH
vs.examples.EXAMPLES_DIR = config.EXAMPLES_DIR
vs.examples.SIGNALS_PATH = config.SIGNALS_PATH

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(Path(__file__).parent))
