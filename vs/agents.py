"""The four agents of a refresh. They work at the same time and hand work on
through queues, so every opportunity reaches the feed as soon as it has its
score instead of at the end of the refresh:

    Explorer -> Downloader -> Reader -> Scorer

Explorer lists every source and watched page; Downloader fetches the pages
(one worker per site, polite delays per site); Reader reads each page with
the chat model; Scorer scores it against you (portfolio, taste, model).

Only the Reader and the Scorer's explanations use the chat model, which stays
on the GPU; comparing texts (embeddings) runs on the CPU at the same time.

Stopping an agent also stops the ones before it (their work would only pile
up); what is already queued further on is finished. The Scorer can be stopped
alone: what it skips is scored when the refresh ends."""

from __future__ import annotations

import queue
import threading
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from vs import db, llm
from vs.fetch import RawItem
from vs.fetch.adapters import ADAPTERS
from vs.fetch.polite import Blocked, PoliteClient
from vs.fetch.watch import check as watch_check

END = object()


class Run:
    """What the agents of one refresh share: settings, counts, and which
    sources are completely read (only those are skipped next time)."""

    def __init__(self, *, client: PoliteClient, portfolio, model: str, prefs: dict, force: bool, rerank: bool,
                 say, jobs: list[tuple[str, dict]]) -> None:
        self.client, self.portfolio, self.model, self.p = client, portfolio, model, prefs
        self.force, self.rerank, self.say, self.jobs = force, rerank, say, jobs
        fetch = prefs.get("fetch", {})
        self.budget = int(fetch.get("max_pages_per_refresh", 150))
        self.keep = int(fetch.get("detail_pages_per_source", 40))
        self.parallel = int(fetch.get("parallel_downloads", 4))
        self.min_interval = timedelta(hours=float(fetch.get("source_min_interval_h", 12)))
        self.counts = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "too_short": 0, "duplicate": 0,
                       "failed": 0, "expired": 0}
        self.sources: dict[str, dict] = {}
        self.sent = 0  # pages handed to the Downloader (the budget)
        self.lock = threading.Lock()

    def listed(self, sid: str, items: int, history: list[int], pending: int, trimmed: bool) -> None:
        with self.lock:
            rep = self.sources[sid]
            rep.update(items=items, history=history, pending=pending, trimmed=trimmed)
        if not pending:
            self._maybe_complete(sid)

    def finish(self, sid: str, res: str) -> None:
        """One page of `sid` is through (read, skipped, failed...)."""
        with self.lock:
            self.counts[res] = self.counts.get(res, 0) + 1
            rep = self.sources.get(sid)
            if rep is None:
                return
            rep["processed"][res] = rep["processed"].get(res, 0) + 1
            rep["pending"] -= 1
            last = rep["pending"] == 0
        if last:
            self._maybe_complete(sid)

    def _maybe_complete(self, sid: str) -> None:
        # Only now is the source done: an interrupted, stopped or budget-trimmed
        # run must not make the next refresh skip it.
        rep = self.sources[sid]
        if rep.get("trimmed") or rep.get("error"):
            return
        with db.session() as conn:
            conn.execute(
                "INSERT INTO source_state(source_id, last_fetch, last_ok, items_last, items_history, error, blocked_runs) "
                "VALUES(?,?,?,?,?,NULL,0) ON CONFLICT(source_id) DO UPDATE SET last_fetch=excluded.last_fetch, "
                "last_ok=excluded.last_ok, items_last=excluded.items_last, items_history=excluded.items_history, "
                "error=NULL, blocked_runs=0",
                (sid, db.now(), db.now(), rep["items"], db.dumps(rep.get("history", []))))


