"""Thin client for the local Ollama server (chat with a JSON schema, embeddings)."""

import json
import threading
import time
from collections import defaultdict

import httpx
import numpy as np

from vs.config import prefs


class LLMError(RuntimeError):
    pass


def _cfg() -> dict:
    return prefs().get("llm", {})


def host() -> str:
    return _cfg().get("host", "http://127.0.0.1:11434").rstrip("/")


_tags: tuple[float, list[str]] = (0.0, [])


def available_models() -> list[str]:
    """Pulled models; asked again at most once a minute (not on every call)."""
    global _tags
    if time.monotonic() - _tags[0] < 60 and _tags[1]:
        return _tags[1]
    try:
        r = httpx.get(f"{host()}/api/tags", timeout=5)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama is not reachable at {host()}. Start it with 'ollama serve'.") from e
    _tags = (time.monotonic(), [m["name"] for m in r.json().get("models", [])])
    return _tags[1]


def _resolve(name: str, installed: list[str]) -> str | None:
    for m in installed:
        if m == name or m == f"{name}:latest" or m.split(":")[0] == name and ":" not in name:
            return m
    return None


def chat_model() -> str:
    installed = available_models()
    for name in (_cfg().get("model"), _cfg().get("fallback_model")):
        if name and (found := _resolve(name, installed)):
            return found
    raise LLMError(f"None of the configured chat models are pulled. Run: ollama pull {_cfg().get('model')}")


def extract_model() -> str:
    """Model that reads pages: [llm].extract_model (small and fast), else the chat model."""
    name = _cfg().get("extract_model")
    if name and (found := _resolve(name, available_models())):
        return found
    return chat_model()


def embed_model() -> str:
    name = _cfg().get("embed_model", "bge-m3")
    found = _resolve(name, available_models())
    if not found:
        raise LLMError(f"Embedding model not pulled. Run: ollama pull {name}")
    return found


def unload(model: str | None) -> None:
    """Free a model's RAM/VRAM now. Ollama otherwise keeps every model it has
    used loaded at once, which on 16 GB RAM starves the rest of the machine."""
    if not model:
        return
    try:
        httpx.post(f"{host()}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except httpx.HTTPError:
        pass


# ---------------------------------------------------------------- timing

_stats: dict[str, dict] = defaultdict(lambda: {"calls": 0, "seconds": 0.0, "prompt_tokens": 0, "prompt_s": 0.0,
                                               "output_tokens": 0, "output_s": 0.0, "load_s": 0.0})
_stats_lock = threading.Lock()


def _record(purpose: str, seconds: float, body: dict) -> None:
    with _stats_lock:
        s = _stats[purpose]
        s["calls"] += 1
        s["seconds"] += seconds
        s["prompt_tokens"] += body.get("prompt_eval_count") or 0
        s["prompt_s"] += (body.get("prompt_eval_duration") or 0) / 1e9
        s["output_tokens"] += body.get("eval_count") or 0
        s["output_s"] += (body.get("eval_duration") or 0) / 1e9
        s["load_s"] += (body.get("load_duration") or 0) / 1e9


def take_stats() -> dict[str, dict]:
    """Totals per purpose since the last call, then reset."""
    with _stats_lock:
        out = {k: dict(v) for k, v in _stats.items()}
        _stats.clear()
    return out


def describe_stats(purpose: str, s: dict) -> str:
    n = s["calls"] or 1
    rate = lambda tok, sec: f"{tok / sec:.0f} tok/s" if sec else "?"  # noqa: E731
    return (f"{purpose}: {s['calls']} calls, {s['seconds'] / n:.1f} s each "
            f"(reading {s['prompt_tokens'] // n} tok at {rate(s['prompt_tokens'], s['prompt_s'])}, "
            f"writing {s['output_tokens'] // n} tok at {rate(s['output_tokens'], s['output_s'])}, "
            f"loading {s['load_s']:.0f} s in total)")


CHAT_KEEP_ALIVE = "5m"


def _num_ctx() -> int:
    return int(_cfg().get("num_ctx", 8192))


def warm(model: str | None = None) -> None:
    """Load the chat model into memory now, without asking it anything, so the
    next question doesn't wait for it (~8 s on 8 GB VRAM). Same context size and
    keep-alive as chat_json: with another num_ctx Ollama would load it again."""
    model = model or chat_model()
    try:
        httpx.post(f"{host()}/api/generate", json={"model": model, "keep_alive": CHAT_KEEP_ALIVE,
                                                   "options": {"num_ctx": _num_ctx()}}, timeout=120)
    except httpx.HTTPError:
        pass  # the question itself will load it, or say Ollama is down


def chat_json(system: str, user: str, schema: dict, *, model: str | None = None,
              think: bool = False, temperature: float = 0.0, retries: int = 1,
              max_tokens: int | None = None, purpose: str = "chat", seed: int | None = None) -> dict:
    """One chat call constrained to `schema`. Retries once on invalid JSON.
    `max_tokens` caps the answer, so a model that rambles can't take minutes."""
    model = model or chat_model()
    options = {"temperature": temperature, "num_ctx": _num_ctx()}
    if max_tokens:
        options["num_predict"] = max_tokens
    if seed is not None:
        options["seed"] = seed  # with temperature 0: the same question, the same answer
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "format": schema,
        "stream": False,
        "think": think,
        "keep_alive": CHAT_KEEP_ALIVE,
        "options": options,
    }
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            started = time.monotonic()
            r = httpx.post(f"{host()}/api/chat", json=payload, timeout=600)
            if r.status_code == 400 and "think" in r.text:
                payload.pop("think", None)  # model without a thinking switch
                r = httpx.post(f"{host()}/api/chat", json=payload, timeout=600)
            r.raise_for_status()
            body = r.json()
            _record(purpose, time.monotonic() - started, body)
            if body.get("done_reason") == "length" and "num_predict" in options:
                # Cut off by the cap (a run-on quote): ask again without it
                # rather than lose the page to half a JSON object.
                options.pop("num_predict")
                raise json.JSONDecodeError("answer cut off at max_tokens", "", 0)
            return json.loads(body["message"]["content"])
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
            last_err = e
            time.sleep(1 + attempt)
    raise LLMError(f"LLM call failed: {last_err}")


# During a refresh the agents read pages with the chat model on the GPU while
# others compare texts: embeddings then run on the CPU, so the two models never
# push each other out of video memory.
embed_options: dict = {}


def embed(texts: list[str], *, model: str | None = None) -> list[np.ndarray]:
    """Normalised embeddings, batched."""
    model = model or embed_model()
    out: list[np.ndarray] = []
    for i in range(0, len(texts), 16):
        batch = [t[:6000] or " " for t in texts[i:i + 16]]
        payload = {"model": model, "input": batch, "keep_alive": "2m"}
        if embed_options:
            payload["options"] = embed_options
        try:
            r = httpx.post(f"{host()}/api/embed", json=payload, timeout=300)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"Embedding failed: {e}") from e
        for v in r.json()["embeddings"]:
            arr = np.asarray(v, dtype=np.float32)
            out.append(arr / (np.linalg.norm(arr) or 1.0))
    return out
