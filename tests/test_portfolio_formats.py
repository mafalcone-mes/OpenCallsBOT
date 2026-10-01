"""The folder and PDF portfolio formats (for people without the website)."""

import shutil
from pathlib import Path

from vs import portfolio

TEMPLATE = Path(__file__).resolve().parent.parent / "portfolio-template"


def test_folder_template_loads(tmp_path):
    root = tmp_path / "pf"
    shutil.copytree(TEMPLATE, root)
    p = portfolio.load(root, fmt="folder")
    assert p.person["name"] == "Your Name"
    assert [a.key for a in p.angles] == ["example-project:graphic-design", "example-project:photography"]
    assert {c.id for c in p.categories} == {"graphic-design", "photography"}
    assert p.angles[0].subtitle.startswith("A one-line subtitle")


def test_folder_new_project_changes_hash(tmp_path):
    root = tmp_path / "pf"
    shutil.copytree(TEMPLATE, root)
    before = portfolio.current_hash(root, "folder")
    (root / "projects" / "new-one.md").write_text("---\ntitle: New\ncategories: [maps]\n---\nSub\n\nText.", encoding="utf-8")
    assert portfolio.current_hash(root, "folder") != before
    assert portfolio.load(root, fmt="folder").angle("new-one:maps")


def test_pdf_format_reads_pages(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import NameObject  # noqa: F401  (pypdf available)
    root = tmp_path / "pdf"
    root.mkdir()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    with open(root / "portfolio.pdf", "wb") as f:
        w.write(f)
    p = portfolio.load(root, fmt="pdf")
    assert p.categories[0].id == "portfolio"
    assert any("No readable text" in x for x in p.warnings), "a blank PDF must give a clear warning, not crash"
