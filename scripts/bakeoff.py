"""Model bake-off: run the extraction on hand-labelled real pages with each
model and report accuracy and speed.

    uv run python scripts/bakeoff.py qwen3.5:9b qwen3:8b
"""

import json
import sys
import time
from pathlib import Path

from vs import extract, llm

HERE = Path(__file__).resolve().parent.parent / "tests" / "bakeoff"


def score_case(case: dict, out: dict, checks: dict) -> dict:
    r = {"is_opp": out.get("is_opportunity") == case["is_opportunity"]}
    if not case["is_opportunity"]:
        return r
    r["deadline"] = (out.get("deadline") or "")[:10] == case["deadline"]
    r["deadline_verified"] = bool(checks.get("deadline", {}).get("verified"))
    r["type"] = out.get("type") in case["type"]
    if "money_kind" in case:
        r["money_kind"] = bool(set(out.get("money_kind") or []) & set(case["money_kind"]))
    if "amount" in case:
        want = case["amount"] if isinstance(case["amount"], list) else [case["amount"]]
        r["amount"] = out.get("amount") is not None and any(abs(float(out["amount"]) - w) < 1 for w in want)
    if "age_max" in case:
        r["age_max"] = out.get("age_max") == case["age_max"]
    if "country" in case:
        r["country"] = (out.get("location_country") or "").upper() == case["country"]
    return r


def run(model: str) -> dict:
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    totals: dict[str, list[bool]] = {}
    times, failures = [], 0
    for case in cases:
        text = (HERE / "pages" / f"{case['id']}.txt").read_text(encoding="utf-8")
        t = time.time()
        try:
            out = llm.chat_json(extract.SYSTEM, f"Today is 2026-09-30.\n\n--- PAGE TEXT ---\n{text[:14000]}",
                                extract.SCHEMA, model=model)
        except llm.LLMError as e:
            failures += 1
            print(f"  {case['id']}: FAILED {e}")
            continue
        times.append(time.time() - t)
        checks = extract.verify(out, text, case["lang"])
        res = score_case(case, out, checks)
        for k, v in res.items():
            totals.setdefault(k, []).append(v)
        bad = [k for k, v in res.items() if not v]
        print(f"  {case['id']} {times[-1]:5.1f}s  {'ok' if not bad else 'wrong: ' + ', '.join(bad)}"
              f"  | deadline={out.get('deadline')!r} type={out.get('type')} money={out.get('money_kind')} amount={out.get('amount')}")
    summary = {k: f"{sum(v)}/{len(v)}" for k, v in totals.items()}
    summary["json_failures"] = failures
    summary["sec_per_page"] = round(sum(times) / max(1, len(times)), 1)
    return summary


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    results = {}
    for model in sys.argv[1:] or ["qwen3.5:9b"]:
        print(f"== {model} (page 01 includes loading the model)")
        results[model] = run(model)
    print(json.dumps(results, indent=2))
