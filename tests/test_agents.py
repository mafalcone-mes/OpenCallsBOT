"""The four agents of a refresh work at the same time, and each opportunity
is scored as soon as it is read."""

import hashlib
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from vs import agents, db, extract, llm, pipeline, score
from vs import portfolio as portfolio_mod
from vs.fetch import RawItem

TEMPLATE = Path(__file__).resolve().parent.parent / "portfolio-template"
REAL_LOAD = portfolio_mod.load


def _run(**kw):
    defaults = dict(client=None, portfolio=None, model="m", prefs={"fetch": {"parallel_downloads": 4}}, force=False,
                    rerank=False, say=lambda msg: None, jobs=[])
    return agents.Run(**(defaults | kw))


def _vec(text: str) -> np.ndarray:
    v = np.frombuffer(hashlib.sha256(text.encode()).digest()[:16], dtype=np.uint8).astype(np.float32) - 127
    return v / np.linalg.norm(v)


def test_each_opportunity_is_scored_while_the_next_ones_are_still_being_read(monkeypatch, tmp_path):
    events, lock = [], threading.Lock()

    def log(e):
        with lock:
            events.append(e)

    def fake_source(client, sid, cfg):
        for i in range(6):
            yield RawItem(url=f"https://site.example/call-{i}", title=f"Call {i}", source_id=sid,
                          text=f"Open call number {i} for designers and artists. " * 5, needs_detail=False)

    def fake_extract(text, *, title="", url="", lang="", today=None):
        time.sleep(0.05)  # the model takes a while
        log(("read", title))
        return {"is_opportunity": True, "type": "open_call", "title_en": title, "org_name": f"Org {title}",
                "summary_en": text[:80], "deadline": "2099-01-01"}

    real_score_one = score.score_one

    def logged_score_one(conn, ctx, opp_id, **kw):
        out = real_score_one(conn, ctx, opp_id, **kw)
        log(("scored", opp_id))
        return out

    prefs = {"sources": {"fake-src": {"adapter": "fake", "prefilter": False}}, "fetch": {"max_pages_per_refresh": 0},
             "weights": {}, "taste": {}}
    root = tmp_path / "pf"
    shutil.copytree(TEMPLATE, root)
    monkeypatch.setitem(agents.ADAPTERS, "fake", fake_source)
    monkeypatch.setattr(pipeline, "prefs", lambda: prefs)
    monkeypatch.setattr(score, "prefs", lambda: prefs)
    monkeypatch.setattr(portfolio_mod, "load", lambda *a, **k: REAL_LOAD(root, fmt="folder"))
    monkeypatch.setattr(llm, "embed_model", lambda: "emb")
    monkeypatch.setattr(llm, "chat_model", lambda: "chat")
    monkeypatch.setattr(llm, "extract_model", lambda: "chat")
    monkeypatch.setattr(llm, "unload", lambda m: None)
    monkeypatch.setattr(llm, "embed", lambda texts, model=None: [_vec(t) for t in texts])
    monkeypatch.setattr(extract, "extract", fake_extract)
    monkeypatch.setattr(extract, "verify", lambda data, text, lang="": {})
    monkeypatch.setattr(score, "score_one", logged_score_one)
    import vs.report
    monkeypatch.setattr(vs.report, "write_run_report", lambda *a, **k: tmp_path / "report.md")
    with db.session() as conn:
        for t in ("match", "opportunity", "run", "source_state"):
            conn.execute(f"DELETE FROM {t}")

    counts = pipeline.refresh(force=True, rerank=False)

    assert counts["new"] == 6
    reads = [i for i, e in enumerate(events) if e[0] == "read"]
    scores = [i for i, e in enumerate(events) if e[0] == "scored"]
    assert len(scores) == 6
    assert scores[0] < reads[-1], "the first one is scored before the last one is read"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM match WHERE final IS NOT NULL").fetchone()[0] == 6
        assert conn.execute("SELECT last_ok FROM source_state WHERE source_id='fake-src'").fetchone()[0]
    assert [a["status"] for a in pipeline.progress_state()["agents"]] == ["done"] * 4
    assert llm.embed_options == {}, "embeddings go back to the GPU after the refresh"


def test_stopping_an_agent_stops_the_ones_before_it_but_the_scorer_stops_alone():
    team = agents.build(_run())
    assert agents.stop(team, "reader")
    assert [a.name for a in team if a.stopped()] == ["explorer", "downloader", "reader"]

    team = agents.build(_run())
    assert agents.stop(team, "scorer")
    assert [a.name for a in team if a.stopped()] == ["scorer"]
    assert not agents.stop(team, "nobody")


def test_downloader_uses_one_worker_per_site_in_listing_order(monkeypatch):
    seen, active, overlap, lock = [], {}, [], threading.Lock()

    def fake_prepare(client, conn, item):
        host = item.url.split("/")[2]
        with lock:
            active[host] = active.get(host, 0) + 1
            overlap.append(active[host])
            seen.append(item.url)
        time.sleep(0.01)
        with lock:
            active[host] -= 1
        if item.url.endswith("/1"):
            raise RuntimeError("site down")  # one failed page doesn't stop the rest
        return "unchanged"

    monkeypatch.setattr(pipeline, "prepare_item", fake_prepare)
    run = _run()
    team = agents.build(run)
    down = team[1]
    down.next = None  # nothing here goes on to the Reader
    down.start()
    items = [RawItem(url=f"https://{h}.example/{i}", title=f"{h}{i}", source_id="s") for i in range(4) for h in "ab"]
    for it in items:
        down.put(it)
    down.put(agents.END)
    assert down.finished.wait(10)
    assert max(overlap) == 1
    assert [u for u in seen if "a.example" in u] == [it.url for it in items if "a.example" in it.url]
    assert run.counts["failed"] == 2 and run.counts["unchanged"] == 6
    assert down.status == "done"
