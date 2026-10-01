"""Source adapters. Each returns RawItems; the pipeline does the rest."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawItem:
    url: str
    title: str
    source_id: str
    snippet: str = ""           # short text from the listing (used to prefilter)
    text: str = ""              # full main text, when the adapter already has it
    published: str | None = None
    hints: dict = field(default_factory=dict)  # e.g. deadline, org, country from structured sources
    needs_detail: bool = True   # fetch the page for full text
