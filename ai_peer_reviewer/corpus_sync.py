"""Fetch the corpus index from a GitHub release when the server starts.

The index is built on a laptop, where the review files are, and is too large
for the repository. A server with no disk of its own (the free development
server) would otherwise never have it, and one with a disk would need it copied
in by hand after every rebuild. Instead each rebuild is published as a release
of the private repository, and a server whose copy is older fetches it.

Set AI_PEER_REVIEWER_CORPUS to the release tag and AI_PEER_REVIEWER_CORPUS_TOKEN
to a GitHub token that can read the repository. Unset, nothing happens.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import sys
import threading
import urllib.error
import urllib.request

from .corpus_index import DB_PATH, INDEX_DIR

TAG = "AI_PEER_REVIEWER_CORPUS"
TOKEN = "AI_PEER_REVIEWER_CORPUS_TOKEN"
REPO = "AI_PEER_REVIEWER_CORPUS_REPO"
DEFAULT_REPO = "jhyeonjung97/PeerNa"

#: Written last, so a download cut off halfway is fetched again next start.
MARKER = INDEX_DIR / "BUNDLE"

#: What a bundle holds, each gzipped. The database is the slim one: titles,
#: abstracts and comments, without the raw review files they were parsed from.
FILES = ("corpus.sqlite", "vectors.npy", "ids.json",
         "comment_vectors.npy", "comment_ids.json")


def current() -> str:
    return MARKER.read_text().strip() if MARKER.is_file() else ""


def _request(url: str, token: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "User-Agent": "PeerNa",
    })


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _download(url: str, token: str):
    """Open a release asset. GitHub answers with a redirect to signed storage,
    which refuses a request that still carries the GitHub token, and urllib
    would forward it; so the redirect is followed here, without the header."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(_request(url, token, "application/octet-stream"), timeout=600)
    except urllib.error.HTTPError as error:
        if error.code not in (301, 302, 303, 307, 308):
            raise
        location = error.headers["Location"]
    return urllib.request.urlopen(
        urllib.request.Request(location, headers={"User-Agent": "PeerNa"}), timeout=600)


def is_master() -> bool:
    """True where the index was built: the database still holds the review
    files, which a bundle leaves out. That copy is never overwritten."""
    if not DB_PATH.is_file():
        return False
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return bool(connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'review_text'").fetchone())
    finally:
        connection.close()


def fetch(tag: str, token: str, repo: str = DEFAULT_REPO) -> bool:
    """Download and unpack release `tag` into the index directory.

    Returns True if the index is now at `tag`. Files are unpacked beside their
    final names and moved into place at the end, so a search running meanwhile
    reads the old index or the new one, never half of one.
    """
    if current() == tag:
        return True
    api = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    with urllib.request.urlopen(_request(api, token, "application/vnd.github+json"),
                                timeout=60) as reply:
        assets = {a["name"]: a["url"] for a in json.load(reply)["assets"]}
    missing = [f for f in FILES if f + ".gz" not in assets]
    if missing:
        raise RuntimeError(f"Release {tag} lacks {', '.join(missing)}")

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        partial = INDEX_DIR / (name + ".partial")
        with _download(assets[name + ".gz"], token) as reply, \
                gzip.GzipFile(fileobj=reply) as unpacked, open(partial, "wb") as out:
            shutil.copyfileobj(unpacked, out, 1 << 20)
    # Swapped in together once all are down: vectors from one build beside ids
    # from another would point every search at the wrong comments.
    for name in FILES:
        os.replace(INDEX_DIR / (name + ".partial"), INDEX_DIR / name)
    MARKER.write_text(tag)
    return True


def start() -> None:
    """Fetch in the background if configured; reviews run without it meanwhile."""
    tag, token = os.environ.get(TAG, ""), os.environ.get(TOKEN, "")
    if not tag or not token or current() == tag or is_master():
        return
    repo = os.environ.get(REPO, DEFAULT_REPO)

    def run() -> None:
        try:
            fetch(tag, token, repo)
            from . import corpus_index
            corpus_index._comment_index = None
            print(f"Corpus index {tag} in place.", file=sys.stderr, flush=True)
        except Exception as error:
            print(f"Corpus index {tag} not fetched: {error}", file=sys.stderr, flush=True)

    threading.Thread(target=run, name="corpus-sync", daemon=True).start()