class Agent:
    name = ""
    label = ""
    does = ""

    def __init__(self, run: Run) -> None:
        self.run = run
        self.inbox: queue.Queue = queue.Queue()
        self.next: Agent | None = None
        self.before: list[Agent] = []  # the agents feeding this one, nearest first
        self.status = "waiting"
        self.current = ""
        self.done = 0
        self.failed = False
        self._stop = threading.Event()
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._main, name=f"agent-{self.name}", daemon=True)

    # -- control

    def start(self) -> None:
        self.thread.start()

    def put(self, x) -> None:
        if not self._stop.is_set():
            self.inbox.put(x)

    def stop(self) -> None:
        if not self.finished.is_set():
            self._stop.set()
            self.inbox.put(END)  # wake it if it is waiting

    def stopped(self) -> bool:
        return self._stop.is_set()

    def queued(self) -> int:
        return self.inbox.qsize()

    def snapshot(self) -> dict:
        return {"name": self.name, "label": self.label, "does": self.does, "status": self.status,
                "current": self.current, "done": self.done, "queued": self.queued()}

    # -- work

    def setup(self) -> None:
        pass

    def handle(self, x) -> None:
        raise NotImplementedError

    def wrap_up(self) -> None:
        pass

    def work(self) -> None:
        while True:
            x = self.inbox.get()
            if x is END or self.stopped():
                return
            self.status = "working"
            self.handle(x)
            if self.inbox.empty():
                self.status, self.current = "idle", ""

    def _main(self) -> None:
        self.conn = db.connect()
        try:
            self.setup()
            self.status = "idle"
            self.work()
            self.wrap_up()
        except Exception as e:
            self.failed = True
            self.run.say(f"{self.label} stopped by an error: {type(e).__name__}: {e}")
            traceback.print_exc()
            for a in self.before:  # nothing would take their work any more
                a.stop()
        finally:
            self.conn.close()
            self.status = "failed" if self.failed else "stopped" if self.stopped() else "done"
            self.current = ""
            self.finished.set()
            if self.next:
                self.next.inbox.put(END)


class Explorer(Agent):
    name, label, does = "explorer", "Explorer", "lists every source and watched page"

    def work(self) -> None:
        from vs import rules as rules_mod
        from vs.pipeline import prefilter

        run, conn = self.run, self.conn
        active = rules_mod.compile_rules(run.p)
        for sid, cfg in run.jobs:
            if self.stopped():
                return
            st = conn.execute("SELECT * FROM source_state WHERE source_id=?", (sid,)).fetchone()
            if st and st["last_ok"] and not run.force:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(st["last_ok"])
                if age < run.min_interval:
                    run.say(f"{sid}: fetched {int(age.total_seconds() // 3600)} h ago, skipped (use force)")
                    continue
            if st and (st["blocked_runs"] or 0) >= 2 and cfg.get("adapter") == "jobspy" and not run.force:
                run.say(f"{sid}: auto-disabled after 2 blocked runs")
                continue
            self.status, self.current = "working", sid
            run.say(f"{sid}: listing")
            rep = run.sources[sid] = {"items": 0, "processed": {}, "error": None, "pending": 0}
            try:
                if cfg["adapter"] == "watch":
                    rep["changed"], items = watch_check(run.client, cfg)
                else:
                    items = list(ADAPTERS[cfg["adapter"]](run.client, sid, cfg))
                listed = len(items)
                kept = [it for it in items if not rules_mod.reason(it.title, "", sid, active)]
                if len(kept) < len(items):
                    run.say(f"{sid}: {len(items) - len(kept)} skipped by your rules (title not in your fields / never-show)")
                items = kept
                today = datetime.now().date().isoformat()
                closed = [it for it in items if ((it.hints or {}).get("deadline") or "9999")[:10] < today]
                if closed:
                    run.say(f"{sid}: {len(closed)} skipped, deadline already passed")
                    items = [it for it in items if it not in closed]
                # Every source is ranked cheaply (keywords + embeddings) before
                # the slow model reads anything; "prefilter": false opts out.
                if cfg.get("prefilter", True) is not False:
                    before = len(items)
                    items = prefilter(conn, run.portfolio, items, int(cfg.get("keep", run.keep)), run.model)
                    run.say(f"{sid}: {before} listed, {len(items)} new and closest to your work kept")
                conn.commit()
                trimmed = False
                if run.budget > 0 and run.sent + len(items) > run.budget:
                    items, trimmed = items[:max(0, run.budget - run.sent)], True
                    run.say(f"{sid}: {len(items)} read this time (max_pages_per_refresh); the rest at the next refresh")
                run.sent += len(items)
                history = ((db.row_dict(st)["items_history"] or []) if st else [])[-9:] + [listed]
                run.listed(sid, listed, history, len(items), trimmed)
                for it in items:
                    self.next.put(it)
            except Exception as e:
                blocked = isinstance(e, Blocked) or "429" in str(e)
                rep["error"] = f"{type(e).__name__}: {e}"
                run.say(f"{sid}: ERROR {rep['error'][:200]}")
                conn.execute(
                    "INSERT INTO source_state(source_id, last_fetch, error, blocked_runs) VALUES(?,?,?,?) "
                    "ON CONFLICT(source_id) DO UPDATE SET last_fetch=excluded.last_fetch, error=excluded.error, "
                    "blocked_runs=source_state.blocked_runs + ?",
                    (sid, db.now(), rep["error"][:500], int(blocked), int(blocked)))
                conn.commit()
            self.done += 1
        self.current = ""


