"""Orchestration: run the lenses over the manuscript, then consolidate.

This layer is provider-agnostic. It decides what to ask and in what order; the
backend decides how to ask it.
"""

from __future__ import annotations

import json
import threading
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
from .schema import Mismatches, RefereeReport, Verdict

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
CONDENSE_TARGET_WORDS = 45  # the corpus median is 42

#: What a review does after the lenses, in order. Named here rather than counted
#: at each call site: the count was written out in two places and they had
#: drifted apart, so the progress bar in the browser stopped short of the end of
#: every run. Adding a stage now means adding it here and nowhere else.
LATER_STAGES = (
    "Checking consistency",
    "Field expectations",
    "Consolidating",
    "Cutting it down",
)


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
            self.backend.note(
                "Web search was disabled: no citation was verified, and claims "
                "about prior work were suppressed rather than guessed."
            )
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

        Silently skipped when the corpus index has not been built — the review
        is still complete without it, just without the field-specific layer.
        """
        from .corpus_index import similar

        text = _leading_text(manuscript)
        if not text:
            return ""
        try:
            neighbours = similar(text, k=PRECEDENT_PAPERS)
        except Exception as exc:
            self.backend.warnings.append(
                f"Could not consult the corpus index ({type(exc).__name__}); "
                "the field-specific pass was skipped."
            )
            return ""
        if not neighbours:
            return ""

        lines, budget = [], PRECEDENT_COMMENTS
        for n in neighbours:
            lines.append(f"\n=== From referee reports on: {n.title[:90]} ===")
            for c in n.comments[: max(1, budget // len(neighbours))]:
                lines.append(f"- {c[:600]}")
                budget -= 1
        self.notes["precedent_sources"] = ", ".join(
            f"{n.title[:60]} ({n.similarity:.2f})" for n in neighbours
        )
        return self.backend.run_text(
            self.system,
            as_text_only(manuscript.parts),
            f"{PRECEDENT_INSTRUCTION}\n{chr(10).join(lines)}",
            False,
            "medium",
            "Field expectations",
        )

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
        from . import checks

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
        from . import checks

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
                )
            except Exception as exc:  # noqa: BLE001 - see the docstring
                self.backend.note(f"Could not shorten the comments ({exc}); kept the draft.")
                return best or report

            # Returning the wrong number of comments is the one failure that
            # matters and the one thing here that can be checked without
            # judgement, so it is checked rather than trusted to the prompt.
            if len(shorter.comments) != expected:
                continue

            if best is None or _median_words(shorter) < _median_words(best):
                best = shorter
            if _median_words(best) <= CONDENSE_TARGET_WORDS:
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

        def announce() -> None:
            if not progress:
                return
            with self._lock:
                label = " · ".join(running) if running else "Finishing"
                finished = done
            progress(finished, total, label)

        def one(review_pass: Pass) -> None:
            nonlocal done
            with self._lock:
                running.append(review_pass.title)
            announce()
            try:
                self.run_pass(manuscript, review_pass)
            finally:
                with self._lock:
                    if review_pass.title in running:
                        running.remove(review_pass.title)
                    done += 1
            announce()

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for future in [pool.submit(one, p) for p in PASSES]:
                future.result()  # re-raises whatever a pass raised

        # Independent of everything else, so it runs before the expensive half:
        # if the PDF cannot be parsed we would rather know now.
        self.check_consistency(
            manuscript,
            announce=(lambda said: progress(len(PASSES), total, said)) if progress else None,
        )
        done = len(PASSES) + 1

        if progress:
            progress(done, total, "Field expectations")
        self.backend.pace()
        precedent = ""
        if self.checkpoint:
            precedent = self.checkpoint.load("precedent") or ""
        if not precedent:
            precedent = self.precedent_pass(manuscript)
            if precedent and self.checkpoint:
                self.checkpoint.save("precedent", precedent)
        if precedent:
            self.notes["precedent"] = precedent

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

        shorter = self._resume_stage("report")
        if shorter is None:
            if progress:
                progress(total, total, "Cutting it down")
            self.backend.pace()
            shorter = self.condense(report)
            self._save_stage("report", shorter)
        else:
            self.reused.append("Cutting")
        report = shorter

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
        )


def _leading_text(manuscript: Manuscript, limit: int = 6000) -> str:
    """Title and abstract, roughly — enough to place the paper in the literature."""
    from .parts import TextPart

    for part in as_text_only(manuscript.parts):
        if isinstance(part, TextPart) and part.text.strip():
            return part.text[:limit]
    return ""


def _combine(prior: dict, current: Usage) -> Usage:
    return Usage(
        input_tokens=prior.get("input_tokens", 0) + current.input_tokens,
        output_tokens=prior.get("output_tokens", 0) + current.output_tokens,
        cache_write_tokens=prior.get("cache_write_tokens", 0)
        + current.cache_write_tokens,
        cache_read_tokens=prior.get("cache_read_tokens", 0) + current.cache_read_tokens,
    )
