"""Oppo, the little robot that looks for opportunities: the app's mascot.

Its drawings are SVG files in static/oppo/<pose>.svg (see the README there).
They are inlined, so parts drawn in `currentColor` or `var(--oppo)` follow
the light and dark themes. A missing pose falls back to default.svg, and a
missing default to a plain placeholder mark (not a drawing of Oppo).
"""

import re
from pathlib import Path

from markupsafe import Markup

DIR = Path(__file__).parent / "static" / "oppo"
POSES = ("default", "searching", "found", "empty", "sleep")

# Until the drawing exists: a rounded frame with one green "beacon".
PLACEHOLDER = ('<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="3" width="18" height="18" rx="6" '
               'stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="3.5" style="fill: var(--oppo)"/></svg>')


def svg_source(pose: str = "default") -> str:
    """The raw SVG for a pose: its own file, else default.svg, else the placeholder."""
    for name in (pose, "default"):
        path = DIR / f"{name}.svg"
        if name.isidentifier() and path.is_file():
            return path.read_text(encoding="utf-8")
    return PLACEHOLDER


def oppo(pose: str = "default", size: int = 48, label: str = "") -> Markup:
    """Oppo inline at `size` px. Decorative (hidden from screen readers)
    unless `label` describes it."""
    src = re.sub(r"<\?xml.*?\?>|<!DOCTYPE.*?>|<!--.*?-->", "", svg_source(pose), flags=re.S).strip()
    a11y = f' role="img" aria-label="{Markup.escape(label)}"' if label else ' aria-hidden="true"'

    def opening(m: re.Match) -> str:
        attrs = re.sub(r'\s(?:width|height|class)="[^"]*"', "", m.group(1))
        return f'<svg class="oppo oppo-{Markup.escape(pose)}" width="{size}" height="{size}" focusable="false"{a11y}{attrs}>'

    return Markup(re.sub(r"<svg\b([^>]*)>", opening, src, count=1))
