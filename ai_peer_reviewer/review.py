"""Orchestration: run the lenses over the manuscript, then consolidate.

This layer is provider-agnostic. It decides what to ask and in what order; the
backend decides how to ask it.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .backends import Backend, Usage
from .checkpoint import Checkpoint
from .loader import Manuscript
from .parts import as_text_only
from .passes import (
    CITATION_INSTRUCTION,
    CROSS_REFERENCE_INSTRUCTION,
    MECHANICAL_INSTRUCTION,
    VERDICT_INSTRUCTION,
    NO_SEARCH_CAVEAT,
    PARTS_INSTRUCTION,
    PARTWISE_PRECEDENT_INSTRUCTION,
    PASSES,
    PRECEDENT_INSTRUCTION,
    SYNTHESIS_INSTRUCTION,
    Pass,
    condense_instruction,
    system_prompt,
)

#: How many similar papers to draw precedent comments from, and how many of
#: their comments to carry. Enough to show what the subfield expects without
#: burying the manuscript under other people's reviews.
PRECEDENT_PAPERS = 6
PRECEDENT_COMMENTS = 120

#: The same, part by part. Ten parts at eight comments is eighty, under the
#: paper-by-paper budget, and every one was found for a reason. The topic set
#: is wide on purpose: it only has to rule out other fields, and a method's
#: best precedent is often in a paper about a different material.
PRECEDENT_PARTS = 10
PRECEDENT_PER_PART = 8
PRECEDENT_TOPIC_PAPERS = 150
from .schema import Mismatches, Parts, RefereeReport, Verdict

#: Said on the command line when the literature pass runs without search. The
#: report leaves it out: its foot already says "Web search: off".
SEARCH_OFF_WARNING = (
    "Web search was disabled: no citation was verified, and claims about prior "
    "work were suppressed rather than guessed."
)

#: How many lenses to run at once. The passes are independent — only the
#: consolidation needs all of them — so running them in sequence wastes most of
#: the wall clock.
#:
#: Two is the measured optimum, and raising it makes things worse rather than
#: better. On a 27-page manuscript: sequential 9m03s, two at a time 5m54s, three
#: at a time 7m00s. Three loses because prompt caching needs a prefix to already
#: be cached — fire three requests together and all three start cold, so the
#: manuscript is billed and processed in full for each. Cache reuse fell from
#: 87k tokens to 21k, cost rose from $0.07 to $0.10, and every pass got slower.
#: Rate limits were never the constraint; the cache was.
CONCURRENCY = 2

#: How hard to try at shortening, and when to stop trying.
#:
#: The pass is noisy. Run identically five times on the same draft it returned
#: median comment lengths of 44, 44, 44, 60 and 72 words — every one an
#: improvement on the 84-word draft, but a spread wider than the difference
#: between any two versions of the instruction, which is what made three rounds
#: of prompt tuning here unreadable. Read alone, each of those rounds looked like
#: a regression it almost certainly was not.
#:
#: Sampling and keeping the shortest was the obvious answer and did not work:
#: best-of-three came back at 60, 42 and 51 against single draws of 44 to 72, for
#: three times the cost. The draws inside one call are evidently correlated, so
#: extra ones buy little. Two attempts is what the evidence supports — enough to
#: recover from a wrong comment count, which does happen, without paying for
#: draws that do not help.
CONDENSE_ATTEMPTS = 2

#: Effort for the cutting pass. Measured on one draft (15 comments, median 89
#: words), two runs each: high effort with a retry whenever the median stayed
#: above the target took 70 s and 62 s and left medians of 77 and 70; medium,
#: once, took 14 s and 15 s and left 63 and 61. Rewriting to a length is not a
#: reasoning problem, and the extra thought went into keeping words.
CONDENSE_EFFORT = "medium"

#: Effort for settling the recommendation. One question about a finished list
#: of comments, with a code check behind it that demotes an unsupported major;
#: high spent thinking tokens on it for no measured gain (2026-10-24).
VERDICT_EFFORT = "medium"

#: What a review does after the lenses, in order. Named here rather than counted
#: at each call site: the count was written out in two places and they had
#: drifted apart, so the progress bar in the browser stopped short of the end of
#: every run. Adding a stage now means adding it here and nowhere else.
LATER_STAGES = (
    "Checking consistency",
    "Field expectations",
    "Consolidating",
    "Cutting it down",
    "Deciding",
)


#: The order lenses start in, which is not the order they are reported in. The
#: literature lens searches the web and is the slowest; started last it was the
#: one everything else waited for, so it takes the second slot from the start.
#: The claims lens keeps the first: it is text only, and its finishing is what
#: lets the precedent pass start on a warm cache.
START_ORDER = ("claims", "literature", "methods", "figures")


def _start_order(review_pass) -> int:
    key = review_pass.key
    return START_ORDER.index(key) if key in START_ORDER else len(START_ORDER)


def total_steps() -> int:
    return len(PASSES) + len(LATER_STAGES)


def _median_words(report: RefereeReport) -> float:
    lengths = sorted(len(c.text.split()) for c in report.comments)
    if not lengths:
        return 0.0
    middle = len(lengths) // 2
    if len(lengths) % 2:
        return float(lengths[middle])
    return (lengths[middle - 1] + lengths[middle]) / 2


@dataclass
class ReviewResult:
    report: RefereeReport
    pass_notes: dict[str, str]
    usage: Usage
    warnings: list[str] = field(default_factory=list)
    #: For the line at the foot of the report: when it was made, and whether
    #: the literature pass could search. Left None by anything that builds a
    #: result by hand, and the line then leaves them out.
    web_search: bool | None = None
    finished_at: float | None = None
    #: Wall-clock seconds per stage, and per lens. Lenses overlap, so theirs do
    #: not add up to the "Lenses" figure, which is the time the group took.
    timings: dict[str, float] = field(default_factory=dict)


class Reviewer:
    def __init__(
        self,
        backend: Backend,
        mode: str,
        use_web_search: bool = True,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        self.backend = backend
        self.mode = mode
        self.use_web_search = use_web_search
        self.checkpoint = checkpoint
        self.notes: dict[str, str] = {}
        #: A corpus paper to keep out of the precedent search. Only testing sets
        #: it: a manuscript that is in the corpus would otherwise be handed its
        #: own referees' comments as precedent.
        self.exclude = ""
        #: The supplementary PDF, when the author supplied one. Only the
        #: consistency checks read it.
        self.supplementary: "Path | None" = None
        #: Passes that came from disk rather than the API, for the run summary.
        self.reused: list[str] = []
        self._lock = threading.Lock()

    @property
    def system(self) -> str:
        return system_prompt(self.mode)

    def run_pass(self, manuscript: Manuscript, review_pass: Pass) -> str:
        if self.checkpoint:
            saved = self.checkpoint.load(review_pass.key)
            if saved:
                self.notes[review_pass.key] = saved
                self.reused.append(review_pass.title)
                return saved

        web_search = review_pass.web_search and self.use_web_search
        instruction = review_pass.instruction
        if review_pass.web_search and not self.use_web_search:
            # Without search this pass cannot verify anything, so it is told not
            # to claim it has. Left unsaid, it asserts prior art from memory.
            instruction = f"{instruction}\n{NO_SEARCH_CAVEAT}"
            self.backend.note(SEARCH_OFF_WARNING)
        parts = (
            manuscript.parts
            if review_pass.needs_figures
            else as_text_only(manuscript.parts)
        )
        result = self.backend.run_text(
            self.system, parts, instruction, web_search, review_pass.effort,
            review_pass.title,
        )

        with self._lock:
            self.notes[review_pass.key] = result
            if self.checkpoint:
                # Saved the moment it returns, so a failure in a sibling pass
                # costs nothing already paid for.
                self.checkpoint.save(review_pass.key, result)
                spent = self.backend.take_usage()
                self.checkpoint.record_usage(
                    {
                        "input_tokens": spent.input_tokens,
                        "output_tokens": spent.output_tokens,
                        "cache_write_tokens": spent.cache_write_tokens,
                        "cache_read_tokens": spent.cache_read_tokens,
                    }
                )
        return result

    def precedent_pass(self, manuscript: Manuscript) -> str:
        """Check the manuscript against what referees expect of nearby work.

        Part by part when the comment index is built: break the manuscript into
        what it does, find what referees asked of each of those things in other
        papers, and check each against the place it appears here. Otherwise
        paper by paper, as before — the comments on the nearest papers, whole.
        Silently skipped when neither index exists — the review is still
        complete without it, just without the field-specific layer.
        """
        try:
            found = self.precedents_by_part(manuscript, self.exclude)
            if found is None:
                found = self.precedents_by_paper(manuscript, self.exclude)
        except Exception as exc:
            self.backend.warnings.append(
                f"Could not consult the corpus index ({type(exc).__name__}); "
                "the field-specific pass was skipped."
            )
            return ""
        if found is None:
            return ""
        instruction, material, slugs = found

        # Kept as DOI and title so the report can credit them. The referee
        # reports these came from are published under CC BY 4.0, which permits
        # exactly this use and asks for the source in return. The referees are
        # anonymous, so the source is the paper and the journal.
        titles = _titles(slugs)
        self.notes["precedent_sources"] = "\n".join(
            f"10.1038/{slug} — {titles.get(slug, '')[:110]}" for slug in slugs
        )
        return self.backend.run_text(
            self.system,
            as_text_only(manuscript.parts),
            f"{instruction}\n{material}",
            False,
            # Medium since the pipeline was written, never measured; raised
            # 2026-10-24 to match the breakdown that feeds it. It runs beside
            # the lenses, so it adds cost (about $0.03 on Sonnet) more than time.
            "high",
            "Field expectations",
        )

    def precedents_by_part(
        self, manuscript: Manuscript, exclude: str = ""
    ) -> "tuple[str, str, list[str]] | None":
        """What referees asked of each thing this manuscript does.

        Returns the instruction, the precedents grouped under the part they were
        found for, and the papers they came from; None when there is no comment
        index to search. `exclude` keeps a paper's own reports out, for testing
        on papers that are in the corpus.
        """
        from .corpus_index import has_comment_index, nearest_papers, precedents

        if not has_comment_index():
            return None
        breakdown = self.backend.run_structured(
            self.system, as_text_only(manuscript.parts), PARTS_INSTRUCTION, Parts
        )
        parts = breakdown.parts[:PRECEDENT_PARTS]
        if not parts:
            return None
        self.notes["parts"] = "\n".join(f"- {p.what} ({p.where})" for p in parts)

        # The topic narrows the search before the parts do. Without it a part
        # like "long-term stability test" matches the stability of a protein as
        # readily as that of a catalyst.
        nearby = nearest_papers(
            f"{breakdown.topic}\n{_leading_text(manuscript)}",
            PRECEDENT_TOPIC_PAPERS,
            exclude,
        )
        found = precedents(
            [p.what for p in parts],
            papers=[slug for slug, _ in nearby] or None,
            k=PRECEDENT_PER_PART,
            exclude=exclude,
        )
        lines, slugs = [], []
        for part, comments in zip(parts, found):
            lines.append(f"\n=== Part: {part.what} — in this manuscript at {part.where} ===")
            if not comments:
                lines.append("(no close precedent in the corpus)")
            for c in comments:
                lines.append(f"- {c.text[:600]}")
                if c.slug not in slugs:
                    slugs.append(c.slug)
        if not slugs:
            return None
        return PARTWISE_PRECEDENT_INSTRUCTION, "\n".join(lines), slugs

    def precedents_by_paper(
        self, manuscript: Manuscript, exclude: str = ""
    ) -> "tuple[str, str, list[str]] | None":
        """Every comment on the papers nearest this one, as reference material."""
        from .corpus_index import is_referee_comment, similar

        text = _leading_text(manuscript)
        if not text:
            return None
        neighbours = similar(text, k=PRECEDENT_PAPERS, exclude=exclude)
        if not neighbours:
            return None

        lines, budget = [], PRECEDENT_COMMENTS
        for n in neighbours:
            lines.append(f"\n=== From referee reports on: {n.title[:90]} ===")
            kept = [c for c in n.comments if is_referee_comment(c)]
            for c in kept[: max(1, budget // len(neighbours))]:
                lines.append(f"- {c[:600]}")
                budget -= 1
        return PRECEDENT_INSTRUCTION, "\n".join(lines), [n.slug for n in neighbours]

    def synthesise(self, manuscript: Manuscript) -> RefereeReport:
        joined = "\n\n".join(
            f"=== Pass: {p.title} ===\n{self.notes.get(p.key, '(no findings)')}"
            for p in PASSES
        )
        if self.notes.get("mechanical"):
            joined += ("\n\n=== Consistency check ===\n"
                       + MECHANICAL_INSTRUCTION + self.notes["mechanical"])
        if self.notes.get("precedent"):
            joined += (
                "\n\n=== Pass: What referees expect of papers like this ===\n"
                + self.notes["precedent"]
            )
        return self.backend.run_structured(
            self.system,
            manuscript.parts,
            f"{SYNTHESIS_INSTRUCTION}\n\n{joined}",
            RefereeReport,
        )

    def check_consistency(self, manuscript: Manuscript, announce=None) -> None:
        """Everything that can be verified rather than judged.

        Three kinds, cheapest first: the manuscript against itself, the
        bibliography against Crossref, and each cross-reference and citation
        against what it points at. The last two need a model, but only to answer
        one small question per pair — the enumerating, which is the part a review
        pass cannot do at this scale, is code.

        Every step is optional. A failure costs one housekeeping comment; an
        exception would cost the whole review, so none of them are allowed to
        raise.
        """
        if manuscript.path.suffix.lower() != ".pdf":
            return
        # Inside the guard like everything else here: a module that will not
        # import is one more check that did not run, not a failed review.
        try:
            from . import checks
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self.backend.note(f"Could not load the consistency checks ({exc}).")
            return

        # Stored as JSON rather than as the joined text, so that "ran and found
        # nothing" survives a resume. Saved as text it would be an empty string,
        # which the checkpoint treats as absent, and ninety seconds of network
        # calls would be repeated to reach the same silence.
        if self.checkpoint:
            saved = self.checkpoint.load("mechanical")
            if saved is not None:
                lines = json.loads(saved)
                if lines:
                    self.notes["mechanical"] = "\n".join(f"- {l}" for l in lines)
                self.reused.append("Consistency checks")
                return

        found: list[str] = []
        for label, said, work in (
            ("consistency checks", "Checking figures and numbering",
             lambda: checks.run(manuscript.path, self.supplementary)),
            ("bibliography lookup", "Looking up every reference",
             lambda: checks.check_bibliography(manuscript.path)),
        ):
            # Resolving a bibliography is one network call per reference and can
            # run past a minute. Saying which part is in flight is the difference
            # between a slow step and an apparent hang.
            if announce:
                announce(said)
            try:
                found += [f.detail for f in work()]
            except Exception as exc:  # noqa: BLE001 - see the docstring
                self.backend.note(f"Could not run the {label} ({exc}).")

        found += self._verify_pairs(manuscript, announce)
        found = list(dict.fromkeys(found))
        if self.checkpoint:
            self.checkpoint.save("mechanical", json.dumps(found))
        if found:
            self.notes["mechanical"] = "\n".join(f"- {line}" for line in found)

    def _verify_pairs(self, manuscript: Manuscript, announce=None) -> list[str]:
        """Check every cross-reference and citation against what it points at."""
        try:
            from . import checks
        except Exception as exc:  # noqa: BLE001 - see check_consistency
            self.backend.note(f"Could not load the pairing checks ({exc}).")
            return []

        out: list[str] = []
        jobs = (
            (
                "cross-references",
                "Checking cross-references",
                CROSS_REFERENCE_INSTRUCTION,
                lambda: checks.claims(manuscript.path),
                lambda c: f"[{c.target}]\n  sentence: {c.sentence}\n"
                          f"  caption of that panel: {c.caption}",
                "The text at {t} does not match what that panel's caption "
                "describes. {w}",
            ),
            (
                "citations",
                "Checking citations",
                CITATION_INSTRUCTION,
                lambda: checks.citations(manuscript.path),
                lambda c: f"[ref {c.number}] {c.title}\n  sentence: {c.sentence}\n"
                          f"  abstract: {c.abstract}",
                "Reference {t} may not support what it is cited for. {w}",
            ),
        )
        for label, said, instruction, gather, render, template in jobs:
            if announce:
                announce(said)
            try:
                pairs = gather()
                if not pairs:
                    continue
                result = self.backend.run_structured(
                    self.system,
                    [],
                    f"{instruction}\n\n" + "\n\n".join(render(p) for p in pairs),
                    Mismatches,
                )
                out += [
                    template.format(t=m.target, w=m.why.strip())
                    for m in result.mismatches
                ]
            except Exception as exc:  # noqa: BLE001 - see check_consistency
                self.backend.note(f"Could not verify {label} ({exc}).")
        return out

    def _resume_stage(self, key: str) -> "RefereeReport | None":
        if not self.checkpoint:
            return None
        saved = self.checkpoint.load(key)
        if not saved:
            return None
        try:
            return RefereeReport.model_validate_json(saved)
        except Exception:
            # A report written by an older schema is not worth migrating; the
            # stage simply runs again.
            return None

    def _save_stage(self, key: str, report: RefereeReport) -> None:
        if self.checkpoint:
            self.checkpoint.save(key, report.model_dump_json())

    def decide(self, report: RefereeReport) -> RefereeReport:
        """Settle the recommendation on its own, against the finished list.

        Sent without the manuscript. Re-reading the paper here would invite a
        fresh opinion, and the whole point is to judge the report that exists
        rather than to review the work again.

        A failure leaves the recommendation the synthesis gave, which is the
        current behaviour, so there is nothing to lose by trying.
        """
        listed = "\n".join(
            f"{index}. {comment.text.strip()}"
            for index, comment in enumerate(report.comments, start=1)
        )
        try:
            verdict = self.backend.run_structured(
                self.system,
                [],
                f"{VERDICT_INSTRUCTION}\n\n{report.general_comments.strip()}\n\n{listed}",
                Verdict,
                effort=VERDICT_EFFORT,
            )
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self.backend.note(f"Could not settle the recommendation ({exc}).")
            return report

        named = [n for n in verdict.escalating_comments if 1 <= n <= len(report.comments)]
        call = verdict.recommendation
        # The naming requirement is only real if it is enforced. Escalating with
        # nothing to point at is the failure this pass exists to stop, so it is
        # checked here rather than trusted to the instruction.
        if call in ("major_revision", "reject") and not named:
            self.backend.note(
                f"The verdict pass asked for {call.replace('_', ' ')} without naming "
                "a comment that requires it; recorded as minor revision."
            )
            call = "minor_revision"

        return report.model_copy(
            update={"recommendation": call, "confidence": verdict.confidence}
        )

    def condense(self, report: RefereeReport) -> RefereeReport:
        """Cut the drafted comments down without changing what they say.

        Sent without the manuscript attached. The pass has no business consulting
        the paper — anything it would find there would be a new point, which is
        exactly what it is forbidden to add — and leaving the pages out keeps it
        cheap and fast.

        A failure here loses nothing worth having, so it is swallowed: a verbose
        report is a far better outcome than no report.
        """
        drafted = report.model_dump_json(indent=2)
        expected = len(report.comments)
        instruction = condense_instruction()
        best: RefereeReport | None = None

        for attempt in range(CONDENSE_ATTEMPTS):
            reminder = (
                ""
                if best is not None or attempt == 0
                else f"\n\nYour previous attempt returned the wrong number of "
                     f"comments. Return exactly {expected}, in the original order."
            )
            try:
                shorter = self.backend.run_structured(
                    self.system,
                    [],
                    f"{instruction}{reminder}\n\n{drafted}",
                    RefereeReport,
                    effort=CONDENSE_EFFORT,
                )
            except Exception as exc:  # noqa: BLE001 - see the docstring
                self.backend.note(f"Could not shorten the comments ({exc}); kept the draft.")
                return best or report

            # Returning the wrong number of comments is the one failure that
            # matters and the one thing here that can be checked without
            # judgement, so it is checked rather than trusted to the prompt.
            if len(shorter.comments) != expected:
                continue

            # A second attempt is for a wrong count only. Retrying to get under
            # the length target cost another minute and, at high effort, came
            # back no shorter (see CONDENSE_EFFORT).
            best = shorter
            break

        if best is None:
            self.backend.note(
                "Shortening kept returning the wrong number of comments; kept the draft."
            )
            return report
        return best

    def review(self, manuscript: Manuscript, progress=None) -> ReviewResult:
        """Run the lenses concurrently, then consolidate.

        `progress` is called as progress(done, total, label). Because passes run
        together and finish out of order, the label names what is in flight right
        now rather than a single current step — a counter alone would suggest a
        sequence that is not happening.
        """
        total = total_steps()
        done = 0
        running: list[str] = []
        timings: dict[str, float] = {}
        started = time.monotonic()
        mark = [started]

        def lap(name: str) -> None:
            now = time.monotonic()
            timings[name] = round(now - mark[0], 1)
            mark[0] = now

        def announce() -> None:
            if not progress:
                return
            with self._lock:
                label = " · ".join(running) if running else "Finishing"
                finished = done
            progress(finished, total, label)

        # Set when the first text-only lens is done, so the manuscript's text is
        # in the provider's cache; the precedent pass, which reads the same
        # text, waits for it rather than for a lens slot.
        text_cached = threading.Event()
        first_text_lens = next((p.key for p in PASSES if not p.needs_figures), None)
        if first_text_lens is None:
            text_cached.set()  # nothing to wait for; never hang on it

        def one(review_pass: Pass) -> None:
            nonlocal done
            with self._lock:
                running.append(review_pass.title)
            announce()
            began = time.monotonic()
            try:
                self.run_pass(manuscript, review_pass)
            finally:
                timings[f"  {review_pass.title}"] = round(time.monotonic() - began, 1)
                if review_pass.key == first_text_lens:
                    text_cached.set()
                with self._lock:
                    if review_pass.title in running:
                        running.remove(review_pass.title)
                    done += 1
            announce()

        def consistency() -> None:
            nonlocal done
            with self._lock:
                running.append("Checking consistency")
            announce()
            began = time.monotonic()
            try:
                self.check_consistency(manuscript)
            finally:
                timings["Consistency checks"] = round(time.monotonic() - began, 1)
                with self._lock:
                    running.remove("Checking consistency")
                    done += 1
                announce()

        def precedents() -> None:
            nonlocal done
            text_cached.wait()
            with self._lock:
                running.append("Field expectations")
            announce()
            began = time.monotonic()
            try:
                precedent = self.checkpoint.load("precedent") if self.checkpoint else ""
                if not precedent:
                    precedent = self.precedent_pass(manuscript)
                    if precedent and self.checkpoint:
                        self.checkpoint.save("precedent", precedent)
                if precedent:
                    self.notes["precedent"] = precedent
            finally:
                timings["Field expectations"] = round(time.monotonic() - began, 1)
                with self._lock:
                    running.remove("Field expectations")
                    done += 1
                announce()

        # Everything before consolidation is independent of everything else in
        # it, so none of it waits on the rest. The consistency checks are mostly
        # Crossref round trips and take their own thread from the start. The
        # precedent pass takes a third model slot, but only once the first
        # text-only lens has finished: three requests started together all read
        # the manuscript cold, which is why CONCURRENCY is two, while a third
        # that starts on a warm cache costs nothing extra. Queued behind the
        # lenses instead, it waited about 75 s for a slot.
        with ThreadPoolExecutor(max_workers=2) as side, \
                ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            checking = side.submit(consistency)
            looking = side.submit(precedents)
            jobs = [pool.submit(one, p) for p in sorted(PASSES, key=_start_order)]
            for future in jobs:
                future.result()  # re-raises whatever a pass raised
            looking.result()
            checking.result()
        lap("Before consolidation")
        done = len(PASSES) + 2

        # Synthesis and the cutting pass are checkpointed separately. They are
        # the last two stages and between them the most expensive, so an
        # interruption here used to throw away the half of the run least likely
        # to be reached again cheaply.
        report = self._resume_stage("draft")
        if report is None:
            if progress:
                progress(done + 1, total, "Consolidating")
            self.backend.pace()
            report = self.synthesise(manuscript)
            self._save_stage("draft", report)
        else:
            self.reused.append("Consolidation")
        lap("Consolidating")

        shorter = self._resume_stage("condensed")
        if shorter is None:
            if progress:
                progress(done + 2, total, "Cutting it down")
            self.backend.pace()
            shorter = self.condense(report)
            self._save_stage("condensed", shorter)
        else:
            self.reused.append("Cutting")
        lap("Cutting")

        report = self._resume_stage("report")
        if report is None:
            if progress:
                progress(total, total, "Deciding")
            self.backend.pace()
            report = self.decide(shorter)
            self._save_stage("report", report)
        else:
            self.reused.append("Decision")
        lap("Deciding")
        timings["Waiting on pace()"] = round(self.backend.paced, 1)
        timings["Total"] = round(time.monotonic() - started, 1)

        usage = self.backend.usage
        if self.checkpoint:
            usage = _combine(self.checkpoint.prior_usage(), usage)
            # The report exists now, so the intermediate notes have served their
            # purpose and a re-run should start clean.
            self.checkpoint.clear()

        warnings = list(self.backend.warnings)
        if self.reused:
            warnings.append(
                "Resumed a previous run; these passes were reused from disk rather "
                "than re-requested: " + ", ".join(self.reused) + "."
            )

        return ReviewResult(
            report=report,
            pass_notes=dict(self.notes),
            usage=usage,
            warnings=warnings,
            web_search=self.use_web_search,
            finished_at=time.time(),
            timings=timings,
        )


def _leading_text(manuscript: Manuscript, limit: int = 6000) -> str:
    """Title and abstract, roughly — enough to place the paper in the literature."""
    from .parts import TextPart

    for part in as_text_only(manuscript.parts):
        if isinstance(part, TextPart) and part.text.strip():
            return part.text[:limit]
    return ""


def _titles(slugs: list[str]) -> dict[str, str]:
    import sqlite3

    from .corpus_index import DB_PATH

    if not slugs:
        return {}
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
    try:
        marks = ",".join("?" * len(slugs))
        return dict(connection.execute(
            f"SELECT slug, title FROM papers WHERE slug IN ({marks})", slugs
        ))
    finally:
        connection.close()


def _combine(prior: dict, current: Usage) -> Usage:
    return Usage(
        input_tokens=prior.get("input_tokens", 0) + current.input_tokens,
        output_tokens=prior.get("output_tokens", 0) + current.output_tokens,
        cache_write_tokens=prior.get("cache_write_tokens", 0)
        + current.cache_write_tokens,
        cache_read_tokens=prior.get("cache_read_tokens", 0) + current.cache_read_tokens,
    )
