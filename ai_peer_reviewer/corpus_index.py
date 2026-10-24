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
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

INDEX_DIR = Path.home() / ".cache" / "ai-peer-reviewer" / "index"
DB_PATH = INDEX_DIR / "corpus.sqlite"
VECTORS_PATH = INDEX_DIR / "vectors.npy"
IDS_PATH = INDEX_DIR / "ids.json"

#: One vector per referee comment, alongside the one per paper above. A paper's
#: nearest neighbours carry every comment their referees wrote — the XPS fit,
#: the stability test, the typo on page 3 — and only some of those are about
#: anything this manuscript does. Searching the comments themselves finds the
#: ones written about the same method or the same kind of claim.
COMMENT_VECTORS_PATH = INDEX_DIR / "comment_vectors.npy"
COMMENT_IDS_PATH = INDEX_DIR / "comment_ids.json"

EMBED_MODEL = "text-embedding-3-small"

#: Comment vectors are cut to 512 dimensions and stored as float16. At the full
#: 1536 in float32, 140,000 comments come to 860 MB, which does not fit beside
#: the database on the 2 GB deployed disk; at this size they are 143 MB. The
#: model is trained so a prefix of its vector stays a usable embedding.
COMMENT_DIMENSIONS = 512

#: A peer review file interleaves each reviewer comment with the authors' reply
#: to it. Splitting on the reviewer heading alone leaves the reply attached to
#: the comment — measured at a quarter of everything parsed — which would feed
#: the index sentences describing what the authors *did* as though they were
#: what a referee *asked for*. Cut at the first reply marker.
#: A page number often survives between the comment and the reply, so an
#: optional bare number is allowed before the marker.
REPLY_MARKER = re.compile(
    r"(?i)(?:(?<=[.!?])|(?<=\n)|^)\s*(?:\d{1,3}\s+)?"
    r"(?:Response|Reply|Authors?['’]?\s+(?:response|reply)|Our\s+(?:response|reply)|"
    r"Response\s+(?:from|of)\s+(?:the\s+)?authors?|Answer)"
    r"\s*[:.\-–]\s"
)

#: Referee reports carry the reviewers' comments and the authors' replies in one
#: file. Only the former is wanted, and it is introduced by a fixed heading.
REVIEWER_HEAD = re.compile(r"Reviewer\s*#?\s*(\d)\s*\(Remarks to the Author\)\s*:?", re.I)
NUMBERED = re.compile(r"(?m)^\s*\(?(\d{1,2})[.)]\s+(?=\S)")
LICENSE = re.compile(r"Open Access This (?:Peer Review )?[Ff]ile is licensed.*?creativecommons\.org/licenses/by/4\.0/?\.?", re.S)


@dataclass
class Neighbour:
    slug: str
    title: str
    similarity: float
    comments: list[str]


