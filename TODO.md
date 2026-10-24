# PeerNa — what is left

Ordered by what would change the output most, not by effort. Rationale for
anything already decided lives in `DECISIONS.md`; this file is only the open
work.

## Settled

**The architecture earns its cost.** Ten papers, 79 planted defects, web search
on for both arms: the full pipeline caught 31 against 16 for a single pass plus
the cutting pass. McNemar p=0.001, at 2.7× the price and 2.8× the time.

The gap is entirely in defects that require comparing two distant points in the
paper — cross-references 5 to 1, units 7 to 1 — while overstated claims, which a
single sentence settles, came out level at 5 to 5. That is what reading four
times buys.

Three earlier comparisons found nothing because two things were wrong with them,
both since fixed: the control had no cutting pass, so its effect and the passes'
were tangled; and web search was off, which gags the literature pass entirely and
left PeerNa fighting with three passes instead of four.

**Comment length.** The cutting pass took the median comment from 84 words to
44–60 against a corpus median of 42. The short tail is still thin — real reports
put 16% under 20 words, PeerNa reaches about 10%.

**Consistency checks.** `checks.py`, wired in. Dangling figure references, panel
letters outside a caption, undiscussed figures, citations past the end of the
bibliography, every reference resolved through Crossref for existence, date and
retraction, and each cross-reference and citation paired with what it points at
for the model to check one at a time. About $0.005 and 90 seconds. Verified to
catch a planted `Fig. 5g → 5j` that no mechanical check can see, because 5j
exists.

**Verdict variance — withdrawn, but the fix was kept.** Every run says major
revision, which was recorded as a defect on the assumption that a published
paper deserves at worst minor. 95% of 600 corpus review files show a revision
round: major revision *is* the base rate.

`Reviewer.decide` settles the recommendation in its own call, against the
finished comments, starting at minor and requiring the escalating comments to be
named, with a code check that demotes to minor when none are. It leaves reports
about published papers untouched — correctly, since their comments do name
unestablished claims — and moves a report whose comments are all presentation
from major to minor. Wired in as the last stage.

**Implementation gaps — closed.** Six found by reading the code after the checks
went in, all fixed: the step count lived in two places and had drifted, so the
progress bar never reached the end; the checks ran ninety silent seconds and now
name the part in flight; the CLI takes `--supplementary`; the consistency checks,
synthesis, cutting and the verdict are each checkpointed; the cost preview prices
manuscript-carrying calls apart from text-only ones; and `Reviewer.decide` is
wired.

A seventh turned up while testing them: `has_credentials` knew two providers by
name and assumed everything else was Anthropic, so the routed models — the only
ones with a working key — showed in the browser as unavailable.

**Confidentiality.** `requires_data_retention` had been declared since the
registry was written and shown to nobody. It now reads through
`retention_problem()`, which also flags that a routed model puts a second company
in the path. `AI_PEER_REVIEWER_ZERO_RETENTION` lets an operator declare an
agreement they hold; what it does is refuse the models that would make the claim
false. None of this covers web search, so the literature pass is now told to
search in its own words and never put a sentence, title, measured value, sample
name or author into a query — and to report a claim unverified rather than quote
the manuscript to check it.

## 1. Measure the checks

The checks went in after the ablation, so their effect is unmeasured. The same
ten flawed papers and the same answer sheet are on disk; re-running gives the
number directly. Of the 79 defects, 28 are cross-reference or reference-number
errors — the categories where both arms scored worst — so this is where the
headroom is. ~$1.20, 40 minutes.

## 2. A human reads them blind

**The most important open item, and the cheapest.** Every objective result rests
on defects we planted ourselves, which measures detection and nothing else.

The two supporting metrics turned out to be worth nothing:

- **Preference 9/10** was measured in the same call as the defect scoring, after
  it, with the planted list in the prompt. Nine of ten stated reasons cite the
  defect count. It is the defect result counted twice, not a second result.
- **"Unplanted real problems" 174 vs 159** accepted 97% and 98% of all comments.
  It measures which report is longer.

So there is no evidence yet about whether the reports are *better to receive*.
The twenty reports from the ablation are gone with a temporary directory and
have to be regenerated (~$1.50) — better from the Research Square set now, where
real reports of the same text exist. Stripped of labels and shuffled, one
afternoon of reading answers it, and a person is the only judge here who does
not share a model family with the writer.

Worth also re-running the model preference properly: its own call, no defect
list, reports only. That gives a machine baseline to compare the human against.

## 3. Precedent retrieval: the test on submitted manuscripts

Built part by part (DECISIONS: Precedents part by part) and still not shown to
help. The pilot on 126 published papers found nothing, but published text gives
the answers away and only 10 of them were in the field.

