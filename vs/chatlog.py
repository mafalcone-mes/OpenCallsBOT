"""Saved chats: each question with the app's answer as it was shown, so a chat
can be reopened from the side bar and continued."""

from __future__ import annotations

import re
from html import escape

from vs import db

SIDEBAR_MAX = 8
_CONFIRM = re.compile(r'<div class="chat-confirm">.*?</div>', re.S)
_CHECK = re.compile(r'<div class="chat-check"[^>]*>.*?</div>', re.S)
_REPLY = re.compile(r'<div class="chat-reply[^"]*"[^>]*>.*?</div>', re.S)


def title_of(question: str) -> str:
    q = " ".join(question.split())
    return q if len(q) <= 60 else q[:59].rstrip() + "…"


def create(conn, question: str) -> int:
    cur = conn.execute("INSERT INTO chat(title, started, updated, turns) VALUES(?,?,?,'[]')",
                       (title_of(question) or "Question", db.now(), db.now()))
    return cur.lastrowid


def get(conn, cid: int) -> dict | None:
    return db.row_dict(conn.execute("SELECT * FROM chat WHERE id=?", (cid,)).fetchone())


def add_turn(conn, cid: int, q: str, html: str, shown: str, facts: str = "") -> None:
    """`facts`: what the app answered, in words, for the model's reply; the
    reply itself is filled in later (None until then)."""
    c = get(conn, cid)
    if not c:
        return
    turns = (c["turns"] or []) + [{"q": q, "html": html, "shown": shown, "facts": facts,
                                   "reply": None if facts else "", "at": db.now()}]
    conn.execute("UPDATE chat SET turns=?, updated=? WHERE id=?", (db.dumps(turns), db.now(), cid))


def settle(conn, cid: int, turn: int, done_html: str) -> None:
    """A confirmation was answered (done or cancelled): the saved turn shows
    the outcome instead of the buttons, so reopening the chat can't repeat it."""
    c = get(conn, cid)
    if not c or not 0 <= turn < len(c["turns"] or []):
        return
    turns = c["turns"]
    turns[turn]["html"] = _CONFIRM.sub(lambda _: done_html, turns[turn]["html"], count=1)
    conn.execute("UPDATE chat SET turns=? WHERE id=?", (db.dumps(turns), cid))


def settle_check(conn, cid: int, turn: int, html: str) -> None:
    """A site check started or finished: the saved turn shows its progress
    (resumed when the chat is reopened) or what it found."""
    c = get(conn, cid)
    if not c or not 0 <= turn < len(c["turns"] or []):
        return
    turns = c["turns"]
    turns[turn]["html"] = _CHECK.sub(lambda _: html, turns[turn]["html"], count=1)
    conn.execute("UPDATE chat SET turns=? WHERE id=?", (db.dumps(turns), cid))


def reply_html(text: str) -> str:
    if not text:
        return ""
    paras = "".join(f"<p>{escape(p)}</p>" for p in re.split(r"\n\s*\n", text.strip()) if p.strip())
    return f'<div class="chat-reply">{paras}</div>'


def set_reply(conn, cid: int, turn: int, text: str, html: str) -> None:
    """The reply arrived: kept as text (the next replies read it) and in place
    of the waiting dots in the saved answer."""
    c = get(conn, cid)
    if not c or not 0 <= turn < len(c["turns"] or []):
        return
    turns = c["turns"]
    turns[turn]["reply"] = text
    turns[turn]["html"] = _REPLY.sub(lambda _: html, turns[turn]["html"], count=1)
    conn.execute("UPDATE chat SET turns=? WHERE id=?", (db.dumps(turns), cid))


def recent(conn, limit: int | None = SIDEBAR_MAX) -> list[dict]:
    sql = "SELECT id, title, updated FROM chat ORDER BY updated DESC, id DESC"
    return [dict(r) for r in conn.execute(sql + (f" LIMIT {int(limit)}" if limit else ""))]


def count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM chat").fetchone()[0]


def delete(conn, cid: int) -> None:
    conn.execute("DELETE FROM chat WHERE id=?", (cid,))
