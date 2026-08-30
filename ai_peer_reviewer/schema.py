"""The shape of a review.

The model fills this structure rather than writing prose directly. That buys
two things: every review has the same sections regardless of the paper, and the
result can be re-rendered (referee report, severity-sorted checklist, diff
against a previous run) without paying for another API call.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field


class Comment(BaseModel):
    """One numbered point, written the way a referee writes it.

    Real referee comments are continuous prose — no headings, no labelled
    "why it matters" or "suggested fix" sections. Measured across 758 reports in
    Nature Communications, the median comment is 52 words; three quarters are
    under 115. Anything much longer reads as a memo, not a review.
    """

    text: str = Field(
        description="The comment as a referee would write it: continuous prose, "
        "no headings or labels. Lead with the objection — the authors wrote this "
        "paper and do not need its contents summarised back to them, so a bare "
        "pointer to the location is enough before the point itself. Length "
        "follows the weight of the point; a small one is a single sentence. Say "
        "what would settle it where that helps, in the same flowing prose."
    )
    severity: Literal["major", "minor"] = Field(
        description="Used only to order the list; it is not printed. Major means "
        "it affects whether a conclusion holds."
    )


class CitationCheck(BaseModel):
    reference: str = Field(description="The work as cited in the manuscript.")
    location: str = Field(description="Where in the manuscript this citation appears.")
    status: Literal["verified", "not_found", "misattributed", "unverified"] = Field(
        description="'verified' only if a web search actually confirmed this work "
        "exists and says what the manuscript claims. 'not_found' if a search "
        "failed to find it. 'misattributed' if it exists but does not support "
        "the claim. 'unverified' if no search was run."
    )
    note: str = Field(description="What the check found. Empty string if nothing to add.")


class RefereeReport(BaseModel):
    """A report in the shape journals actually receive.

    The structure here mirrors what Nature Communications referees write: one
    opening paragraph, then a numbered list running from the most serious point
    to the most trivial. Everything else this tool works out — severity, figure
    problems, citation checks — is folded into that list rather than given its
    own headed section, because a real report has no such sections.
    """

    general_comments: str = Field(
        description="The opening paragraph. Say what the manuscript reports and "
        "what its main result is, in two or three sentences and your own words, "
        "then give your overall judgement in a final sentence or two — whether it "
        "is suitable for publication, and after what level of revision. About 100 "
        "words; certainly between 70 and 150. Write it as one flowing paragraph, "
        "not as headed parts."
    )
    comments: list[Comment] = Field(
        description="The numbered points, ordered from most important to most "
        "minor — the first entry should be the thing that most threatens the "
        "paper. One problem per entry: two issues an author would fix separately "
        "are two comments, never one paragraph covering both. Twelve to eighteen "
        "entries is normal for this review, which is the product of five "
        "readings. Figure problems belong here like any other comment. Citation "
        "verification does not: it is reported in citation_checks. Do not pad, "
        "and do not split one problem across two entries."
    )
    citation_checks: list[CitationCheck] = Field(
        description="Only citations you actually checked or that you are flagging "
        "as questionable. Not an inventory of the bibliography."
    )
    references_total: int = Field(
        description="How many references the manuscript's bibliography contains, "
        "as best you can count. 0 if you could not tell. This is what makes the "
        "checked list honest: a reader needs to know 8 of 60 were examined, not "
        "just that 8 were."
    )
    recommendation: Literal[
        "accept", "minor_revision", "major_revision", "reject"
    ]
    confidence: Literal["low", "medium", "high"] = Field(
        description="Your confidence in this assessment. Say 'low' when the "
        "paper sits outside what you can competently judge."
    )


class Verdict(BaseModel):
    """The publication decision, taken on its own.

    Written in the same breath as the comments, the recommendation came back
    `major_revision` nine times out of nine — including on papers that were
    already published, where the honest answer is at worst minor. The one-shot
    control did the same, so this is the model declining to commit rather than
    anything in our prompt.

    Two changes, both about making the safe answer cost something. It is decided
    in its own call, against a finished list, so it is not bound to the tone the
    opening paragraph set before a single comment existed. And escalating beyond
    minor means naming the comments that force it — a claim that can be checked
    against the list, in code, rather than a word that costs nothing to write.
    """

    escalating_comments: list[int] = Field(
        description="The numbers of the comments that make this more than a "
        "minor revision, if any. A comment belongs here only if the paper's "
        "conclusions do not survive it as written. Empty for accept or minor."
    )
    recommendation: Literal["accept", "minor_revision", "major_revision", "reject"]
    confidence: Literal["low", "medium", "high"] = Field(
        description="Your confidence in this decision. Say 'low' when the paper "
        "sits outside what you can competently judge."
    )


class Mismatch(BaseModel):
    """One pairing that does not hold up."""

    target: str = Field(description="The cross-reference or reference number, "
                                    "exactly as it was given to you")
    why: str = Field(description="What the sentence says it shows, against what "
                                 "the caption or abstract says it contains. One sentence.")


class Mismatches(BaseModel):
    mismatches: list[Mismatch]


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Emit a JSON Schema the structured-output API will accept.

    Two transformations are needed beyond what Pydantic emits: `$ref`/`$defs`
    are inlined, and every object is closed (`additionalProperties: false` with
    all properties required).
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    resolved = _inline_refs(schema, defs)
    _close_objects(resolved)
    return resolved


def _inline_refs(node: Any, defs: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.split("/")[-1]
            if name in seen:
                raise ValueError(f"Recursive schema reference: {name}")
            target = copy.deepcopy(defs[name])
            # Sibling keys (e.g. a description) override the referenced schema.
            merged = {k: v for k, v in node.items() if k != "$ref"}
            target.update(merged)
            return _inline_refs(target, defs, seen + (name,))
        return {k: _inline_refs(v, defs, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(item, defs, seen) for item in node]
    return node


def _close_objects(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
        for value in node.values():
            _close_objects(value)
    elif isinstance(node, list):
        for item in node:
            _close_objects(item)