@dataclass
class Precedent:
    """One referee comment, from another paper, found for one part of this one."""

    slug: str
    title: str
    text: str
    similarity: float


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
            text TEXT,
            kind TEXT DEFAULT 'point',
            round INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS comments_slug ON comments(slug);
        """
    )
    # A database from before comments had a kind and a round gains the two
    # columns; every existing row reads as a first-round point, which is what
    # the old parser kept.
    have = {row[1] for row in connection.execute("PRAGMA table_info(comments)")}
    if "kind" not in have:
        connection.execute("ALTER TABLE comments ADD COLUMN kind TEXT DEFAULT 'point'")
    if "round" not in have:
        connection.execute("ALTER TABLE comments ADD COLUMN round INTEGER DEFAULT 1")


def has_kinds(connection: sqlite3.Connection) -> bool:
    """Whether this database records a kind per comment. A bundle published
    before the column existed does not, and a query naming it would fail."""
    return any(row[1] == "kind" for row in connection.execute("PRAGMA table_info(comments)"))


def points_only(connection: sqlite3.Connection) -> str:
    """The SQL condition that keeps a referee's specific points, or nothing
    where the database cannot tell points from summaries."""
    return " AND kind = 'point'" if has_kinds(connection) else ""


@dataclass
class Parsed:
    """One unit of a referee report: the opening assessment or one point."""

    reviewer: int
    round: int
    kind: str          # "summary" or "point"
    position: int
    text: str


#: Where the authors' side of the file begins inside a reviewer's section: the
#: rebuttal that follows the last report of a round, or a reply to one comment.
AUTHOR_SECTION = re.compile(
    r"(?im)^\s*(?:Response\s+to\s+(?:the\s+)?(?:reviewers?|referees?|comments)|"
    r"Point[- ]by[- ]point|Rebuttal\b|Dear\s+(?:editor|Dr\b|Prof)|"
    r"We\s+(?:would\s+like\s+to\s+)?thank\s+(?:all\s+)?(?:the\s+)?(?:three\s+|two\s+|four\s+)?"
    r"(?:reviewers?|referees?)|Reply\s+to\s+(?:the\s+)?reviewers?|Our\s+(?:response|reply))"
)

#: Heading a round of reports. The first round is often headed too, so a round
#: only begins at one that comes after a report.
ROUND_HEAD = re.compile(r"(?m)^\s*REVIEWERS?['’]?\s+COMMENTS\b")

#: A label standing alone at the end of a report's opening, before its list.
LIST_LABEL = re.compile(
    r"(?is)\s*(?:(?:major|minor|specific|general|detailed|other|additional)\s+)?"
    r"(?:comments?|concerns?|issues?|points?|questions?|remarks?)(?:\s+and\s+\w+)?\s*[:.]?\s*$"
)

#: A paragraph that gives the verdict, wherever in a prose report it falls.
VERDICT = re.compile(
    r"(?i)\b(?:recommend|publication|publishable|accept(?:ed|ance)?|reject|suitable\s+for|"
    r"addressed\s+(?:all\s+)?(?:my|the)\s+(?:comments|concerns))\b"
)

#: A table-of-contents line in a rebuttal: "Responses to Reviewer #1 ....... 2".
DOT_LEADER = re.compile(r"\.{6,}|…{3,}")

SHORTEST_UNIT = 15
LONGEST_UNIT = 4000


def store_comments(connection: sqlite3.Connection, slug: str, parsed: list[Parsed]) -> None:
    """Replace one paper's comments with these."""
    connection.execute("DELETE FROM comments WHERE slug = ?", (slug,))
    connection.executemany(
        "INSERT INTO comments (slug, reviewer, position, text, kind, round) "
        "VALUES (?,?,?,?,?,?)",
        [(slug, p.reviewer, p.position, p.text, p.kind, p.round) for p in parsed],
    )


