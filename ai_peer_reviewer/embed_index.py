"""Rebuild the similarity index from whatever is in the database.

Kept separate from harvesting so the two can fail independently: a harvest that
ran for three hours should not be lost because an embedding call timed out at the
end, and re-embedding should not mean re-downloading.
"""

from __future__ import annotations

import json
import sqlite3

from .corpus_index import DB_PATH, IDS_PATH, INDEX_DIR, VECTORS_PATH, embed

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


def main(argv: list[str] | None = None) -> int:
    import sys

    def report(done: int, total: int) -> None:
        print(f"  embedded {done}/{total}", file=sys.stderr, flush=True)

    count = rebuild(report)
    print(f"Indexed {count} papers.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
