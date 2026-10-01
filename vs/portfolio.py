"""The ONLY module that reads the portfolio website folder (read-only).

Mirrors the website's own semantics (lib/projects.ts, lib/content.ts):
  content/projects.csv              one row per (slug, category); first row =
                                    primary category; '#' lines are comments
  content/categories.ts             category ids, labels, colours (data-driven)
  lib/site.ts                       name, role, bio, contacts
  public/projects/<slug>/<cat>.md   case-study text per angle (optional)
  public/projects/<slug>/<cat>/     gallery images + optional captions.json
  public/cv.pdf                     CV (optional)
  app/globals.css                   colour tokens reused by the dashboard

If the website's format changes, this is the one file to fix.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from vs.config import portfolio_dir

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
META_COLUMNS = ("context", "role", "collaborators", "medium", "location")


class PortfolioError(RuntimeError):
    pass


@dataclass
class Category:
    id: str
    label: str
    color: str


@dataclass
class Angle:
    """A project seen through one of its categories: the unit we match on."""

    slug: str
    category: str
    project_name: str
    subtitle: str
    body: str
    image_count: int
    text_hash: str

    @property
    def key(self) -> str:
        return f"{self.slug}:{self.category}"

    def embed_text(self, category_label: str) -> str:
        return f"{self.project_name} ({category_label}). {self.subtitle}\n\n{self.body}".strip()


@dataclass
class Project:
    slug: str
    name: str
    year: int | None
    categories: list[str]
    meta: dict[str, str] = field(default_factory=dict)
    has_cover: bool = False


@dataclass
class Portfolio:
    root: Path
    person: dict
    categories: list[Category]
    projects: list[Project]
    angles: list[Angle]
    has_cv: bool
    cv_text: str
    css_tokens: dict[str, str]
    profile_hash: str
    media_hash: str
    warnings: list[str]

    def category(self, cid: str) -> Category:
        for c in self.categories:
            if c.id == cid:
                return c
        # Unknown ids are still data: show them with a neutral colour.
        return Category(cid, cid.replace("-", " ").title(), "#8c8b8a")

    def angle(self, key: str) -> Angle | None:
        return next((a for a in self.angles if a.key == key), None)

    def project(self, slug: str) -> Project | None:
        return next((p for p in self.projects if p.slug == slug), None)

    def profile_text(self) -> str:
        """Plain-text profile for LLM prompts, built from live content."""
        p = self.person
        lines = [f"{p.get('name', '')}: {p.get('role', '')}.", " ".join(p.get("bio") or [])]
        for proj in self.projects:
            labels = ", ".join(self.category(c).label for c in proj.categories)
            angle = next((a for a in self.angles if a.slug == proj.slug and a.body), None)
            summary = angle.body[:600] if angle else ""
            meta = "; ".join(f"{k}: {v}" for k, v in proj.meta.items())
            lines.append(f"- {proj.name} [{labels}] {meta}\n  {angle.subtitle if angle else ''}: {summary}")
        return "\n".join(lines)


# ---------------------------------------------------------------- readers


def _read_csv(path: Path) -> list[dict[str, str]]:
    raw = path.read_text(encoding="utf-8")
    kept = "\n".join(line for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(io.StringIO(kept))]


def _eval_ts(path: Path) -> dict:
    """Import a .ts module with Node's type stripping and return its exports."""
    url = path.resolve().as_uri()
    script = f"import({json.dumps(url)}).then(m => process.stdout.write(JSON.stringify(m)))"
    try:
        out = subprocess.run(
            ["node", "--experimental-strip-types", "--no-warnings", "-e", script],
            capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=path.parent,
        )
    except FileNotFoundError as e:
        raise PortfolioError("Node.js not found; it is needed to read .ts files from the website") from e
    if out.returncode != 0:
        raise PortfolioError(f"Could not evaluate {path}: {out.stderr.strip()[:400]}")
    return json.loads(out.stdout)


