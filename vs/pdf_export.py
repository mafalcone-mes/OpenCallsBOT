"""PDFs:
- the tailored portfolio, by calling the website's own scripts/export-portfolio.mjs
  (starting the site's dev server on our own port when nothing is running);
- briefs and reports, by printing our own HTML with headless Chrome/Edge.

The website folder is only read. Its dev server writes its own `.next/` cache
there; that is the site's build cache, not content."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from vs.config import LOG_DIR, portfolio_dir, prefs

BROWSERS = {
    "win32": [
        r"C:/Program Files/Google/Chrome/Application/chrome.exe",
        r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ],
    "linux": ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge"],
}


class ExportError(RuntimeError):
    def __init__(self, message: str, manual_command: str = ""):
        super().__init__(message)
        self.manual_command = manual_command


def find_browser() -> str:
    explicit = os.environ.get("PORTFOLIO_BROWSER")
    if explicit:
        return explicit
    for b in BROWSERS.get(sys.platform, BROWSERS["linux"]):
        if Path(b).exists():
            return b
    raise ExportError("No Chrome, Edge or Chromium found; install Chrome or set PORTFOLIO_BROWSER to its path.")


def html_to_pdf(html_path: Path, pdf_path: Path) -> Path:
    browser = find_browser()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    profile = LOG_DIR / "chrome-print-profile"
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={profile}",
           f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not pdf_path.exists():
        raise ExportError(f"Printing failed: {out.stderr[-400:]}")
    return pdf_path


def _up(url: str) -> bool:
    try:
        httpx.head(url, timeout=3, follow_redirects=True)
        return True
    except httpx.HTTPError:
        return False


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@dataclass
class DevServer:
    base: str
    proc: subprocess.Popen | None = None

    def stop(self) -> None:
        """Stop the dev server and the node processes it spawned."""
        if not self.proc or self.proc.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
        else:
            import os
            import signal
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.proc.terminate()


def ensure_site(log=print) -> DevServer:
    cfg = prefs().get("portfolio", {})
    base = os.environ.get("PORTFOLIO_BASE") or cfg.get("base", "http://localhost:3000")
    if _up(base):
        log(f"Using the site already running at {base}")
        return DevServer(base)
    site = portfolio_dir()
    if not (site / "node_modules").is_dir():
        raise ExportError("The website has no node_modules. Run 'npm install' in the website folder once.",
                          f'cd "{site}" && npm install')
    port = int(cfg.get("own_port", 3107))
    if not _port_free(port):
        raise ExportError(f"Port {port} is busy; change [portfolio].own_port in preferences.toml.")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise ExportError("npm not found on PATH.")
    log_file = open(LOG_DIR / "site-dev-server.log", "w", encoding="utf-8")
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
             else {"start_new_session": True})  # its own process group, so stop() ends it all
    proc = subprocess.Popen([npm, "run", "dev", "--", "-p", str(port)], cwd=site, stdout=log_file,
                            stderr=subprocess.STDOUT, **group)
    server = DevServer(f"http://localhost:{port}", proc)
    log(f"Starting the site on port {port} (first compile can take a minute)")
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            raise ExportError(f"The site's dev server exited; see {LOG_DIR / 'site-dev-server.log'}")
        try:
            r = httpx.get(f"{server.base}/portfolio-builder", timeout=60)
            if r.status_code == 200:
                return server
        except httpx.HTTPError:
            pass
        time.sleep(2)
    server.stop()
    raise ExportError("The site did not start within 120 s.", f'cd "{site}" && npm run dev')


def export_portfolio(selection: list[str], out: Path, *, cover: bool = True, about: bool = True,
                     full_res: bool = False, log=print) -> Path:
    """selection: ["slug:category:images", ...] in page order."""
    site = portfolio_dir()
    args = ["--projects", ",".join(selection), "--out", str(out.resolve())]
    if not cover:
        args.append("--no-cover")
    if not about:
        args.append("--no-about")
    if full_res:
        args.append("--full-res")
    manual = f'cd "{site}" && node scripts/export-portfolio.mjs ' + " ".join(f'"{a}"' if " " in a else a for a in args)
    server = ensure_site(log)
    try:
        cmd = ["node", "scripts/export-portfolio.mjs", "--base", server.base, *args]
        out.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(cmd, cwd=site, capture_output=True, text=True, timeout=240)
        if res.returncode != 0 or not out.exists():
            raise ExportError(f"Portfolio export failed: {(res.stderr or res.stdout).strip()[-400:]}", manual)
        log(res.stdout.strip())
        return out
    except subprocess.TimeoutExpired as e:
        raise ExportError("Portfolio export timed out.", manual) from e
    finally:
        server.stop()
