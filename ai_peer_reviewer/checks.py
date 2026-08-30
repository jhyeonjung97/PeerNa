"""Consistency checks a language model cannot be trusted with.

Three tests of PeerNa against a single plain prompt found no difference in what
the two turned up, with one exception that pointed the way out: of five planted
cross-reference errors and two mangled citation numbers, both systems caught
zero. Every one of them is a mechanical comparison across forty pages — is the
panel this sentence points at one the caption actually defines, is this citation
number inside the reference list — and that is the shape of question attention
is worst at and a regular expression is perfect at.

So these run in code and produce facts, not opinions. The reviewer is told what
was found; it does not get to decide whether Fig. 5j exists.

The bias throughout is towards silence. A referee report carrying a confident
complaint about a figure that is fine is worse than one that missed the error,
so every check here would rather say nothing than guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Nature-style captions: "Fig. 3 | Title. a First panel. b Second panel."
CAPTION = re.compile(r"(?m)^(Fig(?:ure)?\.?|Table)\s*(\d{1,2})\s*[|.]\s")

#: A mention in the body: "Fig. 4b", "Figs. 2a-e", "Table 1".
#:
#: The lookbehind is the whole game. "Supplementary Fig. 7" contains the string
#: "Fig. 7", and supplementary figures live in a different file, so without it
#: every reference to the SI is reported as a reference to a figure that does
#: not exist — which is where all 77 of this checker's first false positives
#: came from.
MENTION = re.compile(
    r"\b(Fig(?:ure|s|s\.)?\.?|Table)\s*(\d{1,2})\s*([a-z](?:\s*[,–—-]\s*[a-z])*)?(?![\w.])"
)

#: Supplementary references are blanked out before anything else is scanned.
#: A lookbehind is not enough: "Supplementary Figs. 9, 13, 15" carries the word
#: once and the numbers three times, and the line may break anywhere. Masking the
#: whole run — the word, the label and the enumeration that follows — is what
#: finally removed the last of the false positives, which had all been references
#: to a file that simply was not open.
SUPPLEMENTARY_RUN = re.compile(
    r"(?:Supplementary|Extended)\s+(?:Data\s+)?"
    r"(?:Figs?|Figures?|Tables?|Notes?|Movies?|Videos?|Methods?|Eqs?|Equations?)\.?"
    # An enumeration may change label part-way — "Supplementary Figs. 12-14 and
    # Table 1" — and the second half is just as supplementary as the first.
    r"(?:\s*(?:,|and)?\s*(?:Figs?\.?|Figures?|Tables?|Notes?|Eqs?\.?)?\s*"
    r"\d{1,3}[a-z]?(?:\s*[–—-]\s*[a-z\d]{1,3})?)*",
    re.I,
)

#: The same mention, but in the supplementary information, which can only be
#: checked when the author supplied that file too.
SUPPLEMENTARY = re.compile(
    r"Supplementary\s+(Fig(?:ure|s|s\.)?\.?|Table)\s*(\d{1,2})\s*"
    r"([a-z](?:\s*[,–—-]\s*[a-z])*)?(?![\w.])"
)

#: Citation markers are superscripts. Reading them off the flattened text and
#: requiring a letter in front nearly works, but PDFs lose spaces often enough
#: that "heated at 120 °C" arrives as "at120 °C" and becomes a citation of
#: reference 120 — which is how a published paper got accused of citing a
#: reference ninety past the end of its list. So they are read off the font size
#: instead: a superscript is set smaller than the body, and no amount of lost
#: whitespace changes that.
SUPERSCRIPT_RATIO = 0.86

#: How far above the baseline a span must sit, as a fraction of the body font
#: size, before it counts as raised rather than merely small.
RAISED_BY = 0.15

#: Within a superscript span, the numbers and the ranges between them.
RUN = re.compile(r"\d{1,3}(?:\s*[,–—-]\s*\d{1,3})*")

#: The start of the reference list, and a numbered entry within it.
REFERENCE_HEAD = re.compile(r"(?m)^\s*References\s*$")
REFERENCE_ENTRY = re.compile(r"(?m)^\s*(\d{1,3})\.\s*$|(?m)^\s*(\d{1,3})\.\s+(?=[A-Z])")

#: Captions inside the supplementary file, which are numbered in their own
#: series and carry the word in front: "Supplementary Fig. 14 | Schematic…".
SUPP_CAPTION = re.compile(
    r"(?mi)^\s*Supplementary\s+(Fig(?:ure)?s?\.?|Table)\s*(\d{1,3})\s*[|.]\s"
)

#: How far past the caption line to look for panel letters. Long enough for a
#: full Nature caption, short enough not to run into the body text after it.
CAPTION_WINDOW = 2600

#: Two-column typesetting hyphenates across line breaks, so "Supplementary"
#: arrives as "Supple-\nmentary" and no pattern matching the whole word finds
#: it. Rejoining these was worth more than every other fix here combined: it
#: took the false positives on three published papers from nineteen to a
#: handful, because almost every one had been a supplementary reference whose
#: label had been broken in half.
HYPHENATED = re.compile(r"(\w)[-‐‑]\n(\w)")


@dataclass(frozen=True)
class Finding:
    kind: str
    detail: str


def _panels(block: str) -> set[str]:
    """Panel letters a caption defines.

    Single letters are picked out and then held to the one thing that is always
    true of panel labels and never of stray words: they run a, b, c… without a
    gap. Taking only the unbroken run from 'a' throws away the "a" of ordinary
    English, which is what makes this usable at all.
    """
    seen = {m.group(1) for m in re.finditer(r"(?<![\w])([a-z])(?![\w])", block)}
    # Captions label consecutive panels as a range — "a–c SEM images of…" — and
    # then b and c never appear on their own. Without expanding these the run
    # stops at 'a' and every later panel the text mentions is reported as one
    # the caption does not define, which was two thirds of this check's false
    # positives across forty published papers.
    for low, high in re.findall(r"(?<![\w])([a-z])\s*[–—-]\s*([a-z])(?![\w])", block):
        if low < high:
            seen.update(chr(c) for c in range(ord(low), ord(high) + 1))
    run = set()
    for index in range(26):
        letter = chr(ord("a") + index)
        if letter not in seen:
            break
        run.add(letter)
    return run


#: Where a sentence ends. Abbreviations that end in a period are the hazard —
#: "Fig. 5g shows" must not be cut in half — so a following capital is required
#: and the common abbreviations are excluded by the lookbehind.
SENTENCE_END = re.compile(r"(?<![A-Z])(?<!Fig)(?<!Figs)(?<!Eq)(?<!ref)(?<!vs)\.\s+(?=[A-Z(])")


def _caption_segments(block: str) -> dict[str, str]:
    """What each panel's caption says that panel shows.

    Nature captions run the panels together — "a XRD patterns of… b EPR spectra
    of…" — so each panel's description is the text between its own label and the
    next one.
    """
    marks = [
        m for m in re.finditer(r"(?<![\w])([a-z])(?:\s*[–—-]\s*([a-z]))?(?=\s+[A-Za-z])", block)
    ]
    segments: dict[str, str] = {}
    for index, mark in enumerate(marks):
        stop = marks[index + 1].start() if index + 1 < len(marks) else len(block)
        body = " ".join(block[mark.end():stop].split())[:220]
        if not body:
            continue
        low, high = mark.group(1), mark.group(2) or mark.group(1)
        if low > high:
            continue
        for code in range(ord(low), ord(high) + 1):
            segments.setdefault(chr(code), body)
    return segments


@dataclass(frozen=True)
class Claim:
    """A sentence, and what the caption says the panel it points at contains."""

    target: str
    sentence: str
    caption: str


def claims(pdf_path: Path, limit: int = 120) -> list[Claim]:
    """Every cross-reference paired with the caption of the panel it names.

    A reference to a panel that exists but is the wrong one cannot be caught by
    comparing strings — both point at something real. It can be caught by reading
    what the sentence says the panel shows against what the caption says it
    shows, which is a judgement, but a very small one: two lines of text and one
    question. Enumerating the pairs is the part that has to be mechanical, since
    a paper carries scores of them and nobody checks them all by hand.
    """
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        text = "\n".join(page.get_text() for page in doc)
    text = HYPHENATED.sub(r"\1\2", text)
    body, _ = _split(text)

    marks = list(CAPTION.finditer(body))
    captions: dict[tuple[str, int], dict[str, str]] = {}
    for index, mark in enumerate(marks):
        kind = "Table" if mark.group(1).startswith("Table") else "Fig"
        stop = min(
            marks[index + 1].start() if index + 1 < len(marks) else len(body),
            mark.end() + CAPTION_WINDOW,
        )
        key = (kind, int(mark.group(2)))
        captions.setdefault(key, {}).update(_caption_segments(body[mark.end():stop]))

    scan = SUPPLEMENTARY_RUN.sub(lambda m: " " * len(m.group(0)), body)
    # Caption text is not prose about the figure; pairing a caption with itself
    # would ask whether it agrees with itself.
    spans = [(m.start(), m.end() + CAPTION_WINDOW) for m in marks]

    found: list[Claim] = []
    seen: set[tuple[str, str]] = set()
    for mark in MENTION.finditer(scan):
        if any(start <= mark.start() < stop for start, stop in spans):
            continue
        kind = "Table" if mark.group(1).startswith("Table") else "Fig"
        number = int(mark.group(2))
        letters = sorted(set(re.findall(r"[a-z]", mark.group(3) or "")))
        panels = captions.get((kind, number)) or {}
        described = [panels[l] for l in letters if l in panels]
        if not described:
            continue

        left = max((m.end() for m in SENTENCE_END.finditer(scan, 0, mark.start())), default=None)
        start = left if left is not None else max(0, mark.start() - 320)
        stop_match = SENTENCE_END.search(scan, mark.end())
        sentence = " ".join(scan[start:stop_match.start() + 1 if stop_match else mark.end() + 200].split())

        target = f"{kind}. {number}{''.join(letters)}"
        key = (target, sentence[:80])
        if key in seen or len(sentence) < 40:
            continue
        seen.add(key)
        found.append(Claim(target, sentence[:400], " / ".join(dict.fromkeys(described))[:300]))
        if len(found) >= limit:
            break
    return found


def _split(text: str) -> tuple[str, str]:
    """Body and reference list, split at the References heading."""
    heads = list(REFERENCE_HEAD.finditer(text))
    if not heads:
        return text, ""
    cut = heads[-1].start()
    return text[:cut], text[cut:]


def _captions(body: str) -> dict[tuple[str, int], set[str]]:
    found: dict[tuple[str, int], set[str]] = {}
    marks = list(CAPTION.finditer(body))
    for index, mark in enumerate(marks):
        kind = "Table" if mark.group(1).startswith("Table") else "Fig"
        number = int(mark.group(2))
        stop = min(
            marks[index + 1].start() if index + 1 < len(marks) else len(body),
            mark.end() + CAPTION_WINDOW,
        )
        block = body[mark.end():stop]
        found[(kind, number)] = found.get((kind, number), set()) | _panels(block)
    return found


def _mentions(body: str) -> list[tuple[str, int, set[str]]]:
    out = []
    for mark in MENTION.finditer(body):
        kind = "Table" if mark.group(1).startswith("Table") else "Fig"
        letters = set(re.findall(r"[a-z]", mark.group(3) or ""))
        # "Fig. 2a-e" names a range; everything between the ends is meant too.
        span = mark.group(3) or ""
        if re.search(r"[–—-]", span) and len(letters) >= 2:
            low, high = min(letters), max(letters)
            letters = {chr(c) for c in range(ord(low), ord(high) + 1)}
        out.append((kind, int(mark.group(2)), letters))
    return out


def _reference_count(references: str) -> int:
    """How many entries the reference list has.

    Demanding an unbroken run from 1 was too brittle: an entry whose number ends
    up on its own line in some other shape than the regex expects opens a gap,
    the count stops there, and every citation past it is reported as beyond the
    end of the list — which is how a published paper was accused of citing six
    references it does not have. Allowing a tenth of the entries to go unparsed
    keeps the count honest without inventing findings.
    """
    numbers = {int(a or b) for a, b in REFERENCE_ENTRY.findall(references)}
    if not numbers:
        return 0
    total = 0
    for candidate in range(1, max(numbers) + 1):
        if len({n for n in numbers if n <= candidate}) >= candidate * 0.9:
            total = candidate
    return total


def _cited(doc) -> set[int]:
    """Reference numbers cited in the text, read off the superscript spans."""
    import statistics

    lines = [
        line
        for page in doc
        for block in page.get_text("dict")["blocks"] if block.get("type") == 0
        for line in block["lines"]
    ]
    spans = [span for line in lines for span in line["spans"]]
    if not spans:
        return set()
    body_size = statistics.median(span["size"] for span in spans)

    # Small text alone is not a citation, and two other things wear the same
    # disguise:
    #
    # Axis tick labels, panel letters and scale bars are set small too, and they
    # are what turned a figure's "123" into a citation of reference 123. What
    # separates a superscript from those is company — it sits on a line of body
    # text, so the line also carries full-sized spans, while a tick label has the
    # line to itself.
    #
    # Subscripts are the harder case, because "RuO2" and "durability7" are the
    # same shape: a word followed by small digits. Size cannot tell them apart
    # and neither can context; in a materials paper both are everywhere. What
    # does tell them apart is height. A superscript is raised above the baseline
    # and a subscript is dropped below it, and PyMuPDF reports where each span
    # sits, so the two are separated by the one property that actually differs.
    found: set[int] = set()
    for line in lines:
        full = [span for span in line["spans"] if span["size"] > body_size * SUPERSCRIPT_RATIO]
        if not full:
            continue
        baseline = statistics.median(span["origin"][1] for span in full)
        for span in line["spans"]:
            if span["size"] > body_size * SUPERSCRIPT_RATIO:
                continue
            # Smaller y is higher on the page. A raised span clears the baseline
            # by a real margin; anything level with it or below is a subscript,
            # a chemical formula or a crystallographic index.
            if span["origin"][1] > baseline - body_size * RAISED_BY:
                continue
            for mark in RUN.finditer(span["text"]):
                run = mark.group(0)
                parts = [int(p) for p in re.findall(r"\d{1,3}", run)]
                if re.search(r"[–—-]", run) and len(parts) == 2 and parts[0] < parts[1]:
                    found.update(range(parts[0], parts[1] + 1))
                else:
                    found.update(parts)
    return found


def _read(path: Path) -> str:
    import pymupdf

    with pymupdf.open(path) as doc:
        return HYPHENATED.sub(r"\1\2", "\n".join(page.get_text() for page in doc))


def _supplementary_captions(si_text: str) -> dict[tuple[str, int], set[str]]:
    found: dict[tuple[str, int], set[str]] = {}
    marks = list(SUPP_CAPTION.finditer(si_text))
    for index, mark in enumerate(marks):
        kind = "Table" if mark.group(1).startswith("Table") else "Fig"
        stop = min(
            marks[index + 1].start() if index + 1 < len(marks) else len(si_text),
            mark.end() + CAPTION_WINDOW,
        )
        key = (kind, int(mark.group(2)))
        found[key] = found.get(key, set()) | _panels(si_text[mark.end():stop])
    return found


def _supplementary_mentions(text: str) -> list[tuple[str, int, set[str]]]:
    out = []
    for mark in SUPPLEMENTARY.finditer(text):
        kind = "Table" if mark.group(1).startswith("Table") else "Fig"
        letters = set(re.findall(r"[a-z]", mark.group(3) or ""))
        span = mark.group(3) or ""
        if re.search(r"[–—-]", span) and len(letters) >= 2:
            letters = {chr(c) for c in range(ord(min(letters)), ord(max(letters)) + 1)}
        out.append((kind, int(mark.group(2)), letters))
    return out


# ------------------------------------------------------- the bibliography
#
# Everything above compares the manuscript with itself. This part leaves it, and
# it is the half of "citation checking" that actually holds up. Asking a model to
# verify citations gets two or three of them looked at and the rest asserted from
# memory, which is where models invent papers. A registry lookup cannot invent
# anything: the DOI is there or it is not.
#
# Two checks, and they have very different reach:
#
#   - **Existence, metadata and retraction** need only what Crossref returns for
#     every registered work. Measured on a real bibliography: 12 of 12 resolved.
#   - **Whether the citation says what the manuscript claims** needs the text,
#     and online that means the abstract. Nine of those 12 had one — Nature
#     titles largely do not deposit abstracts — and an abstract catches a
#     citation pointed at the wrong subject, not a misquoted number from a
#     figure.
#
# Only the first is done here. The second belongs to the literature pass, which
# has the manuscript in front of it and can weigh what it reads.

CROSSREF = "https://api.crossref.org/works"

#: Crossref asks for a contact address and gives faster, more reliable service
#: to requests that carry one.
CROSSREF_AGENT = "peerna/1.0 (https://github.com/; mailto:peerna@example.invalid)"

#: An entry begins at a line that starts with its number. Requiring the number
#: to sit alone on its line — which it does about half the time — found 23 of 46
#: references in one paper and 25 of 62 in another.
REFERENCE_SPLIT = re.compile(r"(?m)^\s*(?=\d{1,3}\.(?:\s|$))")

#: A DOI printed in the entry, which makes the lookup exact rather than a search.
DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")

#: How alike two titles must be before a search hit is treated as the same work.
#: Below this the entry is left alone: a wrong match would put a confident and
#: false accusation into a referee report, which is the one outcome worth
#: avoiding at any cost here.
TITLE_MATCH = 0.87


def reference_entries(pdf_path: Path) -> list[tuple[int, str]]:
    """The bibliography, one numbered entry at a time."""
    text = HYPHENATED.sub(r"\1\2", _read(pdf_path))
    _, references = _split(text)
    found: list[tuple[int, str]] = []
    for chunk in REFERENCE_SPLIT.split(references):
        head = re.match(r"\s*(\d{1,3})\.\s*(.*)", chunk, re.S)
        if not head:
            continue
        body = " ".join(head.group(2).split())
        if len(body.split()) >= 6:
            found.append((int(head.group(1)), body[:400]))
    return found


def _crossref(params: dict) -> dict | None:
    import json
    import urllib.parse
    import urllib.request

    url = f"{CROSSREF}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": CROSSREF_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except Exception:
        return None


def _resolve(entry: str) -> dict | None:
    """The registered work this entry refers to, or None if it cannot be pinned."""
    from difflib import SequenceMatcher

    doi = DOI.search(entry)
    if doi:
        found = _crossref({"filter": f"doi:{doi.group(0).rstrip('.')}", "rows": 1})
    else:
        found = _crossref({"query.bibliographic": entry, "rows": 1})
    items = (found or {}).get("message", {}).get("items", [])
    if not items:
        return None
    work = items[0]
    if doi:
        return work

    # A bibliographic search always returns its best guess, however poor. Without
    # a title close enough to be the same paper, this reports nothing at all —
    # "not found" from a fuzzy search is not evidence that a reference is fake.
    titles = work.get("title") or [""]
    quoted = entry.lower()
    if not any(
        SequenceMatcher(None, t.lower()[:120], quoted[: len(t) + 60]).ratio() > TITLE_MATCH
        or t.lower()[:60] in quoted
        for t in titles if t
    ):
        return None
    return work


def check_bibliography(pdf_path: Path, limit: int = 80) -> list[Finding]:
    """Look every reference up in Crossref: does it exist, and was it retracted?"""
    import time

    findings: list[Finding] = []
    for number, entry in reference_entries(pdf_path)[:limit]:
        work = _resolve(entry)
        time.sleep(0.2)  # Crossref asks for a gentle rate; nothing here is urgent
        if work is None:
            if DOI.search(entry):
                findings.append(Finding(
                    "reference_not_registered",
                    f"Reference {number} gives a DOI that Crossref does not "
                    f"recognise: {entry[:90]}",
                ))
            continue

        retractions = [
            update for update in (work.get("update-to") or []) + (work.get("updated-by") or [])
            if "retract" in (update.get("type") or "").lower()
        ]
        if retractions:
            findings.append(Finding(
                "reference_retracted",
                f"Reference {number} has been retracted: "
                f"{(work.get('title') or ['?'])[0][:90]}.",
            ))

        year = (work.get("issued", {}).get("date-parts") or [[None]])[0][0]
        printed = re.findall(r"\((\d{4})\)", entry)
        if year and printed and str(year) not in printed:
            findings.append(Finding(
                "reference_year",
                f"Reference {number} is dated {printed[-1]} but the registered "
                f"record says {year}: {(work.get('title') or ['?'])[0][:70]}.",
            ))
    return findings


@dataclass(frozen=True)
class Citation:
    """A sentence, the reference it cites, and what that reference is about."""

    number: int
    sentence: str
    title: str
    abstract: str


def _laid_out(doc) -> tuple[str, list[tuple[int, int]]]:
    """The text with every citation's position in it.

    Rebuilt line by line rather than taken from `get_text()`, because the
    citation numbers are superscripts and only the font metrics distinguish them
    from the subscripts in a chemical formula. Positions are needed so each
    citation can be tied to the sentence that makes it.
    """
    import statistics

    lines = [
        line
        for page in doc
        for block in page.get_text("dict")["blocks"] if block.get("type") == 0
        for line in block["lines"]
    ]
    spans = [span for line in lines for span in line["spans"]]
    if not spans:
        return "", []
    body_size = statistics.median(span["size"] for span in spans)

    text_parts: list[str] = []
    marks: list[tuple[int, int]] = []
    offset = 0
    for line in lines:
        full = [s for s in line["spans"] if s["size"] > body_size * SUPERSCRIPT_RATIO]
        baseline = statistics.median(s["origin"][1] for s in full) if full else None
        for span in line["spans"]:
            body = span["text"]
            raised = (
                full
                and span["size"] <= body_size * SUPERSCRIPT_RATIO
                and span["origin"][1] <= baseline - body_size * RAISED_BY
            )
            if raised:
                for mark in RUN.finditer(body):
                    numbers = [int(n) for n in re.findall(r"\d{1,3}", mark.group(0))]
                    if re.search(r"[–—-]", mark.group(0)) and len(numbers) == 2 and numbers[0] < numbers[1]:
                        numbers = list(range(numbers[0], numbers[1] + 1))
                    for number in numbers:
                        marks.append((number, offset))
            text_parts.append(body)
            offset += len(body)
        text_parts.append("\n")
        offset += 1
    return "".join(text_parts), marks


def citations(pdf_path: Path, limit: int = 120) -> list[Citation]:
    """Every citation paired with the abstract of the work it points at.

    The pairing is what has to be mechanical. A paper cites sixty works across a
    hundred sentences, and nobody — model or referee — checks them all by hand;
    a chat session looks at two or three and asserts the rest. Judging one pair
    is small enough to be done reliably, and there is no reason not to do it for
    every pair when the whole set costs one request.
    """
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        text, marks = _laid_out(doc)
    body, _ = _split(text)
    entries = dict(reference_entries(pdf_path))

    resolved: dict[int, dict] = {}
    found: list[Citation] = []
    seen: set[tuple[int, int]] = set()
    for number, position in marks:
        if position >= len(body) or number not in entries:
            continue
        left = max((m.end() for m in SENTENCE_END.finditer(body, 0, position)), default=None)
        start = left if left is not None else max(0, position - 300)
        stop = SENTENCE_END.search(body, position)
        sentence = " ".join(body[start:stop.start() + 1 if stop else position + 200].split())
        key = (number, hash(sentence[:60]) % 10**6)
        if key in seen or len(sentence.split()) < 8:
            continue
        seen.add(key)

        if number not in resolved:
            import time

            resolved[number] = _resolve(entries[number]) or {}
            time.sleep(0.2)
        work = resolved[number]
        abstract = re.sub(r"<[^>]+>", " ", work.get("abstract") or "")
        if not abstract.strip():
            continue  # nothing to check it against; silence beats a guess
        found.append(Citation(
            number, sentence[:400],
            (work.get("title") or ["?"])[0][:150],
            " ".join(abstract.split())[:900],
        ))
        if len(found) >= limit:
            break
    return found


def run(pdf_path: Path, supplementary: Path | None = None) -> list[Finding]:
    """Every inconsistency the text can be checked against itself for.

    With the supplementary file supplied, the same checks reach the half of the
    manuscript that usually carries the data. Without it, references into the
    supplement are masked out and go unchecked — there is nothing to check them
    against, and guessing would only produce noise.
    """
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        text = "\n".join(page.get_text() for page in doc)
        cited = _cited(doc)
    text = HYPHENATED.sub(r"\1\2", text)
    body, references = _split(text)
    # Everything the supplementary file owns is removed before the main text is
    # checked against itself. Padding with spaces rather than deleting keeps the
    # offsets, and so the caption windows, intact.
    # Captions are read before masking, never after. A caption can begin on the
    # same line that a body sentence ends on — "…(Supplementary\nFig. 1 | Synthesis
    # and characterization…" — and masking first blanks the caption marker along
    # with the reference, leaving the checker convinced that Fig. 1 has no caption.
    captions = _captions(body)
    body = SUPPLEMENTARY_RUN.sub(lambda m: " " * len(m.group(0)), body)
    findings: list[Finding] = []

    # --- references to things that are not there
    for kind, number, letters in _mentions(body):
        defined = captions.get((kind, number))
        if defined is None:
            if captions:
                findings.append(Finding(
                    "missing_figure",
                    f"The text refers to {'Table' if kind == 'Table' else 'Fig.'} {number}, but no such "
                    f"{'table' if kind == 'Table' else 'figure'} caption exists.",
                ))
            continue
        # Only complain about a panel letter when the caption labels panels at
        # all. A caption with no labels tells us nothing about what is in it.
        if defined and (stray := sorted(letters - defined)):
            findings.append(Finding(
                "wrong_panel",
                f"The text refers to {kind}. {number}{''.join(stray)}, but the "
                f"caption for {kind}. {number} only labels panels "
                f"{'–'.join([min(defined), max(defined)]) if len(defined) > 1 else min(defined)}.",
            ))

    # --- things that are there and never discussed
    mentioned = {(kind, number) for kind, number, _ in _mentions(body)}
    for kind, number in sorted(captions):
        if (kind, number) not in mentioned:
            findings.append(Finding(
                "undiscussed_figure",
                f"{kind}. {number} has a caption but is never referred to in the text.",
            ))

    # --- citations past the end of the reference list
    total = _reference_count(references)
    if total:
        beyond = sorted(n for n in cited if n > total)
        if beyond:
            findings.append(Finding(
                "citation_out_of_range",
                f"The text cites reference{'s' if len(beyond) > 1 else ''} "
                f"{', '.join(str(n) for n in beyond[:6])} but the list ends at {total}.",
            ))
        # Tried and dropped: reporting entries that are never cited. It fired on
        # all three published papers, at twelve, four and seven entries, and
        # there is no way to tell from here whether those papers really carry
        # uncited references or whether some citations are simply not being
        # detected. A check that cannot distinguish a finding from its own blind
        # spot has no business in a referee report.

    if supplementary is not None:
        findings += _check_supplementary(body, supplementary)

    return _dedupe(findings)


def _check_supplementary(body: str, path: Path) -> list[Finding]:
    """The same checks, across the boundary between the two files."""
    try:
        si_text = _read(path)
    except Exception:
        return []
    captions = _supplementary_captions(si_text)
    if not captions:
        return []

    findings: list[Finding] = []
    # References to the supplement are made from both files, and a figure the SI
    # discusses only among its own pages is still discussed.
    everywhere = _supplementary_mentions(body) + _supplementary_mentions(si_text)
    for kind, number, letters in _supplementary_mentions(body):
        defined = captions.get((kind, number))
        label = f"Supplementary {'Table' if kind == 'Table' else 'Fig.'} {number}"
        if defined is None:
            findings.append(Finding(
                "missing_supplementary",
                f"The manuscript refers to {label}, but the supplementary file "
                f"has no such caption.",
            ))
            continue
        if defined and (stray := sorted(letters - defined)):
            findings.append(Finding(
                "wrong_supplementary_panel",
                f"The manuscript refers to {label}{''.join(stray)}, but that "
                f"caption only labels panels "
                f"{'–'.join([min(defined), max(defined)]) if len(defined) > 1 else min(defined)}.",
            ))

    mentioned = {(kind, number) for kind, number, _ in everywhere}
    absent = [key for key in sorted(captions) if key not in mentioned]
    # One unreferenced supplementary figure is a slip worth mentioning; twenty is
    # this checker failing to read the reference style, and saying so would be
    # worse than saying nothing.
    if 0 < len(absent) <= 3:
        for kind, number in absent:
            findings.append(Finding(
                "undiscussed_supplementary",
                f"Supplementary {'Table' if kind == 'Table' else 'Fig.'} {number} "
                f"is never referred to, in either file.",
            ))
    return findings


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """One finding per distinct problem, however many times it was written."""
    return list(dict.fromkeys(findings))