class Downloader(Agent):
    """One worker per site, each fetching its site's pages in the order they
    were listed: several workers on one site would only queue on its polite
    delay, and the page needed next could wait behind them."""
    name, label, does = "downloader", "Downloader", "fetches the pages, one worker per site"

    def __init__(self, run: Run) -> None:
        super().__init__(run)
        self.pool = ThreadPoolExecutor(max_workers=max(1, run.parallel), thread_name_prefix="download")
        self.sites: dict[str, deque] = {}
        self.busy: set[str] = set()
        self.site_lock = threading.Lock()

    def queued(self) -> int:
        with self.site_lock:
            waiting = sum(len(q) for q in self.sites.values())
        return self.inbox.qsize() + waiting

    def snapshot(self) -> dict:
        out = super().snapshot()
        with self.site_lock:
            busy = sorted(self.busy)
        if busy and out["status"] == "idle":
            out["status"] = "working"
        if busy:
            out["current"] = ", ".join(busy[:3]) + (f" and {len(busy) - 3} more" if len(busy) > 3 else "")
        return out

    def handle(self, item: RawItem) -> None:
        host = urlparse(item.url).netloc
        with self.site_lock:
            self.sites.setdefault(host, deque()).append(item)
            if host in self.busy:
                return
            self.busy.add(host)
        self.pool.submit(self._site, host)

    def _site(self, host: str) -> None:
        from vs.pipeline import prepare_item

        conn = db.connect()
        try:
            while not self.stopped():
                with self.site_lock:
                    q = self.sites.get(host)
                    if not q:
                        self.busy.discard(host)
                        return
                    item = q.popleft()
                try:
                    res = prepare_item(self.run.client, conn, item)
                except Exception:
                    res = "failed"
                conn.commit()
                self.done += 1
                if isinstance(res, str):
                    self.run.finish(item.source_id, res)
                else:
                    self.next.put(res)
        finally:
            with self.site_lock:
                self.busy.discard(host)
            conn.close()

    def wrap_up(self) -> None:
        # Every page handed over must be fetched before the Reader hears "end".
        self.pool.shutdown(wait=True, cancel_futures=self.stopped())
        self.current = ""


class Reader(Agent):
    name, label, does = "reader", "Reader", "reads each page with the model"

    def handle(self, prepared) -> None:
        from vs.pipeline import read_item

        item = prepared.item
        self.current = item.title[:80]
        try:
            res, opp_id = read_item(self.conn, prepared, self.run.model)
        except llm.LLMError as e:
            self.run.say(f"  LLM failed on {item.url}: {e}")
            res, opp_id = "failed", None
        self.conn.commit()
        self.run.finish(item.source_id, res)
        if res != "unchanged":
            label = {"skipped": "not a call", "too_short": "too little text on the page",
                     "failed": "could not read"}.get(res, res)
            self.run.say(f"  [{self.done + 1}] {label}: {item.title[:70]}")
        self.done += 1
        if opp_id and res in ("new", "updated"):
            self.next.put(opp_id)


class Scorer(Agent):
    name, label, does = "scorer", "Scorer", "scores each one against you, as soon as it is read"

    def setup(self) -> None:
        from vs import score
        self.ctx = score.context(self.conn, self.run.portfolio)

    def handle(self, opp_id: int) -> None:
        from vs import score

        row = self.conn.execute("SELECT title_en FROM opportunity WHERE id=?", (opp_id,)).fetchone()
        self.current = ((row and row["title_en"]) or "")[:80]
        final = score.score_one(self.conn, self.ctx, opp_id, rerank=self.run.rerank)
        self.done += 1
        if final is not None:
            self.run.say(f"  scored {round(final * 100)}: {self.current[:70]}")


def build(run: Run) -> list[Agent]:
    agents = [Explorer(run), Downloader(run), Reader(run), Scorer(run)]
    for i, a in enumerate(agents):
        a.next = agents[i + 1] if i + 1 < len(agents) else None
        a.before = list(reversed(agents[:i]))
    return agents


def stop(agents: list[Agent], name: str) -> bool:
    """Stop one agent and the ones before it; the Scorer stops alone."""
    target = next((a for a in agents if a.name == name), None)
    if target is None or target.finished.is_set():
        return False
    target.stop()
    if name != "scorer":
        for a in target.before:
            a.stop()
    return True