def _paragraphs(text: str) -> list[str]:
    """Split extracted PDF text back into paragraphs.

    The text arrives one printed line per line, with no blank line between
    paragraphs. A paragraph ends on a line that finishes a sentence well short
    of the full measure, or before a line that opens a list item.
    """
    lines = [l.rstrip() for l in text.split("\n")]
    widths = sorted(len(l) for l in lines if len(l) > 20)
    measure = widths[len(widths) // 2] if widths else 80
    out: list[list[str]] = [[]]
    for line in lines:
        if not line.strip():
            if out[-1]:
                out.append([])
            continue
        if out[-1] and (re.match(r"\s*(?:[-•–]\s|\(?[a-z]\)\s|\(?[ivx]+\)\s)", line)
                        or AUTHOR_SECTION.match(line)):
            out.append([])
        out[-1].append(line)
        if line.endswith((".", "?", "!", ":", ".)")) and len(line) < 0.75 * measure:
            out.append([])
    return [" ".join(" ".join(p).split()) for p in out if p]


def _shingles(text: str, size: int = 8) -> set[str]:
    words = re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()
    return {" ".join(words[i:i + size]) for i in range(max(len(words) - size + 1, 1))}


def parse_review(pdf_text: str) -> list[Parsed]:
    """Each reviewer's report, as an opening assessment and separate points.

    A report is either numbered, in which case the text before the first
    number is its assessment and each number a point, or written as
    paragraphs, in which case the first paragraph and any that give the
    verdict are the assessment and the rest are points. Before this, only
    numbered points were kept: the assessment of every report was lost, and
    reports written as prose were lost whole, about half the long ones.

    The authors' rebuttal quotes each report again under the same heading.
    A section most of whose text has already been seen in this file is such a
    quotation and is skipped; one that is new is the next round's report.
    """
    text = LICENSE.sub("", pdf_text)
    heads = list(REVIEWER_HEAD.finditer(text))
    if not heads:
        return []
    round_marks = [m.start() for m in ROUND_HEAD.finditer(text) if m.start() > heads[0].start()]
    seen_shingles: set[str] = set()
    seen_keys: set[str] = set()
    reports_by_reviewer: dict[int, int] = {}
    out: list[Parsed] = []

    for index, head in enumerate(heads):
        stop = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        # Not cut at the start of the rebuttal: some files carry a report only
        # as the rebuttal quotes it, comment by comment between the replies.
        # The replies are dropped one unit at a time instead, below.
        body = text[head.end():stop]
        marks = list(NUMBERED.finditer(body))

        units: list[tuple[str, str]] = []
        if marks:
            opening = body[:marks[0].start()]
            for marker in (AUTHOR_SECTION, REPLY_MARKER):
                found = marker.search(opening)
                if found:
                    opening = opening[:found.start()]
            opening = LIST_LABEL.sub("", opening)
            if len(opening.split()) >= 25:
                units.append(("summary", opening))
            for i, mark in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
                raw = body[mark.end():end]
                found = REPLY_MARKER.search(raw)
                units.append(("point", raw[:found.start()] if found else raw))
        else:
            paragraphs = []
            for paragraph in _paragraphs(body):
                found = REPLY_MARKER.search(paragraph)
                if found:
                    paragraph = paragraph[:found.start()]
                if len(paragraph.split()) >= 4 and not AUTHOR_SECTION.match(paragraph) \
                        and not DOT_LEADER.search(paragraph):
                    paragraphs.append(paragraph)
            for i, paragraph in enumerate(paragraphs):
                verdict = i == 0 or (i == len(paragraphs) - 1 and VERDICT.search(paragraph))
                units.append(("summary" if verdict else "point", paragraph))

        # A unit is already seen when most of its wording is: the rebuttal
        # quotes each comment again, with the PDF's ligatures sometimes coming
        # out differently, so exact matching misses the copy.
        kept, repeated = [], 0
        for kind, raw in units:
            item = " ".join(unicodedata.normalize("NFKC", raw).split())
            if not SHORTEST_UNIT < len(item) < LONGEST_UNIT:
                continue
            shingles = _shingles(item)
            if comment_key(item) in seen_keys or \
                    len(shingles & seen_shingles) >= 0.5 * len(shingles):
                repeated += 1
                continue
            seen_shingles |= shingles
            seen_keys.add(comment_key(item))
            # The authors' replies interleaved with a quoted report are not
            # marked off; they are recognised by how they read.
            if is_referee_comment(item):
                kept.append((kind, item))
        # Mostly repeats: this section is the rebuttal quoting a report, not a
        # new report, and does not start a round.
        if not kept or repeated >= len(kept):
            continue

        reviewer = int(head.group(1))
        by_heading = 1 + sum(1 for m in round_marks if m < head.start())
        reports_by_reviewer[reviewer] = reports_by_reviewer.get(reviewer, 0) + 1
        # Headings mark some rounds and not others; a reviewer's next new report
        # is their next round either way.
        round_ = max(by_heading, reports_by_reviewer[reviewer])
        for position, (kind, item) in enumerate(kept, 1):
            out.append(Parsed(reviewer, round_, kind, position, item))
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


#: What a numbered item in a review file turns out to be. Measured on 400 drawn
#: at random from the corpus and labelled by a model, then checked by hand: two
#: thirds were a referee's comment, a fifth the authors' reply that the reply-
#: marker cut had missed, a tenth a bibliography entry. The rules below are
#: written to keep every referee comment and lose what they can of the rest: on
#: that sample they dropped one referee comment in 265, nine bibliography entries
#: in ten, and about half the replies. A reply that slips through costs a
#: precedent slot; a referee comment wrongly dropped costs a precedent.
AUTHOR_OPENING = re.compile(
    r"^\W*(?:\d+[.)]\s*)?(?:"
    r"(?:Response|Reply|Answer)(?!\s+to\s+(?:the\s+)?reviewer)\s*\d*\s*[:.)\-–]|A\d+\s*[:.)]|R\d+\s*[:.)]|"
    r"We (?:have|had|thank|appreciate|agree|apologi[sz]e|added|revised|now|are grateful)\b|"
    r"Thank(?:s| you)\b|As (?:suggested|requested|recommended|pointed out) by\b|"
    r"Following (?:the|your) (?:reviewer|suggestion|advice|comment|recommendation)|In response to\b|"
    r"According to (?:the|your) (?:reviewer|suggestion)|"
    r"(?:Added|Revised|Corrected|Changed|Modified|Rephrased|Amended)\b)",
    re.I,
)
AUTHOR_CHANGE = re.compile(
    r"\b(?:we (?:have|had) (?:now |also |further |additionally )?(?:added|revised|included|performed|"
    r"conducted|carried out|provided|modified|corrected|changed|clarified|rewritten|updated|measured|"
    r"calculated|rephrased|removed|deleted|amended)|we (?:added|revised|included|performed|conducted|"
    r"carried out|clarified|corrected|amended)|(?:has|have) been (?:added|revised|included|corrected|"
    r"amended|updated) (?:in|to) the (?:revised |main )?(?:manuscript|text|supplementary|SI)|"
    r"(?:thank|appreciate) the reviewer|the reviewer['’]s (?:comment|suggestion|concern))",
    re.I,
)
#: Referees write "the authors"; the authors write "we". A "we" with no word
#: addressed to the authors is the authors talking — except for the handful of
#: phrases referees, often writing in a second language, use to soften a point.
FIRST_PERSON = re.compile(r"\b(?:we|our|us|ours)\b", re.I)
ADDRESSED = re.compile(r"(?i)\b(?:the authors?|authors['’]|you|your)\b|\?")
REFEREE_WE = re.compile(
    r"(?i)\b(?:we (?:suggest|recommend|believe|think|wonder|would (?:like|suggest|recommend))|"
    r"in our (?:opinion|view))\b"
)
CITATION = re.compile(
    r"^\W*(?:\[?\d{1,3}[\].)]?\s*)?(?:"
    r"[A-Z][\w'’\-]+(?:\s[a-z]{1,3}\s[A-Z][\w'’\-]+)?,?\s+(?:[A-Z]\.\s?-?){1,3}(?:,|;|\s+et al|\s*&|\s+and)"
    r"|[A-Z][\w'’\-]+\s+[A-Z]{1,3},?\s+(?:et al|[A-Z][\w'’\-]+\s+[A-Z]{1,3}\b)"
    r"|(?:[A-Z]\.\s?-?){1,3}\s?[A-Z][\w'’\-]+,\s+(?:[A-Z]\.\s?){1,3}"
    r"|[A-Z]{1,2}\s[A-Z][\w'’\-]+,\s+[A-Z]{1,2}\s[A-Z]"
    r"|[A-Z][\w'’\-]+,\s+[A-Z]\.(?:\s?[A-Z]\.)?;"
    r")"
)
CITATION_TAIL = re.compile(r"\(\d{4}\)|\b\d{1,4},\s*\d{1,6}(?:[-–]\d{1,6})?\b|\bdoi\b|\b\d{4}\b")
ASKING = re.compile(r"(?i)\b(?:the authors?|please|should|could you|i (?:suggest|recommend|wonder)|it is unclear)\b")
#: A label a parser left on the front of a comment.
LEADING_LABEL = re.compile(r"^\W*(?:(?:Comment|Point|Question|Issue|Q|C)\s*\d*\s*[:.)]\s*|\d{1,2}[.)]\s+)", re.I)


def is_referee_comment(text: str) -> bool:
    """Whether a parsed item is a referee's comment, not a reply or a reference."""
    head = text[:500]
    # A PDF set in a font with no character map extracts as private-use code
    # points: unreadable, and dense enough to overrun the embedding limit.
    if sum(c.isascii() for c in head) < len(head) / 2:
        return False
    if CITATION.match(head) and CITATION_TAIL.search(head) and len(text) < 600 and not ASKING.search(text):
        return False
    if AUTHOR_OPENING.match(head) or AUTHOR_CHANGE.search(head):
        return False
    if FIRST_PERSON.search(head) and not ADDRESSED.search(head) and not REFEREE_WE.search(head):
        return False
    return True


def comment_key(text: str) -> str:
    """What two copies of one comment have in common: no label, case or spacing."""
    for _ in range(2):
        text = LEADING_LABEL.sub("", text)
    return " ".join(text.lower().split())[:300]


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
                "SELECT text FROM comments WHERE length(text) BETWEEN ? AND ? "
                "AND text LIKE '%.'" + points_only(connection) +
                " ORDER BY substr(text, -3), rowid LIMIT 2000",
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


def embed(texts: list[str], dimensions: int | None = None) -> "list[list[float]]":
    """Embed in batches; the API caps how much one request may carry."""
    client = _client()
    extra = {"dimensions": dimensions} if dimensions else {}
    out: list[list[float]] = []
    for start in range(0, len(texts), 96):
        chunk = [t[:8000] or " " for t in texts[start:start + 96]]
        for attempt in range(8):
            try:
                reply = client.embeddings.create(model=EMBED_MODEL, input=chunk, **extra)
                break
            except Exception as error:
                # A whole-corpus rebuild runs into the per-minute token cap;
                # waiting it out is cheaper than losing the run.
                if "maximum input length" in str(error) and attempt == 0:
                    # Characters are not tokens; text that is mostly symbols
                    # runs over at a length plain prose never reaches.
                    chunk = [t[:2000] for t in chunk]
                    continue
                if "rate_limit" not in str(error) or attempt == 7:
                    raise
                time.sleep(5 * (attempt + 1))
        out.extend(item.embedding for item in reply.data)
    return out


def _unit(rows):
    import numpy as np

    rows = np.asarray(rows, dtype="float32")
    return rows / (np.linalg.norm(rows, axis=-1, keepdims=True) + 1e-12)


def load_vectors():
    import numpy as np

    if not VECTORS_PATH.is_file():
        return None, []
    return np.load(VECTORS_PATH, mmap_mode="r"), json.loads(IDS_PATH.read_text())


def nearest_papers(text: str, k: int, exclude: str = "") -> list[tuple[str, float]]:
    """Slugs of the k papers whose title and abstract sit closest to this text."""
    import numpy as np

    vectors, ids = load_vectors()
    if vectors is None or not ids:
        return []
    scores = vectors @ _unit(embed([text])[0])  # rows are already unit length
    found = []
    for position in np.argsort(-scores):
        if ids[int(position)] == exclude:
            continue
        found.append((ids[int(position)], float(scores[position])))
        if len(found) == k:
            break
    return found


def similar(text: str, k: int = 5, exclude: str = "") -> list[Neighbour]:
    """The k corpus papers closest to this text, with their reviewer comments."""
    nearest = nearest_papers(text, k, exclude)
    if not nearest:
        return []

    connection = sqlite3.connect(DB_PATH)
    try:
        found = []
        for slug, score in nearest:
            row = connection.execute(
                "SELECT title FROM papers WHERE slug = ?", (slug,)
            ).fetchone()
            comments = [
                r[0] for r in connection.execute(
                    "SELECT text FROM comments WHERE slug = ?" + points_only(connection)
                    + " ORDER BY round, reviewer, position",
                    (slug,),
                )
            ]
            found.append(Neighbour(slug, row[0] if row else slug, score, comments))
        return found
    finally:
        connection.close()


_comment_index: tuple | None = None

SCORE_BLOCK = 50_000


def load_comment_vectors():
    """Comment vectors with the rowid and paper of each, or None if not built.

    Held for the life of the process: at 143 MB, reading it once per review
    would cost more than the search it serves.
    """
    global _comment_index
    import numpy as np

    if _comment_index is None and COMMENT_VECTORS_PATH.is_file():
        ids = json.loads(COMMENT_IDS_PATH.read_text())
        _comment_index = (
            # Mapped, not read: the server has 512 MB and this is most of it.
            np.load(COMMENT_VECTORS_PATH, mmap_mode="r"),
            np.array(ids["rowid"]),
            np.array(ids["slug"]),
        )
    return _comment_index


def has_comment_index() -> bool:
    return COMMENT_VECTORS_PATH.is_file()


def precedents(
    queries: list[str],
    papers: list[str] | None = None,
    k: int = 8,
    exclude: str = "",
) -> list[list[Precedent]]:
    """For each query, the k referee comments written closest to it.

    A query describes one thing a manuscript does — a method, a measurement, a
    claim — and the comments found are ones referees wrote about that kind of
    thing in other papers. `papers` limits the search to comments on those
    papers, which keeps a query like "stability test" from pulling in the
    stability of a protein when the manuscript is about a catalyst. A comment
    already found for an earlier query is not returned again, so two parts that
    overlap do not show the same precedent twice. Matching is on the text, not
    the row: a review file often repeats a comment — quoted again above the
    authors' reply, or carried into the second round — and parses to two rows.
    """
    import numpy as np

    index = load_comment_vectors()
    if index is None or not queries:
        return [[] for _ in queries]
    vectors, rowids, slugs = index

    allowed = slugs != exclude
    if papers is not None:
        allowed &= np.isin(slugs, list(papers))
    candidates = np.flatnonzero(allowed)
    if not candidates.size:
        return [[] for _ in queries]

    asked = _unit(embed(queries, dimensions=COMMENT_DIMENSIONS))
    # In blocks, so searching the whole corpus never holds a float32 copy of it.
    scores = np.concatenate([
        asked @ vectors[candidates[i:i + SCORE_BLOCK]].astype("float32").T
        for i in range(0, candidates.size, SCORE_BLOCK)
    ], axis=1)

    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
    try:
        titles = dict(connection.execute("SELECT slug, title FROM papers"))
        taken: set[str] = set()
        out: list[list[Precedent]] = []
        for row in scores:
            found: list[Precedent] = []
            for position in np.argsort(-row):
                rowid = int(rowids[candidates[position]])
                text = connection.execute(
                    "SELECT text FROM comments WHERE rowid = ?", (rowid,)
                ).fetchone()
                # Checked here as well as when the index is built, so an index
                # built before the filter existed still returns only referees.
                if not text or not is_referee_comment(text[0]):
                    continue
                key = comment_key(text[0])
                if key in taken:
                    continue
                taken.add(key)
                slug = str(slugs[candidates[position]])
                found.append(
                    Precedent(slug, titles.get(slug, slug), text[0], float(row[position]))
                )
                if len(found) == k:
                    break
            out.append(found)
        return out
    finally:
        connection.close()
