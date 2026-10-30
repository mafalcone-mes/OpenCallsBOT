"""The shared sources sheet: parsing, fallbacks, lossless round trip."""

import csv
import io
from pathlib import Path

from vs import sheet

SNAPSHOT = Path(__file__).resolve().parent.parent / "sources-snapshot.csv"


def _apply(rows, monkeypatch):
    monkeypatch.setattr(sheet, "rows", lambda cfg: (rows, None))
    monkeypatch.delenv("VS_NO_SHEET", raising=False)
    return sheet.apply_to_prefs({"sheet": {"enabled": True, "url": "x"}, "sources": {}, "watchlist": []})


def test_snapshot_gives_every_kind(monkeypatch):
    data = _apply(sheet._parse(SNAPSHOT.read_text(encoding="utf-8-sig")), monkeypatch)
    assert data["_from_sheet"]
    assert len(data["sources"]) > 20 and len(data["watchlist"]) > 20
    assert data["_studios"] and data["_excluded"]
    assert all(cfg.get("adapter") for cfg in data["sources"].values())


def test_round_trip_is_lossless(monkeypatch):
    first = _apply(sheet._parse(SNAPSHOT.read_text(encoding="utf-8-sig")), monkeypatch)
    # write those sources back out as sheet rows, read them again: same configuration
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=sheet.COLUMNS)
    w.writeheader()
    for sid, cfg in first["sources"].items():
        extra = {k: v for k, v in cfg.items() if k not in sheet.COMMON}
        import json
        w.writerow({"kind": "source", "id": sid, "name": sid, "enabled": "TRUE" if cfg.get("enabled", True) else "FALSE",
                    "url": cfg.get("url", ""), "how_read": cfg["adapter"], "settings": json.dumps(extra) if extra else "",
                    "weight": cfg.get("weight", ""), "opportunity_type": cfg.get("kind_hint", "")})
    second = _apply(sheet._parse(buf.getvalue()), monkeypatch)
    assert second["sources"] == first["sources"]


def test_decimal_comma_and_bad_json_do_not_break(monkeypatch):
    rows = [{"kind": "source", "id": "x", "name": "x", "url": "https://example.org", "how_read": "rss",
             "settings": "{not json", "weight": "0,9"}]
    data = _apply(rows, monkeypatch)
    assert data["sources"]["x"]["weight"] == 0.9
    assert "not valid JSON" in data["_sheet_error"]


def test_a_local_csv_replaces_the_sheet_without_touching_it(monkeypatch, tmp_path):
    """VS_SOURCES_CSV: try another list of sources; the Google Sheet is not read."""
    local = tmp_path / "trial.csv"
    local.write_text("kind,id,name,enabled,url,how_read\nsource,trial,trial,TRUE,https://example.org/feed,rss\n", encoding="utf-8")
    monkeypatch.setenv("VS_SOURCES_CSV", str(local))
    monkeypatch.setenv("VS_NO_SHEET", "1")  # a local list still applies when the sheet is switched off
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the sheet was fetched")))
    data = sheet.apply_to_prefs({"sheet": {"enabled": False}, "sources": {"old": {}}, "watchlist": []})
    assert list(data["sources"]) == ["trial"]