**The real test is ready.** 560 Research Square "In Review" manuscripts, the
text the referees read, each with its own referee reports as the answer key,
about 310 in the field (DECISIONS: A test set of manuscripts as submitted).
Files are in `~/PeerNa-data/rsquare/`: `pdf-clean/` holds the
manuscripts with the publication stamp redacted, `testset.slugs` the list.

In order:

1. **Test on what is harvested so far** rather than wait for the rest. The
   index of 2026-10-24 covers 43,555 papers, back to early March 2021; the
   held-out set (published from 2026-06-01) is complete at 3,240 papers. The
   harvest carries on from 2021-03-10 back to 2016 alongside, into the live
   database; the test reads a snapshot, `~/PeerNa-data/corpus-2026-10-24.sqlite`,
   which doubles as the backup that `~/.cache` lacks.
2. **Re-parsed with parser v2:** 1,274,166 points (936,822 first-round) and
   232,971 summaries from 43,351 files; comment vectors for points only.
   Still to do when the harvest ends: re-index, retry the failed papers,
   bundle with `tools.corpus_bundle` and, if the bundle passes 2 GB, keep
   only the last three or five years (the year is in the slug).
3. **Run it.**

       python -m tools.precedent_eval ~/PeerNa-data/rsquare/pdf-clean OUT \
           --slugs ~/PeerNa-data/rsquare/testset.slugs --submitted \
           --db ~/PeerNa-data/corpus-2026-10-24.sqlite

   Started 2026-10-24 into `~/PeerNa-data/runs/precedent-rsquare-2026-10-24/`. Web search stays off (it finds the published paper). About $28. Report the
   field subset and the years separately: the papers are from 2020 to 2026 and
   the predictor may have read the older ones.
3b. **Parser v2 — done** (e5458f6). Each comment has a `kind` (`summary` for
   a report's opening assessment, `point` for each numbered item or prose
   paragraph) and a review `round`. Rebuttal quotes and author replies are
   dropped per unit. On 31,834 files: 954,530 points (693,071 first-round)
   and 176,982 summaries, about 29% more points than before. Precedents and
   the answer key use points only, the answer key first round only;
   summaries are stored and untried.
4. **Decide.** If part by part wins, merge `method-level-precedent` and point
   the production server at the corpus release (`AI_PEER_REVIEWER_CORPUS`,
   DEPLOY: The corpus index). `corpus-2026-10` is published: 11,797 papers and
   330,146 comment vectors, 450 MB packed, fetched in about 25 s and searched
   in 256 MB of memory. The whole-journal harvest started 2026-10-23
   (`harvest-2026-10-all/`) will roughly quadruple it; re-index and publish a
   new tag when it finishes. If nothing wins, the retrieval needs
   rethinking before anything else is built on it.

Both arms of the old 20-paper test hit about a third, and the pilot about a
fifth with a stricter judge: predicting a referee is hard by any route.

## 4. Give it to a few postdocs

Once §1 and §2 land. This answers what no amount of measuring against the corpus
can — whether the comments are ones they would actually send, and whether the
report is worth waiting seven minutes for.

Worth doing while it still runs on this laptop: a handful of people need no
sign-in, no payment and no spending cap, and their reaction decides whether any
of that gets built.

## 5. Score on preprints — now §3

Done by another route than planned: Research Square rather than ChemRxiv, whose
full text is behind a Cloudflare challenge. See §3.

## 6. Sharing it with other people

Everything runs on one account key: three reviews on Luna free, more reviews or a
different model requires paying.

- **Free reviews cost about $0.10 each.** A hundred people taking three is $30.
  Affordable; a script taking them is not.
- **A hard ceiling on total spend**, at the account level, before the first
  stranger sees the URL. Sign-in limits what one person spends; the ceiling
  limits what an unforeseen thing spends.
- **Payment** is the largest item here and should wait until §4 says the tool is
  worth paying for.

### Sign-in: institutional email now, Google later

Verify an emailed link, accept institutional addresses only. Google sign-in is
the eventual upgrade, not the starting point — `notify.py` already sends mail, so
a magic link is one more template, while OAuth needs a client, a consent screen
and a privacy policy.

**The domain restriction does the real work, not the verification.** Verifying
*an* address gates nothing; disposable addresses are free and unlimited. A
`.edu` / `.ac.*` / research-institute allowlist stops them, costs nothing in
reach because everyone this is for has one, and reads as a qualification.

Two details that are easy to miss: normalise `+` addressing and dots before
storing, or one Gmail account becomes unlimited accounts; and keep the allowlist
editable without a deploy, because the first person turned away will be someone
who should have been let in.

