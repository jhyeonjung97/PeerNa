"""Does a model already know a test paper? Asked with the title alone.

A model that can name a paper's first author from its title has read about the
paper, and may have read its peer review file too. Run on the clean test set,
published after both predictors' training data, and on papers from inside it as
a control: the control should show recall, the clean set none.

    python -m tools.memorization_probe SLUGS OUT --model luna|claude-sonnet-5-5
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel

from ai_peer_reviewer import backends, config, models

UA = {"User-Agent": "peerna/1.0 (mailto:jhyeonjung97+peerna@gmail.com)"}

QUESTION = """\
Do you know this published research paper? Title: "{title}"

Answer from memory only. If you recognise it, give its first author's family
name, the journal and the year of publication. If you do not, say so; do not
guess from the title."""


class Recall(BaseModel):
    recognised: bool
    first_author_family_name: str
    journal: str
    year: str


def crossref(slug: str) -> dict:
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    f"https://api.crossref.org/works/10.1038/{slug}", headers=UA), timeout=60) as r:
                m = json.loads(r.read())["message"]
            return {"title": (m.get("title") or [""])[0],
                    "first": ((m.get("author") or [{}])[0].get("family") or "")}
        except Exception:
            time.sleep(3 * (attempt + 1))
    return {}


def ask(model: str, title: str) -> Recall:
    if model.startswith("claude-"):
        from tools.precedent_eval import claude_cli

        reply = claude_cli("Answer briefly and honestly.", QUESTION.format(title=title),
                           model, Recall.model_json_schema())
        return Recall.model_validate(reply["structured_output"])
    backend = backends.build(models.resolve(model))
    return backend.run_structured("Answer briefly and honestly.", [],
                                  QUESTION.format(title=title), Recall, effort="low")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slugs", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    config.load()
    done = {}
    if args.out.exists():
        done = {json.loads(l)["slug"]: json.loads(l) for l in args.out.read_text().splitlines()}
    slugs = [s for s in args.slugs.read_text().split() if s not in done]
    lock = threading.Lock()

    def one(slug):
        meta = crossref(slug)
        row = {"slug": slug, "model": args.model, **meta}
        try:
            r = ask(args.model, meta["title"])
            row.update(r.model_dump())
            row["author_correct"] = bool(meta.get("first")) and \
                meta["first"].lower() in r.first_author_family_name.lower()
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:300]
        with lock, args.out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(done.values()) + list(pool.map(one, slugs))
    ok = [r for r in rows if "error" not in r]
    print(f"{args.model}: {len(ok)} answered, {sum(r['recognised'] for r in ok)} claimed to "
          f"recognise, {sum(r['author_correct'] for r in ok)} named the first author",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
