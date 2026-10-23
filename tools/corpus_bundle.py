"""Pack the corpus index for a server and, with --publish, release it.

The bundle is what a server needs to search the corpus and nothing more: titles,
abstracts and parsed comments, and the vectors. The raw review files, which
are most of the database and only needed to re-parse, stay on this machine.

    python -m tools.corpus_bundle corpus-2026-10 --publish

then set AI_PEER_REVIEWER_CORPUS=corpus-2026-10 on the server (corpus_sync).
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from ai_peer_reviewer.corpus_index import DB_PATH, INDEX_DIR
from ai_peer_reviewer.corpus_sync import DEFAULT_REPO, FILES


def slim_database(target: Path) -> None:
    """Copy papers and comments without the review files.

    Comments are copied with their rowids, which the comment vectors point at;
    a VACUUM of a copy would renumber them, so the rows are inserted instead.
    """
    target.unlink(missing_ok=True)
    out = sqlite3.connect(target)
    out.executescript(
        """
        CREATE TABLE papers (slug TEXT PRIMARY KEY, title TEXT, abstract TEXT);
        CREATE TABLE comments (slug TEXT, reviewer INTEGER, position INTEGER, text TEXT);
        """
    )
    out.execute("ATTACH DATABASE ? AS source", (str(DB_PATH),))
    out.execute("INSERT INTO papers SELECT slug, title, abstract FROM source.papers")
    out.execute(
        "INSERT INTO comments (rowid, slug, reviewer, position, text) "
        "SELECT rowid, slug, reviewer, position, text FROM source.comments"
    )
    out.commit()
    out.execute("DETACH DATABASE source")
    out.execute("CREATE INDEX comments_slug ON comments(slug)")
    out.commit()
    out.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="Release tag, e.g. corpus-2026-10")
    parser.add_argument("--out", type=Path,
                        default=Path.home() / "PeerNa-data" / "bundles")
    parser.add_argument("--publish", action="store_true",
                        help="Create the GitHub release and upload the files")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)

    folder = args.out / args.tag
    folder.mkdir(parents=True, exist_ok=True)
    slim = folder / "corpus.sqlite"
    slim_database(slim)

    packed = []
    for name in FILES:
        source = slim if name == "corpus.sqlite" else INDEX_DIR / name
        target = folder / (name + ".gz")
        with open(source, "rb") as raw, gzip.open(target, "wb", compresslevel=6) as out:
            shutil.copyfileobj(raw, out, 1 << 20)
        packed.append(target)
        print(f"  {target.name}: {target.stat().st_size / 1e6:.0f} MB", file=sys.stderr)
    slim.unlink()

    if args.publish:
        subprocess.run(
            ["gh", "release", "create", args.tag, *map(str, packed),
             "--repo", args.repo, "--title", f"Corpus index {args.tag}",
             "--notes", "Corpus index for the server; see tools/corpus_bundle.py.",
             "--latest=false"],
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
