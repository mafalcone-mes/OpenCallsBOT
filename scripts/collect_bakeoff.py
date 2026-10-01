"""Collect real call pages for the model bake-off. Saves main text to
tests/bakeoff/pages/<n>.txt and prints the lines that mention deadlines or
money, so the expected values can be labelled by hand in cases.json."""

import json
import re
import sys
from pathlib import Path

from vs.config import prefs
from vs.fetch.adapters import ADAPTERS
from vs.fetch.polite import PoliteClient
from vs.pipeline import page_text

OUT = Path(__file__).resolve().parent.parent / "tests" / "bakeoff" / "pages"
OUT.mkdir(parents=True, exist_ok=True)
PER_SOURCE = {"on-the-move": 4, "exibart": 4, "euraxess": 4, "eu-funding": 1, "artapartofculture": 2}
HINT = re.compile(r"deadline|scadenza|termine|entro il|uiterlijk|deadline|bewerbungsschluss|frist|plazo|fecha límite|"
                  r"€|eur\b|euro|fee|quota|stipend|honorar|salary|salario|stipendio|compens|vergoeding|beca", re.I)


def main() -> None:
    client = PoliteClient()
    index = []
    n = 0
    for sid, k in PER_SOURCE.items():
        cfg = prefs()["sources"][sid]
        items = list(ADAPTERS[cfg["adapter"]](client, sid, cfg))[: k * 3]
        taken = 0
        for it in items:
            if taken >= k:
                break
            text = it.text if not it.needs_detail else page_text(client.get(it.url).text, it.url)
            if len(text) < 400:
                continue
            n += 1
            taken += 1
            (OUT / f"{n:02d}.txt").write_text(text, encoding="utf-8")
            index.append({"id": f"{n:02d}", "source": sid, "url": it.url, "title": it.title})
            print(f"\n=== {n:02d} [{sid}] {it.title[:90]}\n{it.url}")
            for line in text.splitlines():
                if HINT.search(line):
                    print("   >", line.strip()[:220])
    (OUT.parent / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