Key the account record on the verified address so a Google account can later
attach to an existing one rather than starting a fresh free tier.

## 7. Check the direct path on Render

The router is gone — out of the menu, the registry and the blueprint. A
configuration that still names an `r-` model runs its direct equivalent, so
nothing breaks, but the name should be changed where it is set.

The dashboard is cleaned up: `MONO_ROURTER_API_KEY` is gone, the model is `luna`,
and `AI_PEER_REVIEWER_CONTACT` is set. **Still to check:** that a review
completes end to end on Render.

The Anthropic account has a monthly spend limit, separate from its credit
balance. When it is reached every Claude call fails until the month turns, while
the menu still shows the models as available.

## 8. Smaller things

- **Citation content coverage.** The abstract check reaches about 7 of 46
  references on a real paper — the rest have no abstract in Crossref (Nature
  titles largely do not deposit them) or their citation position is not detected.
  Europe PMC would fill some.
- **LaTeX input.** The form accepts `.tex` but nothing has been through end to
  end — in particular whether `\input` files and the bibliography survive, since
  a `.tex` upload arrives without its directory.
- **Speed.** A review took 570 s on one manuscript, 439 s after the checks and
  the precedent pass moved alongside the lenses (on this branch, not deployed).
  Left: the cutting pass takes about 100 s, and the precedent pass waits for a
  lens slot (75 s) when it could start as soon as one lens has warmed the
  cache.
- **Consolidation.** One re-run says the count target was dropping points
  (DECISIONS: Consolidation was dropping points). Check across the Research
  Square set that nothing a pass found goes missing from the report.
- **Remaining corpus noise.** 17% of what is kept is still not a referee. A
  model pass over every comment would remove most of it, ~$5–10.
- **Full-text corpus.** 4,500+ papers and growing, text only. The PDFs were
  not kept; `tools/download_corpus.py` is standalone and resumable if a machine
  with the disk turns up (~800 GB for everything, ~330 GB for reports alone).
- **Embeddings run locally.** Every review sends sentences from each part of
  the manuscript to OpenAI to search the comment index, a second company in
  the path. An open model (bge-small, e5, nomic-embed) would close that, at
  no saving worth the name (the whole index is about $2). It has to run on
  the server too, since the query must use the model the index was built
  with: check it fits 512 MB (ONNX, not PyTorch) and that retrieval holds up,
  after §3 says retrieval is worth keeping.
- **Use the reviewer, position and round of each comment.** Stored by parser
  v2 and so far used only to order comments and to keep the answer key to
  the first round. Each is a hypothesis to test against §3, not a feature to
  build on faith:
  - a point two or three referees of one paper raised independently is
    likely to matter; rank such precedents higher;
  - referees tend to put the major points first; check whether position
    predicts importance before weighting by it;
  - a first-round point that comes back in the second round is one the
    authors could not easily answer; those kinds of request may deserve a
    firmer place in the report;
  - the distribution of points per referee gives a measured target for how
    many comments a PeerNa report should carry.
- **Working files outside the repository.** Test sets, labels and run outputs
  live under `~/.cache/ai-peer-reviewer/`. The scratch directory used before
  2026-10-22 was wiped, taking the ablation reports with it; nothing that a
  result depends on should live there.

## 9. Licensing — resolved, with one thing to keep doing

The worry recorded here was that the corpus is CC BY but the referee reports are
the substance, and their authors had not consented to a commercial product.

The premise was wrong, and one query settled it. **787 of 800 review files carry
their own licence**, and it is not inherited from the paper:

> Open Access **This Peer Review File** is licensed under a Creative Commons
> Attribution 4.0 International License, which permits use, sharing, adaptation,
> distribution and reproduction in any medium or format, as long as you give
> appropriate credit to the original author(s) and the source…

Nature Communications transparent peer review is opt-in: a report is published
only if its referee agrees, and that agreement is what puts CC BY on it.
Commercial use is permitted outright. The caution here was invented, not found.

What CC BY does ask for is credit, and that is now given: a review that consulted
the corpus names the papers it drew on, with DOIs and the licence, in both the
Markdown and the HTML. The referees are anonymous, so the credit goes to the
paper and the journal. The precedent pass is also told to take the expectation
and leave the sentence — quoting a comment into a report about a different paper
would make it a republication, which needs more than a footnote.

**The thing to keep doing:** if the output ever reproduces a referee's wording
rather than paraphrasing it, the attribution obligation changes shape. Worth
checking whenever the precedent prompt is edited.

13 of 800 files carry no licence line. Left in the index — same journal, same
opt-in system, and the missing line is a formatting difference rather than a
different agreement.
