"""Build the beta for testers: one compiled program, no source code.

    uv run --with nuitka python scripts/build_beta.py [--feedback URL] [--version beta-1]

Nuitka turns the Python into C and compiles it (needs a C compiler: Visual
Studio Build Tools on Windows, Xcode command line tools on macOS). It builds
for the system it runs on; .github/workflows/beta.yml builds Windows and
macOS. Output: dist/Oppo-<version>-<windows|macos>.zip.

While it builds, vs/build_info.py is switched to the beta values (5 days
from the first opening, vs/beta.py) and put back afterwards. Only the files
listed below go in: never preferences.toml, data/, output/ or studios.toml."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INFO = ROOT / "vs" / "build_info.py"
DIST = ROOT / "dist"
FEEDBACK = "https://docs.google.com/forms/d/e/1FAIpQLSfdmveaSkqL7pwKzj4ByDDUQUXN6G4stlJMF6OcjtAPv4iA5Q/viewform"

DATA_DIRS = ["vs/web/templates", "vs/web/static", "portfolio-template"]
DATA_FILES = ["sources-snapshot.csv", "preferences.example.toml"]
# packages that read their own non-Python files at run time
PACKAGE_DATA = ["trafilatura", "justext", "tld", "babel", "icalendar", "tls_client", "certifi", "tzdata", "dateparser"]
# packages that import parts of themselves by name at run time: included whole
WHOLE_PACKAGES = ["vs", "uvicorn", "multipart", "dateparser", "starlette", "fastapi"]
PERSONAL = ["preferences.toml", "data", "output", "studios.toml"]

MAC_LAUNCHER = """#!/bin/sh
# Double-click to open Oppo. The first time macOS asks: right-click > Open.
cd "$(dirname "$0")"
xattr -dr com.apple.quarantine . 2>/dev/null  # downloaded files are blocked until cleared
echo "Oppo is running. Close this window to quit."
./Oppo/Oppo
"""


def system() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def nuitka_cmd(out: Path) -> list[str]:
    cmd = [sys.executable, "-m", "nuitka", "--standalone", "--assume-yes-for-downloads",
           f"--output-dir={out}", "--output-filename=Oppo", "--python-flag=no_docstrings",
           "--noinclude-pytest-mode=nofollow", "--noinclude-setuptools-mode=nofollow"]
    cmd += [f"--include-package={p}" for p in WHOLE_PACKAGES]
    cmd += [f"--include-data-dir={ROOT / d}={d}" for d in DATA_DIRS]
    cmd += [f"--include-data-files={ROOT / f}={f}" for f in DATA_FILES]
    cmd += [f"--include-package-data={p}" for p in PACKAGE_DATA]
    if sys.platform == "win32":
        cmd += ["--windows-console-mode=force", f"--windows-icon-from-ico={ROOT / 'vs/web/static/oppo/oppo.ico'}"] \
            if (ROOT / "vs/web/static/oppo/oppo.ico").exists() else ["--windows-console-mode=force"]
    cmd.append(str(ROOT / "oppo.py"))
    return cmd


def write_info(feedback: str) -> None:
    INFO.write_text(f'"""Written by scripts/build_beta.py for the beta build."""\n\nBETA = True\n'
                    f'BUILD_DATE = "{date.today().isoformat()}"\nFEEDBACK_URL = "{feedback}"\n', encoding="utf-8")


def check_clean(folder: Path) -> None:
    """Stop if anything personal or any source file of the app got in."""
    bad = [folder / n for n in PERSONAL if (folder / n).exists()]
    bad += [p for p in (folder / "vs").rglob("*.py")] if (folder / "vs").exists() else []
    if bad:
        sys.exit("Personal files or app sources in the build: " + ", ".join(str(p) for p in bad[:10]))


def selfcheck(program: Path) -> None:
    """Run the compiled program's own check (vs/selfcheck.py) in an empty home folder."""
    import os
    import tempfile
    exe = program / ("Oppo.exe" if sys.platform == "win32" else "Oppo")
    with tempfile.TemporaryDirectory() as home:
        r = subprocess.run([str(exe), "selfcheck"], env={**os.environ, "VS_HOME": home, "VS_NO_SHEET": "1"}, timeout=600)
    if r.returncode:
        sys.exit("The compiled program failed its selfcheck: not packaged.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feedback", default=FEEDBACK)
    ap.add_argument("--version", default=f"beta-{date.today().isoformat()}")
    args = ap.parse_args()

    build = DIST / "build"
    shutil.rmtree(build, ignore_errors=True)
    original = INFO.read_text(encoding="utf-8")
    write_info(args.feedback)
    try:
        subprocess.run(nuitka_cmd(build), check=True, cwd=ROOT)
    finally:
        INFO.write_text(original, encoding="utf-8")

    program = build / "oppo.dist"
    check_clean(program)
    selfcheck(program)
    package = DIST / f"Oppo-{args.version}-{system()}"
    shutil.rmtree(package, ignore_errors=True)
    if sys.platform == "darwin":  # a folder with the program and a launcher that opens Terminal
        package.mkdir(parents=True)
        shutil.copytree(program, package / "Oppo")
        launcher = package / "Open Oppo.command"
        launcher.write_text(MAC_LAUNCHER, encoding="utf-8")
        launcher.chmod(0o755)
    else:
        shutil.copytree(program, package)
        (package / "START HERE.txt").write_text(
            "Double-click Oppo.exe to open Oppo.\r\nIf Windows says it protected your PC: More info > Run anyway.\r\n"
            "A black window stays open while Oppo runs: close it to quit.\r\n", encoding="utf-8", newline="")
    archive = shutil.make_archive(str(package), "zip", package.parent, package.name)
    print(f"Built {archive}")


if __name__ == "__main__":
    main()
