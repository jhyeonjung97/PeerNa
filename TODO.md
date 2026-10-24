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

The checks went in after the ablation, so their effect is unmeasured. The ten
flawed papers and their answer sheet are **not** on disk any more, whatever
this file said before: a search on 2026-10-24 found neither, most likely gone
with the scratch directory wiped on 2026-10-22. Re-plant about 80 defects in
ten published papers, keep them under `~/PeerNa-data/`, and run both arms
again, with the checks and without, since the old 31 / 79 was measured on other
defects. About $2.50 on Luna, 90 minutes.

Add a second arm to the same run: the consistency checks at medium effort
rather than high (two calls a review, about $0.06 on Sonnet). Keep high unless
medium catches as many of the cross-reference and citation defects.

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

## 3. Precedents and the model: one test on clean manuscripts

Two questions on the same run: does precedent retrieval help, and is Sonnet
(the default since 2026-10-24) better than Luna at predicting referees.

**The clean test set.** Manuscripts published from 2026-07-01, after both
predictors' training data (Sonnet 5.5 June 2026, Luna February 2026), with a
Research Square v1 posted between 30 days before receipt and acceptance, CC BY.
Crossref links found 56 among the 3,240 held-out papers and a title and first-
author search of Research Square DOIs 62 more (`~/PeerNa-data/rsquare/
find_by_title.py`). Of the 118, 94 are confirmed by the DOI Research Square
stamps on the preprint once the paper is out; 15 more by a Crossref link, a
title match of 0.9 or better, or the abstract; 9 could not be confirmed and
are left out. **109 manuscripts**, in `~/PeerNa-data/rsquare-clean/`
(`testset.json` records how each pair was confirmed, `pdf-clean/` the
redacted PDFs). Report on all 109 and on the 94 stamped ones.
Kept for the final judgement only, never for tuning; the older 560-paper set
(`testset.slugs`, most of it inside both models' training data) is the one
to tune on.

**Before the run, so it never has to be repeated:**

1. *Precedents from the past only.* Each manuscript sees papers published
   before its received date (`~/PeerNa-data/published-dates.json`, 77,548
   dates from Europe PMC). Without this a 2026 manuscript is shown the
   reviews of later, related papers.
2. *Same effort for both predictors.* Claude Code ran at low, the API at high.
3. *Two judges, and both models' predictions in one pool.* A Sonnet judge
   may favour Sonnet's wording; judge with Sonnet and with Luna, each seeing
   every prediction for a paper shuffled and unlabelled, and trust what both
   agree on. A person marks a sample of 20–30 to see which judge is closer.
4. *Memorisation probe.* Ask each model what the referees of a few test papers
   asked, given the title alone. Anything specific marks the paper as seen.
5. *Keep every input and output.* Prompt, manuscript hash, material, sources,
   model, effort, index tag and commit beside each prediction, so a change to
   judging or counting reruns the judge and nothing else.
6. *Answer-key spot check.* Read the first-round points of ten test papers for
   author replies, reference fragments and round errors.
7. *A five-paper dry run* that checks all of the above, then the full run.
   Report each comparison with its confidence interval, not only a p-value:
   with about 100 papers only a difference near 0.5 hits a paper shows.

**Index.** `corpus-2026-10-24`: papers registered from 2022, 37,990, with
1,098,322 first-and-later-round points, comment vectors as int8 (top-8 search
results 97% the same as float16). Published and live on peerna and peerna-dev
since 2026-10-24. The
snapshot `~/PeerNa-data/corpus-2026-10-24.sqlite` is what the test reads and
the backup `~/.cache` lacks. The harvest carries on back to 2016 into the live
database only.

Predictor and judge run through Claude Code where they are Claude
(`--predictor-cli`, `--judge-cli`); Luna runs on the API.

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
  - not a count target: a PeerNa report stands for all its referees
    together, so it may carry more points than one referee writes. Match
    the length of each comment to the corpus, never the number.
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
