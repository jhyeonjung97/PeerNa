"""A map of what referees ask for that does not depend on the field.

The coverage runs showed that what the corpus holds and a model lacks is
presentation points and general substantive ones, neither tied to a field. The
review passes were designed by hand; this draws the kinds of request from the
corpus instead, so the passes can be built around what referees actually raise.

1. Sample one comment from each of N papers (spread over fields), skipping the
   test papers, and label each presentation, general or field (Haiku, the same
   criteria as tools/classify_points.py).
2. Cluster the presentation and general comments on their stored vectors.
3. Name each cluster from its comments and count it.

    CLAUDE_CONFIG_DIR=… python -m tools.comment_types OUT --exclude test.slugs
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from ai_peer_reviewer import corpus_index  # noqa: E402
from tools.classify_points import PROMPT as CLASSIFY_PROMPT, SYSTEM as CLASSIFY_SYSTEM, Labels  # noqa: E402
from tools.precedent_eval import backend_for  # noqa: E402

BATCH = 50

NAME_SYSTEM = "You summarise groups of referee comments and reply with JSON only."
NAME_PROMPT = """\
Below are referee comments that a clustering put together, from papers in many
fields. Say what kind of request they share, so that a reviewer could check a
new manuscript for it.

name: a short name for the kind of request (a few words).
check: one sentence a reviewer could apply to any manuscript, phrased as what to
  check ("Is every term central to the claims defined where it is first used?").
coherent: whether most of the comments really share this one kind of request.

COMMENTS
{comments}
"""


class ClusterName(BaseModel):
    name: str
    check: str
    coherent: bool


def sample(n: int, exclude: set[str], seed: int) -> list[dict]:
    """One indexed comment from each of n random papers."""
    ids = json.loads(corpus_index.COMMENT_IDS_PATH.read_text())
    by_paper: dict[str, list[int]] = collections.defaultdict(list)
    for position, slug in enumerate(ids["slug"]):
        if slug not in exclude:
            by_paper[slug].append(position)
    rng = random.Random(seed)
    papers = rng.sample(sorted(by_paper), min(n, len(by_paper)))
    connection = sqlite3.connect(f"file:{corpus_index.DB_PATH}?mode=ro", uri=True)
    picked = []
    for slug in papers:
        position = rng.choice(by_paper[slug])
        rowid = ids["rowid"][position]
        (text,) = connection.execute("SELECT text FROM comments WHERE rowid = ?", (rowid,)).fetchone()
        picked.append({"slug": slug, "position": position, "rowid": rowid, "text": text})
    connection.close()
    return picked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--exclude", type=Path, help="Slugs to leave out, one per line (the test set).")
    parser.add_argument("--papers", type=int, default=3000)
    parser.add_argument("--clusters", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--label-model", default="cli:claude-haiku-5-5")
    parser.add_argument("--name-model", default="cli:claude-sonnet-5-5")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    # 1. Sample and label.
    sample_path = args.out / "sample.json"
    if sample_path.is_file():
        comments = json.loads(sample_path.read_text())
    else:
        exclude = set(args.exclude.read_text().split()) if args.exclude else set()
        comments = sample(args.papers, exclude, args.seed)
        sample_path.write_text(json.dumps(comments, indent=1, ensure_ascii=False))
    label_dir = args.out / "labels"
    label_dir.mkdir(exist_ok=True)
    backend, _ = backend_for(args.label_model)

    def label(start: int) -> None:
        path = label_dir / f"{start:05d}.json"
        if path.is_file():
            return
        batch = comments[start:start + BATCH]
        listed = "\n".join(f"R{n}. {c['text'][:800]}" for n, c in enumerate(batch, 1))
        answer = backend.run_structured(CLASSIFY_SYSTEM, [], CLASSIFY_PROMPT.format(comments=listed),
                                        Labels, effort="medium")
        labels = {l.id: l.kind for l in answer.labels}
        path.write_text(json.dumps(labels))
        with lock:
            print(f"  labelled {start + len(batch)}/{len(comments)}", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(label, range(0, len(comments), BATCH)))
    for start in range(0, len(comments), BATCH):
        labels = json.loads((label_dir / f"{start:05d}.json").read_text())
        for n, c in enumerate(comments[start:start + BATCH], 1):
            c["kind"] = labels.get(f"R{n}")
    kinds = collections.Counter(c["kind"] for c in comments)
    print(f"kinds: {dict(kinds)}", file=sys.stderr)

    # 2. Cluster the presentation and general ones on their stored vectors.
    vectors = np.load(corpus_index.COMMENT_VECTORS_PATH, mmap_mode="r")
    chosen = [c for c in comments if c["kind"] in ("presentation", "general")]
    matrix = np.array([vectors[c["position"]] for c in chosen], dtype="float32")
    from sklearn.cluster import KMeans

    assignment = KMeans(n_clusters=args.clusters, n_init=10, random_state=args.seed).fit_predict(matrix)
    for c, k in zip(chosen, assignment):
        c["cluster"] = int(k)

    # 3. Name each cluster from up to 20 of its comments.
    namer, _ = backend_for(args.name_model)
    names_path = args.out / "clusters.json"
    clusters = json.loads(names_path.read_text()) if names_path.is_file() else {}

    def name(k: int) -> None:
        if str(k) in clusters:
            return
        members = [c for c in chosen if c["cluster"] == k]
        shown = random.Random(k).sample(members, min(20, len(members)))
        listed = "\n".join(f"- {c['text'][:500]}" for c in shown)
        answer = namer.run_structured(NAME_SYSTEM, [], NAME_PROMPT.format(comments=listed),
                                      ClusterName, effort="medium")
        with lock:
            clusters[str(k)] = {**answer.model_dump(), "size": len(members),
                                "kinds": dict(collections.Counter(c["kind"] for c in members)),
                                "examples": [c["text"][:300] for c in shown[:5]]}
            names_path.write_text(json.dumps(clusters, indent=1, ensure_ascii=False))
            print(f"  cluster {k}: {answer.name} ({len(members)})", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(name, range(args.clusters)))
    sample_path.write_text(json.dumps(comments, indent=1, ensure_ascii=False))

    total = len(comments)
    lines = [f"{total} comments from {total} papers: " + ", ".join(
        f"{k} {kinds[k]} ({kinds[k] / total:.0%})" for k in ("presentation", "general", "field")), "",
        f"{len(chosen)} presentation and general comments in {args.clusters} clusters, largest first:", ""]
    for k, c in sorted(clusters.items(), key=lambda kv: -kv[1]["size"]):
        mark = "" if c["coherent"] else "  (mixed)"
        lines.append(f"{c['size']:4}  {c['size'] / total:5.1%}  {c['name']}{mark}  "
                     f"[{', '.join(f'{a} {b}' for a, b in sorted(c['kinds'].items()))}]\n"
                     f"            {c['check']}")
    summary = "\n".join(lines)
    (args.out / "summary.txt").write_text(summary + "\n")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
