"""Rebuild the similarity index from whatever is in the database.

Kept separate from harvesting so the two can fail independently: a harvest that
ran for three hours should not be lost because an embedding call timed out at the
end, and re-embedding should not mean re-downloading.
"""

from __future__ import annotations

import json
import sqlite3

from .corpus_index import (
    BIBLIOGRAPHIC,
    COMMENT_DIMENSIONS,
    COMMENT_IDS_PATH,
    COMMENT_VECTORS_PATH,
    DB_PATH,
    IDS_PATH,
    INDEX_DIR,
    VECTORS_PATH,
    embed,
)

#: Comments shorter than this are almost all fragments the parser cut loose —
#: "Minor comments:", a stray page number — and say nothing a search could use.
SHORTEST_COMMENT = 40

BATCH = 500


def rebuild(progress=None) -> int:
    import numpy as np

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    rows = connection.execute(
        "SELECT slug, title, abstract FROM papers ORDER BY slug"
    ).fetchall()
    connection.close()
    if not rows:
        return 0

    slugs = [r[0] for r in rows]
    texts = [f"{r[1]} {r[2]}".strip() for r in rows]

    vectors = []
    for start in range(0, len(texts), BATCH):
        vectors.extend(embed(texts[start:start + BATCH]))
        if progress:
            progress(min(start + BATCH, len(texts)), len(texts))

    array = np.array(vectors, dtype="float32")
    # Stored unit-length so a search is one matrix multiply and no division.
    array /= np.linalg.norm(array, axis=1, keepdims=True)
    np.save(VECTORS_PATH, array)
    IDS_PATH.write_text(json.dumps(slugs))
    return len(slugs)


def rebuild_comments(progress=None) -> int:
    """One vector per referee comment, for finding precedents part by part.

    Reference-list entries are left out here though they stay in the database:
    they are numbered like comments and survive parsing, and in a search by
    method they would surface whenever a query named a technique.
    """
    import numpy as np

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    rows = connection.execute(
        "SELECT rowid, slug, text FROM comments WHERE length(text) >= ? ORDER BY rowid",
        (SHORTEST_COMMENT,),
    ).fetchall()
    connection.close()
    rows = [r for r in rows if not BIBLIOGRAPHIC.search(r[2])]
    if not rows:
        return 0

    vectors = []
    for start in range(0, len(rows), BATCH):
        chunk = [r[2] for r in rows[start:start + BATCH]]
        vectors.extend(embed(chunk, dimensions=COMMENT_DIMENSIONS))
        if progress:
            progress(min(start + BATCH, len(rows)), len(rows))

    array = np.array(vectors, dtype="float32")
    array /= np.linalg.norm(array, axis=1, keepdims=True)
    np.save(COMMENT_VECTORS_PATH, array.astype("float16"))
    COMMENT_IDS_PATH.write_text(json.dumps(
        {"rowid": [r[0] for r in rows], "slug": [r[1] for r in rows]}
    ))
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--comments", action="store_true",
                        help="Also rebuild the per-comment index (~$0.35, ~20 min).")
    parser.add_argument("--comments-only", action="store_true",
                        help="Rebuild the per-comment index and leave the papers alone.")
    args = parser.parse_args(argv)

    def report(done: int, total: int) -> None:
        print(f"  embedded {done}/{total}", file=sys.stderr, flush=True)

    if not args.comments_only:
        count = rebuild(report)
        print(f"Indexed {count} papers.", file=sys.stderr)
    if args.comments or args.comments_only:
        count = rebuild_comments(report)
        print(f"Indexed {count} comments.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