def _categories_regex_fallback(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    found = []
    for m in re.finditer(r"\{[^{}]*\bid:\s*\"([^\"]+)\"[^{}]*\blabel:\s*\"([^\"]+)\"[^{}]*\bcolor:\s*\"([^\"]+)\"", text):
        found.append({"id": m.group(1), "label": m.group(2), "color": m.group(3)})
    if not found:
        raise PortfolioError(f"Could not parse categories from {path}")
    return found


def _css_tokens(path: Path) -> dict[str, str]:
    try:
        css = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    root = re.search(r":root\s*\{([^}]*)\}", css)
    if not root:
        return {}
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"--([\w-]+)\s*:\s*([^;]+);", root.group(1))}


def _hash_files(root: Path, files: list[Path]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda p: p.as_posix()):
        h.update(f.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [f for f in folder.iterdir() if f.suffix.lower() in IMAGE_EXTENSIONS]


# ---------------------------------------------------------------- loader


def source_files(root: Path) -> list[Path]:
    """Text sources whose change must invalidate everything derived."""
    files = [root / "content" / "projects.csv", root / "content" / "categories.ts", root / "lib" / "site.ts"]
    projects_dir = root / "public" / "projects"
    if projects_dir.is_dir():
        files += list(projects_dir.rglob("*.md")) + list(projects_dir.rglob("captions.json"))
    cv = root / "public" / "cv.pdf"
    if cv.exists():
        files.append(cv)
    return [f for f in files if f.exists()]


def portfolio_format() -> str:
    """website (the Next.js site format), folder (profile.md + projects/*.md)
    or pdf (an existing portfolio / CV PDF). Set in [portfolio].format."""
    from vs.config import prefs
    return (prefs().get("portfolio", {}).get("format") or "website").lower()


def current_hash(root: Path | None = None, fmt: str | None = None) -> str:
    root = root or portfolio_dir()
    fmt = fmt or portfolio_format()
    if fmt == "folder":
        return _hash_files(root, _folder_files(root))
    if fmt == "pdf":
        return _hash_files(root, _pdf_files(root))
    return _hash_files(root, source_files(root))


def load(root: Path | None = None, fmt: str | None = None) -> Portfolio:
    root = root or portfolio_dir()
    fmt = fmt or portfolio_format()
    if fmt == "folder":
        if not (root / "profile.md").exists():
            raise PortfolioError(f"No profile.md in {root} (portfolio format 'folder', see portfolio-template/)")
        return _load_folder(str(root), current_hash(root, fmt))
    if fmt == "pdf":
        if not _pdf_files(root):
            raise PortfolioError(f"No PDF found in {root} (portfolio format 'pdf': put portfolio.pdf and/or cv.pdf there)")
        return _load_pdf(str(root), current_hash(root, fmt))
    if not (root / "content" / "projects.csv").exists():
        raise PortfolioError(f"No content/projects.csv in {root}; set PORTFOLIO_DIR or [portfolio].dir "
                             "(or [portfolio].format = 'folder' / 'pdf')")
    return _load_cached(str(root), current_hash(root, fmt))


@lru_cache(maxsize=4)
def _load_cached(root_str: str, profile_hash: str) -> Portfolio:
    root = Path(root_str)
    warnings: list[str] = []

    try:
        cats_raw = _eval_ts(root / "content" / "categories.ts")["categories"]
    except (PortfolioError, KeyError) as e:
        warnings.append(f"categories.ts read with regex fallback ({e})")
        cats_raw = _categories_regex_fallback(root / "content" / "categories.ts")
    categories = [Category(c["id"], c.get("label", c["id"]), c.get("color", "#8c8b8a")) for c in cats_raw]

    person = _eval_ts(root / "lib" / "site.ts").get("site", {})

    projects: dict[str, Project] = {}
    rows = _read_csv(root / "content" / "projects.csv")
    for row in rows:
        slug = row.get("slug", "")
        if not slug:
            continue
        proj = projects.get(slug)
        if proj is None:
            year = int(row["year"]) if row.get("year", "").isdigit() and int(row["year"]) > 0 else None
            proj = projects[slug] = Project(slug, row.get("name") or slug, year, [])
            proj.has_cover = (root / "public" / "projects" / f"{slug}.jpg").exists()
        cat = row.get("category", "")
        if cat and cat not in proj.categories:
            proj.categories.append(cat)
        for col in META_COLUMNS:
            if row.get(col) and col not in proj.meta:
                proj.meta[col] = row[col]

    known = {c.id for c in categories}
    angles: list[Angle] = []
    for proj in projects.values():
        for cat in proj.categories:
            if cat not in known:
                warnings.append(f"{proj.slug}: category '{cat}' is not in categories.ts")
            md = root / "public" / "projects" / proj.slug / f"{cat}.md"
            text = md.read_text(encoding="utf-8").strip() if md.exists() else ""
            subtitle, _, body = text.partition("\n")
            images = _images(root / "public" / "projects" / proj.slug / cat)
            angles.append(Angle(
                slug=proj.slug, category=cat, project_name=proj.name,
                subtitle=subtitle.strip(), body=body.strip(), image_count=len(images),
                text_hash=hashlib.sha256(f"{proj.name}|{cat}|{text}".encode()).hexdigest(),
            ))
            if not text:
                warnings.append(f"{proj.slug}/{cat}: no write-up (.md), so matching uses only the name")
            if not images:
                warnings.append(f"{proj.slug}/{cat}: no gallery images")

    # Identical texts across a project's categories weaken the per-angle matching.
    for proj in projects.values():
        bodies = {a.body for a in angles if a.slug == proj.slug}
        if len(proj.categories) > 1 and len(bodies) == 1:
            warnings.append(f"{proj.slug}: the same write-up is used for all its categories")

    if projects and all(p.year is None for p in projects.values()):
        warnings.append("Every project year is 0000 (unknown)")
    contacts = person.get("contacts") or {}
    if "example.com" in str(contacts.get("email", "")):
        warnings.append("site.ts contacts are placeholders; they appear on the PDF about page")

    cv = root / "public" / "cv.pdf"
    cv_text = ""
    if cv.exists():
        try:
            from pypdf import PdfReader
            cv_text = "\n".join(page.extract_text() or "" for page in PdfReader(cv).pages)
        except Exception as e:  # a broken PDF shouldn't stop the app
            warnings.append(f"cv.pdf unreadable: {e}")
    else:
        warnings.append("No public/cv.pdf: briefs will list the CV as missing")

    media = hashlib.sha256()
    projects_dir = root / "public" / "projects"
    if projects_dir.is_dir():
        for f in sorted(projects_dir.rglob("*")):
            if f.suffix.lower() in IMAGE_EXTENSIONS:
                media.update(f"{f.relative_to(root).as_posix()}:{f.stat().st_size}".encode())

    return Portfolio(
        root=root, person=person, categories=categories, projects=list(projects.values()),
        angles=angles, has_cv=cv.exists(), cv_text=cv_text,
        css_tokens=_css_tokens(root / "app" / "globals.css"),
        profile_hash=profile_hash, media_hash=media.hexdigest(), warnings=warnings,
    )


# ---------------------------------------------------------------- folder format
#
#   <dir>/profile.md            frontmatter: name, role, short_bio, email, website,
#                               instagram, linkedin; body = full bio (paragraphs)
#   <dir>/projects/<slug>.md    frontmatter: title, year, categories (list),
#                               context, role, collaborators, medium, location;
#                               first line of the body = subtitle, rest = text
#   <dir>/projects/<slug>/      optional images (counted for briefs)
#   <dir>/cv.pdf                optional
# Categories are whatever the projects use (no fixed list); colours are assigned.

PALETTE = ["#f4c95d", "#e8734a", "#a78bfa", "#5cc8c2", "#7fb069", "#e56399", "#6c9bd2", "#d4a373"]


def _front(path: Path) -> tuple[dict, str]:
    import yaml
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"---\s*\n(.*?)\n---\s*\n?(.*)", raw, re.S)
    if not m:
        return {}, raw.strip()
    return (yaml.safe_load(m.group(1)) or {}), m.group(2).strip()


