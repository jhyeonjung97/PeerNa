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
    rather than vanish, and papers a model has read are left alone. Returns (papers re-parsed, comments now stored).
    """
    connection = sqlite3.connect(DB_PATH)
    schema(connection)
    rows = connection.execute("SELECT slug, text FROM review_text").fetchall()
    # A paper a model has read keeps the model's comments.
    read = set()
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'comments_model'").fetchone():
        read = {s for (s,) in connection.execute("SELECT DISTINCT slug FROM comments_model")}
    redone = 0
    for slug, text in rows:
        if slug in read:
            continue
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

    # Papers already in the index keep their vectors (a paper's title and
    # abstract do not change), so adding papers embeds only the new ones.
    old = {}
    if VECTORS_PATH.exists() and IDS_PATH.exists():
        old_vectors = np.load(VECTORS_PATH, mmap_mode="r")
        old = {slug: i for i, slug in enumerate(json.loads(IDS_PATH.read_text()))}
    array = np.zeros((len(slugs), old_vectors.shape[1] if old else 1536), dtype="float32")
    fresh = [i for i, slug in enumerate(slugs) if slug not in old]
    for i, slug in enumerate(slugs):
        if slug in old:
            array[i] = old_vectors[old[slug]]
    for start in range(0, len(fresh), BATCH):
        chunk = fresh[start:start + BATCH]
        vectors = np.array(embed([texts[i] for i in chunk]), dtype="float32")
        # Stored unit-length so a search is one matrix multiply and no division.
        array[chunk] = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        if progress:
            progress(min(start + BATCH, len(fresh)), len(fresh))
    np.save(VECTORS_PATH, array)
    IDS_PATH.write_text(json.dumps(slugs))
    return len(slugs)


def rebuild_comments(progress=None, since: int = 0, workers: int = 4) -> int:
    """One vector per referee comment, for finding precedents part by part.

    Reference-list entries and the authors' replies are left out here though
    they stay in the database: both are numbered like comments and survive
    parsing, and in a search by method they surface as precedents that no
    referee wrote. See `is_referee_comment`.

    The vectors go straight into a file on disk as they arrive, with a note of
    which batches are done, so a stopped run carries on where it was. Held in
    memory as Python lists, 1.3 million of them outgrew an 8 GB machine and the
    run was killed after five hours, at the save, with nothing kept.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

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

    total = len(rows)
    partial = COMMENT_VECTORS_PATH.with_name("comment_vectors.partial.npy")
    state_path = COMMENT_VECTORS_PATH.with_name("comment_vectors.partial.json")
    # A partial run is only resumed for the same rows in the same order.
    key = {"total": total, "first": rows[0][0], "last": rows[-1][0], "since": since,
           "batch": BATCH, "dimensions": COMMENT_DIMENSIONS}
    done: set[int] = set()
    if partial.exists() and state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("key") == key:
            done = set(state["done"])
    array = np.lib.format.open_memmap(
        partial, mode="r+" if done else "w+", dtype="float16",
        shape=(total, COMMENT_DIMENSIONS))
    lock = threading.Lock()

    # Comments already in the index keep their vectors: a stored comment's row
    # never changes text (re-reading a paper deletes its rows and inserts new
    # ones), so the same rowid with the same paper is the same comment. Adding
    # papers then embeds only their comments.
    reuse: dict[int, int] = {}
    if not done and COMMENT_VECTORS_PATH.exists() and COMMENT_IDS_PATH.exists():
        old_ids = json.loads(COMMENT_IDS_PATH.read_text())
        before = {rowid: (i, slug) for i, (rowid, slug) in enumerate(zip(old_ids["rowid"], old_ids["slug"]))}
        old_vectors = np.load(COMMENT_VECTORS_PATH, mmap_mode="r")
        for n, (rowid, slug, _) in enumerate(rows):
            hit = before.get(rowid)
            if hit and hit[1] == slug:
                reuse[n] = hit[0]
        if reuse:
            positions = np.fromiter(reuse.keys(), dtype=np.int64)
            sources = np.fromiter(reuse.values(), dtype=np.int64)
            for start in range(0, len(positions), 100_000):
                array[positions[start:start + 100_000]] = old_vectors[sources[start:start + 100_000]]

    def save_state() -> None:
        array.flush()
        state_path.write_text(json.dumps({"key": key, "done": sorted(done)}))

    def one(start: int) -> None:
        chunk = [n for n in range(start, min(start + BATCH, total)) if n not in reuse]
        if chunk:
            vectors = np.array(embed([rows[n][2] for n in chunk], dimensions=COMMENT_DIMENSIONS),
                               dtype="float32")
            # Stored unit-length, as the paper vectors are.
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            array[chunk] = vectors.astype("float16")
        with lock:
            done.add(start)
            if len(done) % 20 == 0:
                save_state()
            if progress:
                progress(min(len(done) * BATCH, total), total)

    todo = [start for start in range(0, total, BATCH) if start not in done]
    try:
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(one, todo))
    finally:
        save_state()
    del array

    partial.replace(COMMENT_VECTORS_PATH)
    COMMENT_IDS_PATH.write_text(json.dumps(
        {"rowid": [r[0] for r in rows], "slug": [r[1] for r in rows]}
    ))
    state_path.unlink(missing_ok=True)
    return total


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
