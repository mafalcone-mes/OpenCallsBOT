"""The loader reads the website live: a new project or category must show up
with no code change, and any content change must change the hash."""

import shutil
from pathlib import Path

import pytest

import os

from vs import portfolio

# The Next.js website format is tested against a real site when one is present
# (WEBSITE_DIR, default ../website); otherwise these tests are skipped.
SITE = Path(os.environ.get("WEBSITE_DIR", Path(__file__).resolve().parent.parent.parent / "website")).resolve()
pytestmark = pytest.mark.skipif(not (SITE / "content" / "projects.csv").exists(),
                                reason="no website-format portfolio on this machine")


@pytest.fixture()
def site_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "website"
    for rel in ("content", "lib", "app/globals.css", "public/projects"):
        src = SITE / rel
        if src.is_dir():
            shutil.copytree(src, dst / rel, ignore=shutil.ignore_patterns("*.jpg", "*.png", "*.webp"))
        else:
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst / rel)
    return dst


def test_reads_real_site():
    p = portfolio.load(SITE, fmt='website')
    assert p.person["name"]
    assert p.categories and all(c.id and c.color for c in p.categories)
    assert p.projects and p.angles
    # every CSV row becomes an angle
    csv_rows = [l for l in (SITE / "content" / "projects.csv").read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.lstrip().startswith("#")][1:]
    assert len(p.angles) == len(csv_rows)


def test_new_category_and_project_need_no_code(site_copy: Path):
    before = portfolio.load(site_copy, fmt='website')
    cats = site_copy / "content" / "categories.ts"
    cats.write_text(cats.read_text(encoding="utf-8").replace(
        "];", '  { id: "data-journalism", label: "Data Journalism", color: "#123456", ringRadius: 44 },\n];'), encoding="utf-8")
    with open(site_copy / "content" / "projects.csv", "a", encoding="utf-8") as f:
        f.write('\ncounter-maps,Counter Maps,data-journalism,2025,"Workshop, Rotterdam",,,,Rotterdam\n')
    (site_copy / "public" / "projects" / "counter-maps").mkdir(parents=True)
    (site_copy / "public" / "projects" / "counter-maps" / "data-journalism.md").write_text(
        "Participatory Mapping\n\nA counter-mapping workshop with residents.", encoding="utf-8")

    after = portfolio.load(site_copy, fmt='website')
    assert after.profile_hash != before.profile_hash
    assert "data-journalism" in [c.id for c in after.categories]
    angle = after.angle("counter-maps:data-journalism")
    assert angle and angle.subtitle == "Participatory Mapping"
    assert after.project("counter-maps").meta["context"] == "Workshop, Rotterdam"


def test_hash_changes_on_writeup_edit(site_copy: Path):
    before = portfolio.current_hash(site_copy, 'website')
    md = next((site_copy / "public" / "projects").rglob("*.md"))
    md.write_text(md.read_text(encoding="utf-8") + "\nEdited.", encoding="utf-8")
    assert portfolio.current_hash(site_copy, 'website') != before