def _folder_files(root: Path) -> list[Path]:
    files = [root / "profile.md", root / "cv.pdf"]
    if (root / "projects").is_dir():
        files += sorted((root / "projects").glob("*.md"))
    return [f for f in files if f.exists()]


@lru_cache(maxsize=4)
def _load_folder(root_str: str, profile_hash: str) -> Portfolio:
    root = Path(root_str)
    warnings: list[str] = []
    meta, bio = _front(root / "profile.md")
    person = {
        "name": meta.get("name", ""), "role": meta.get("role", ""),
        "description": meta.get("short_bio", ""), "shortBio": meta.get("short_bio", ""),
        "bio": [p.strip() for p in bio.split("\n\n") if p.strip()],
        "contacts": {k: meta.get(k, "") for k in ("email", "website", "instagram", "linkedin")},
    }
    projects, angles, cat_ids = [], [], []
    project_files = sorted((root / "projects").glob("*.md")) if (root / "projects").is_dir() else []
    for md in project_files:
        fm, body = _front(md)
        slug = md.stem
        cats = [str(c).strip().lower().replace(" ", "-") for c in (fm.get("categories") or ["work"])]
        year = int(fm["year"]) if str(fm.get("year", "")).isdigit() else None
        proj = Project(slug, fm.get("title") or slug, year, cats,
                       {k: str(fm[k]) for k in META_COLUMNS if fm.get(k)})
        projects.append(proj)
        subtitle, _, text = body.partition("\n")
        images = _images(root / "projects" / slug)
        for cat in cats:
            if cat not in cat_ids:
                cat_ids.append(cat)
            angles.append(Angle(slug=slug, category=cat, project_name=proj.name, subtitle=subtitle.strip(),
                                body=text.strip(), image_count=len(images),
                                text_hash=hashlib.sha256(f"{proj.name}|{cat}|{body}".encode()).hexdigest()))
        if not body:
            warnings.append(f"{slug}: no project text")
    if not projects:
        warnings.append("No projects in projects/: matching will rely on the profile only")
    categories = [Category(c, c.replace("-", " ").title(), PALETTE[i % len(PALETTE)]) for i, c in enumerate(cat_ids)]
    cv = root / "cv.pdf"
    cv_text = _pdf_text(cv) if cv.exists() else ""
    if not cv.exists():
        warnings.append("No cv.pdf in the portfolio folder: briefs will list the CV as missing")
    return Portfolio(root=root, person=person, categories=categories, projects=projects, angles=angles,
                     has_cv=cv.exists(), cv_text=cv_text, css_tokens={}, profile_hash=profile_hash,
                     media_hash=profile_hash, warnings=warnings)


