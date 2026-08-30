"""An index of what reviewers asked of papers like this one.

The idea is to stop guessing at field-specific standards and read them off real
reports instead. Given a manuscript, find the corpus papers closest to it and
look at what their referees actually demanded — ICP quantification, scan-rate
dependence, an undoped control, whatever that corner of the literature expects.

Similarity is done with embeddings rather than by sorting papers into fields.
Fields do not have edges: a paper on Re-doped RuO2 for acidic OER is close to
both "ruthenium oxide stability" and "acidic water electrolysis" work, and any
taxonomy would have to pick one. Nearest neighbours do not have to.

There is no vector database here on purpose. Fifty thousand abstracts is a
300 MB array, and one matrix multiply searches all of it in under a tenth of a
second; a server would add operational weight and no speed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

INDEX_DIR = Path.home() / ".cache" / "ai-peer-reviewer" / "index"
DB_PATH = INDEX_DIR / "corpus.sqlite"
VECTORS_PATH = INDEX_DIR / "vectors.npy"
IDS_PATH = INDEX_DIR / "ids.json"

EMBED_MODEL = "text-embedding-3-small"

#: A peer review file interleaves each reviewer comment with the authors' reply
#: to it. Splitting on the reviewer heading alone leaves the reply attached to
#: the comment — measured at a quarter of everything parsed — which would feed
#: the index sentences describing what the authors *did* as though they were
#: what a referee *asked for*. Cut at the first reply marker.
#: A page number often survives between the comment and the reply, so an
#: optional bare number is allowed before the marker.
REPLY_MARKER = re.compile(
    r"(?i)(?:(?<=[.!?])|(?<=\n)|^)\s*(?:\d{1,3}\s+)?"
    r"(?:Response|Reply|Authors?'?\s+response|Our\s+response|Answer)"
    r"\s*[:.\-–]\s"
)

#: Referee reports carry the reviewers' comments and the authors' replies in one
#: file. Only the former is wanted, and it is introduced by a fixed heading.
REVIEWER_HEAD = re.compile(r"Reviewer\s*#?\s*(\d)\s*\(Remarks to the Author\)\s*:?", re.I)
NUMBERED = re.compile(r"(?m)^\s*\(?(\d{1,2})[.)]\s+(?=\S)")
LICENSE = re.compile(r"Open Access This file.*?creativecommons\.org/licenses/by/4\.0/\.", re.S)


@dataclass
class Neighbour:
    slug: str
    title: str
    similarity: float
    comments: list[str]


# ---------------------------------------------------------------- building


def schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            slug TEXT PRIMARY KEY,
            title TEXT,
            abstract TEXT
        );
        CREATE TABLE IF NOT EXISTS comments (
            slug TEXT,
            reviewer INTEGER,
            position INTEGER,
            text TEXT
        );
        CREATE INDEX IF NOT EXISTS comments_slug ON comments(slug);
        """
    )


def parse_review(pdf_text: str) -> list[tuple[int, list[str]]]:
    """Pull each reviewer's numbered comments out of a peer review file."""
    text = LICENSE.sub("", pdf_text)
    heads = list(REVIEWER_HEAD.finditer(text))
    out: list[tuple[int, list[str]]] = []
    for index, head in enumerate(heads):
        stop = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        body = text[head.end():stop]
        marks = list(NUMBERED.finditer(body))
        if not marks:
            continue
        items = []
        for i, mark in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
            raw = body[mark.end():end]
            cut = REPLY_MARKER.search(raw)
            if cut:
                raw = raw[:cut.start()]
            item = " ".join(raw.split())
            if 15 < len(item) < 4000:
                items.append(item)
        if items:
            out.append((int(head.group(1)), items))
    return out


def first_page_abstract(pdf_path: Path) -> tuple[str, str]:
    """Title and abstract, read off the article's first page."""
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        text = doc[0].get_text()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    title = ""
    for line in lines[:25]:
        if len(line) > 25 and not line.lower().startswith(("http", "doi", "received")):
            title = line
            break
    body = " ".join(lines)
    match = re.search(r"(?i)\babstract\b[:\s]*(.{200,2500}?)(?=\bIntroduction\b|$)", body)
    abstract = match.group(1) if match else body[:1500]
    return title, " ".join(abstract.split())


# ---------------------------------------------------------------- searching


#: Word counts to draw examples at. The corpus median is 42 words and half of
#: every real report sits under that, so the examples have to be dominated by
#: short ones — a sample drawn uniformly would be all long comments and would
#: teach the wrong lesson.
EXAMPLE_BANDS = ((6, 20, 4), (21, 40, 4), (41, 80, 2))

