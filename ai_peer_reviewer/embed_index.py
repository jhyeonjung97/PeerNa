"""Rebuild the similarity index from whatever is in the database.

Kept separate from harvesting so the two can fail independently: a harvest that
ran for three hours should not be lost because an embedding call timed out at the
end, and re-embedding should not mean re-downloading.
"""

from __future__ import annotations

import json
import sqlite3

from .corpus_index import (
    COMMENT_DIMENSIONS,
    COMMENT_IDS_PATH,
    COMMENT_VECTORS_PATH,
    DB_PATH,
    IDS_PATH,
    INDEX_DIR,
    VECTORS_PATH,
    embed,
    is_referee_comment,
    parse_review,
    points_only,
    schema,
    store_comments,
)

#: Comments shorter than this are almost all fragments the parser cut loose —
#: "Minor comments:", a stray page number — and say nothing a search could use.
SHORTEST_COMMENT = 40

BATCH = 500


def since_clause(since: int, column: str = "slug") -> str:
    """SQL keeping papers from `since` on, by the year in a Nature slug.

    `s41467-021-22047-w` was registered in 2021; the year a DOI is registered
    is the year of acceptance, near enough to publication for a cut-off.
    """
    if not since:
        return ""
    return f" AND CAST(substr({column}, 8, 3) AS INTEGER) >= {int(since) - 2000}"


def reparse() -> tuple[int, int]:
    """Parse every stored review file again with the current parser.

    The comments table is what an older parser made of `review_text`; the
    parser has improved since, and re-parsing costs nothing where re-fetching
    takes a night. Papers whose file now yields nothing keep their old comments
    rather than vanish. Returns (papers re-parsed, comments now stored).
    """
    connection = sqlite3.connect(DB_PATH)
    schema(connection)
    rows = connection.execute("SELECT slug, text FROM review_text").fetchall()
    redone = 0
    for slug, text in rows:
        parsed = parse_review(text or "")
        if not parsed:
            continue
        store_comments(connection, slug, parsed)
        redone += 1
    connection.commit()
    total = connection.execute("SELECT count(*) FROM comments").fetchone()[0]
    connection.close()
    return redone, total


def rebuild(progress=None, since: int = 0) -> int:
    import numpy as np

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    rows = connection.execute(
        "SELECT slug, title, abstract FROM papers WHERE 1" + since_clause(since)
        + " ORDER BY slug"
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


def rebuild_comments(progress=None, since: int = 0) -> int:
    """One vector per referee comment, for finding precedents part by part.

    Reference-list entries and the authors' replies are left out here though
    they stay in the database: both are numbered like comments and survive
    parsing, and in a search by method they surface as precedents that no
    referee wrote. See `is_referee_comment`.
    """
    import numpy as np

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    # Points only: an opening assessment ("an interesting study of…") is about
    # the paper as a whole and would match every part of every manuscript.
    rows = connection.execute(
        "SELECT rowid, slug, text FROM comments WHERE length(text) >= ?"
        + points_only(connection) + since_clause(since) + " ORDER BY rowid",
        (SHORTEST_COMMENT,),
    ).fetchall()
    connection.close()
    rows = [r for r in rows if is_referee_comment(r[2])]
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
    parser.add_argument("--reparse", action="store_true",
                        help="First re-parse the stored review files into comments (free).")
    parser.add_argument("--since", type=int, default=0,
                        help="Index papers from this year on only, e.g. 2021.")
    args = parser.parse_args(argv)

    def report(done: int, total: int) -> None:
        print(f"  embedded {done}/{total}", file=sys.stderr, flush=True)

    if args.reparse:
        papers, comments = reparse()
        print(f"Re-parsed {papers} review files; {comments} comments.", file=sys.stderr)
    if not args.comments_only:
        count = rebuild(report, args.since)
        print(f"Indexed {count} papers.", file=sys.stderr)
    if args.comments or args.comments_only:
        count = rebuild_comments(report, args.since)
        print(f"Indexed {count} comments.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