# ---------------------------------------------------------------- pdf format
#
#   <dir>/portfolio.pdf (and/or any other .pdf), <dir>/cv.pdf
# Each portfolio page with enough text becomes one "angle" (title = its first
# line). Less precise than structured projects: the folder format is better.

def _pdf_files(root: Path) -> list[Path]:
    return sorted(root.glob("*.pdf")) if root.is_dir() else []


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception:
        return ""


@lru_cache(maxsize=4)
def _load_pdf(root_str: str, profile_hash: str) -> Portfolio:
    from pypdf import PdfReader

    root = Path(root_str)
    warnings = ["Portfolio read from PDF: one entry per page. A folder with one file per project matches better."]
    cv = root / "cv.pdf"
    cv_text = _pdf_text(cv) if cv.exists() else ""
    angles, projects = [], []
    for pdf in [f for f in _pdf_files(root) if f.name.lower() != "cv.pdf"]:
        try:
            pages = PdfReader(pdf).pages
        except Exception as e:
            warnings.append(f"{pdf.name}: unreadable ({e})")
            continue
        for i, page in enumerate(pages, 1):
            text = (page.extract_text() or "").strip()
            if len(text.split()) < 40:
                continue  # image-only or title pages
            first, _, rest = text.partition("\n")
            slug = f"{pdf.stem}-p{i}"
            name = first.strip()[:80] or slug
            projects.append(Project(slug, name, None, ["portfolio"]))
            angles.append(Angle(slug=slug, category="portfolio", project_name=name, subtitle="",
                                body=rest.strip(), image_count=0, text_hash=hashlib.sha256(text.encode()).hexdigest()))
    if not angles:
        warnings.append("No readable text in the portfolio PDF (scanned or image-only?)")
    first_line = (cv_text.strip().splitlines() or [""])[0][:80]
    person = {"name": first_line, "role": "", "description": "", "bio": [cv_text[:1500]] if cv_text else [], "contacts": {}}
    return Portfolio(root=root, person=person, categories=[Category("portfolio", "Portfolio", "#8c8b8a")],
                     projects=projects, angles=angles, has_cv=cv.exists(), cv_text=cv_text, css_tokens={},
                     profile_hash=profile_hash, media_hash=profile_hash, warnings=warnings)