#: A numbered item in a review file is not always a reviewer comment. Reference
#: lists get numbered too, and so do the authors' own change logs; both survive
#: the reply-marker cut because nothing about them looks like a reply. They are
#: harmless in a 46,000-comment index and poisonous as an example, so examples
#: are filtered where the index is not.
BIBLIOGRAPHIC = re.compile(
    r"(et al\.|\b[A-Z]\.\s?[A-Z]?\.\s|\bvol\b)"          # author-initial style
    r".*\b\d{2,4},?\s?\d{1,5}\s?[-–]\s?\d{1,5}\b"        # volume, page range
    r"|\(\d{4}\)\.?\s*$",                                 # or simply ends in (year).
    re.S,
)
AUTHOR_VOICE = re.compile(r"(?i)\b(reviewer\s*#?\d|we have|we added|as suggested|corrected)\b")
REFEREE_VOICE = re.compile(
    r"(?i)\b(should|must|please|why|how|unclear|not clear|missing|lacking|provide|"
    r"explain|justify|clarify|add|report|suggest|recommend|would be|appears?|seems?|"
    r"the authors?|it is)\b"
)


_examples: list[str] = []


def example_comments() -> list[str]:
    """Real referee comments, short ones over-represented, for use as examples.

    Fixed sample rather than a fresh draw per run: the examples sit near the
    front of a prompt that is otherwise identical between runs, and changing them
    would throw away the prompt cache for the sake of variety nobody sees.

    Only a successful read is remembered. A harvest holds the write lock for
    long stretches, and one unlucky attempt at start-up should not leave the rest
    of the process without examples.
    """
    global _examples

    if _examples:
        return _examples
    if not DB_PATH.is_file():
        return []
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
    try:
        found: list[str] = []
        for low, high, wanted in EXAMPLE_BANDS:
            rows = connection.execute(
                # Ordered by the tail of the text rather than at random, so the
                # same corpus always yields the same examples and the prompt
                # cache survives between runs. Harvesting more papers does move
                # the sample; that is a rebuild, not a per-run change.
                # LIMIT is generous because most rows fail the filters below.
                """SELECT text FROM comments
                   WHERE length(text) BETWEEN ? AND ? AND text LIKE '%.'
                   ORDER BY substr(text, -3), rowid LIMIT 2000""",
                (low * 6, high * 7),
            ).fetchall()
            good = [
                text for (text,) in rows
                if low <= len(text.split()) <= high
                and REFEREE_VOICE.search(text)
                and not BIBLIOGRAPHIC.search(text)
                and not AUTHOR_VOICE.search(text)
            ]
            step = max(len(good) // wanted, 1)
            found.extend(good[::step][:wanted])
        _examples = found
        return found
    except sqlite3.OperationalError:
        return []  # a harvest has the database; the prompt does without
    finally:
        connection.close()


def _client():
    import openai

    return openai.OpenAI()


def embed(texts: list[str]) -> "list[list[float]]":
    """Embed in batches; the API caps how much one request may carry."""
    client = _client()
    out: list[list[float]] = []
    for start in range(0, len(texts), 96):
        chunk = [t[:8000] or " " for t in texts[start:start + 96]]
        reply = client.embeddings.create(model=EMBED_MODEL, input=chunk)
        out.extend(item.embedding for item in reply.data)
    return out


def load_vectors():
    import numpy as np

    if not VECTORS_PATH.is_file():
        return None, []
    return np.load(VECTORS_PATH), json.loads(IDS_PATH.read_text())


def similar(text: str, k: int = 5) -> list[Neighbour]:
    """The k corpus papers closest to this text, with their reviewer comments."""
    import numpy as np

    vectors, ids = load_vectors()
    if vectors is None or not ids:
        return []

    query = np.array(embed([text])[0], dtype="float32")
    query /= np.linalg.norm(query) or 1.0
    scores = vectors @ query  # rows are already unit length

    connection = sqlite3.connect(DB_PATH)
    try:
        found = []
        for position in np.argsort(-scores)[:k]:
            slug = ids[int(position)]
            row = connection.execute(
                "SELECT title FROM papers WHERE slug = ?", (slug,)
            ).fetchone()
            comments = [
                r[0] for r in connection.execute(
                    "SELECT text FROM comments WHERE slug = ? ORDER BY reviewer, position",
                    (slug,),
                )
            ]
            found.append(
                Neighbour(slug, row[0] if row else slug, float(scores[position]), comments)
            )
        return found
    finally:
        connection.close()
