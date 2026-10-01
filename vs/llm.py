"""Thin client for the local Ollama server (chat with a JSON schema, embeddings)."""

import json
import time

import httpx
import numpy as np

from vs.config import prefs


class LLMError(RuntimeError):
    pass


def _cfg() -> dict:
    return prefs().get("llm", {})


def host() -> str:
    return _cfg().get("host", "http://127.0.0.1:11434").rstrip("/")


def available_models() -> list[str]:
    try:
        r = httpx.get(f"{host()}/api/tags", timeout=5)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama is not reachable at {host()}. Start it with 'ollama serve'.") from e
    return [m["name"] for m in r.json().get("models", [])]


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


def chat_json(system: str, user: str, schema: dict, *, model: str | None = None,
              think: bool = False, temperature: float = 0.0, retries: int = 1) -> dict:
    """One chat call constrained to `schema`. Retries once on invalid JSON."""
    model = model or chat_model()
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "format": schema,
        "stream": False,
        "think": think,
        "keep_alive": "5m",
        "options": {"temperature": temperature, "num_ctx": int(_cfg().get("num_ctx", 8192))},
    }
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = httpx.post(f"{host()}/api/chat", json=payload, timeout=600)
            if r.status_code == 400 and "think" in r.text:
                payload.pop("think", None)  # model without a thinking switch
                r = httpx.post(f"{host()}/api/chat", json=payload, timeout=600)
            r.raise_for_status()
            content = r.json()["message"]["content"]
            return json.loads(content)
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
            last_err = e
            time.sleep(1 + attempt)
    raise LLMError(f"LLM call failed: {last_err}")


def embed(texts: list[str], *, model: str | None = None) -> list[np.ndarray]:
    """Normalised embeddings, batched."""
    model = model or embed_model()
    out: list[np.ndarray] = []
    for i in range(0, len(texts), 16):
        batch = [t[:6000] or " " for t in texts[i:i + 16]]
        try:
            r = httpx.post(f"{host()}/api/embed", json={"model": model, "input": batch, "keep_alive": "2m"}, timeout=300)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"Embedding failed: {e}") from e
        for v in r.json()["embeddings"]:
            arr = np.asarray(v, dtype=np.float32)
            out.append(arr / (np.linalg.norm(arr) or 1.0))
    return out
