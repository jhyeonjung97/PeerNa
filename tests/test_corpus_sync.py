"""The corpus bundle round trip: packed, fetched, swapped in whole.

Three things would each break search without an error: a bundle whose comment
rowids no longer match the vectors, a half-fetched index left in place, and a
server fetch overwriting the copy the index is built from.

    python3 -m tests.test_corpus_sync
"""

import gzip
import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from ai_peer_reviewer import corpus_index, corpus_sync
from tools import corpus_bundle


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = tmp / "master.sqlite"
        db = sqlite3.connect(source)
        corpus_index.schema(db)
        db.execute("CREATE TABLE review_text (slug TEXT PRIMARY KEY, text TEXT)")
        db.execute("INSERT INTO papers VALUES ('a', 'A', 'abstract')")
        for n in range(5):
            db.execute("INSERT INTO comments VALUES ('a', 1, ?, ?)", (n, f"comment {n}"))
        db.execute("DELETE FROM comments WHERE position = 1")  # leave a gap in rowids
        db.execute("INSERT INTO review_text VALUES ('a', 'raw')")
        db.commit()
        db.close()

        corpus_bundle.DB_PATH = source
        slim = tmp / "slim.sqlite"
        corpus_bundle.slim_database(slim)
        db = sqlite3.connect(slim)
        if db.execute("SELECT name FROM sqlite_master WHERE name='review_text'").fetchone():
            failures.append("the bundle carries the review files")
        rows = db.execute("SELECT rowid, text FROM comments ORDER BY rowid").fetchall()
        if rows != [(1, "comment 0"), (3, "comment 2"), (4, "comment 3"), (5, "comment 4")]:
            failures.append(f"comment rowids changed in the bundle: {rows}")
        db.close()

        index = tmp / "index"
        corpus_sync.INDEX_DIR = index
        corpus_sync.MARKER = index / "BUNDLE"
        corpus_sync.DB_PATH = index / "corpus.sqlite"
        release = {name: gzip.compress(name.encode()) for name in corpus_sync.FILES}
        broken = {"comment_ids.json"}

        def fake(url, token):
            if url in broken:
                raise OSError("connection dropped")
            return io.BytesIO(release[url])

        corpus_sync._download = fake
        listing = {"assets": [{"name": n + ".gz", "url": n} for n in corpus_sync.FILES]}
        corpus_sync.urllib.request.urlopen = lambda *a, **k: io.BytesIO(json.dumps(listing).encode())
        index.mkdir()
        (index / "comment_vectors.npy").write_bytes(b"old")
        try:
            corpus_sync.fetch("v1", "token")
            failures.append("a failed download was reported as done")
        except OSError:
            pass
        if (index / "comment_vectors.npy").read_bytes() != b"old" or corpus_sync.current():
            failures.append("a half-fetched bundle replaced the index")

        broken.clear()
        corpus_sync.fetch("v1", "token")
        if corpus_sync.current() != "v1" or \
                (index / "comment_vectors.npy").read_bytes() != b"comment_vectors.npy":
            failures.append("a complete bundle was not swapped in")

        corpus_sync.DB_PATH = source
        if not corpus_sync.is_master():
            failures.append("the database with review files is not recognised as the master")

    for failure in failures:
        print("FAIL:", failure)
    print("corpus sync:", "ok" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
