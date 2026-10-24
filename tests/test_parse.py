"""What the review-file parser keeps, on a file shaped like the real ones.

Numbered and prose reports, the opening assessment of each, the rebuttal that
quotes every report again between the authors' replies, and a second round.

    python3 -m tests.test_parse
"""

import sys

from ai_peer_reviewer.corpus_index import parse_review

FILE = """Peer Review File
REVIEWER COMMENTS
Reviewer #1 (Remarks to the Author):
The authors report a doped oxide catalyst for acidic water oxidation and claim a
low overpotential and long stability. The work is interesting but the evidence for
the proposed vacancy mechanism is thin, and it needs revision before publication.
Comments:
1. The stability test runs for only ten hours at a low current density, which says
little about operation in an electrolyser.
2. The XPS fits of the Ru 3p region are not shown, so the claimed low-valent sites
cannot be judged.
Reviewer #2 (Remarks to the Author):
This manuscript describes a doped oxide nanowire catalyst for the oxygen evolution
reaction in acid and supports it with calculations of the reaction free energies
for each intermediate.
The calculations use a different facet from the one the electron microscopy shows,
and the authors should explain why the comparison with experiment still holds for
the exposed surface.
Faradaic efficiency is not reported anywhere and should be measured directly with
a rotating ring-disk electrode or by gas chromatography of the evolved oxygen.
Response to the reviewers' comments
We thank all the reviewers for their careful reading.
Reviewer #1 (Remarks to the Author):
1. The stability test runs for only ten hours at a low current density, which says
little about operation in an electrolyser.
Response: We have extended the test to 500 hours at 100 mA cm-2.
2. The XPS fits of the Ru 3p region are not shown, so the claimed low-valent sites
cannot be judged.
Response: The fits are now in Supplementary Fig. 9.
REVIEWERS' COMMENTS
Reviewer #1 (Remarks to the Author):
The authors have addressed my comments and I recommend publication.
"""


def main() -> int:
    failures = []
    parsed = parse_review(FILE)
    by = {(p.reviewer, p.round, p.kind): [] for p in parsed}
    for p in parsed:
        by[(p.reviewer, p.round, p.kind)].append(p.text)

    if len(by.get((1, 1, "summary"), [])) != 1:
        failures.append("the numbered report's opening assessment is not one summary")
    if len(by.get((1, 1, "point"), [])) != 2:
        failures.append(f"reviewer 1 should have two points, has {by.get((1, 1, 'point'))}")
    if not by.get((2, 1, "summary")) or len(by.get((2, 1, "point"), [])) < 2:
        failures.append(f"the prose report was not split: {by.get((2, 1, 'point'))}")
    if any("We have" in t or "Supplementary Fig. 9" in t for t in sum(by.values(), [])):
        failures.append("an author reply was kept")
    if (1, 2, "summary") not in by:
        failures.append("the second-round report is not round 2")
    if any(p.round == 2 and p.kind == "point" and "stability" in p.text for p in parsed):
        failures.append("the rebuttal's quotation of round 1 was kept as round 2")

    for failure in failures:
        print("FAIL:", failure)
    print("parse:", "ok" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
