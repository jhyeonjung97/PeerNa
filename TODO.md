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
Twenty reports are on disk. Stripped of labels and shuffled, one afternoon of
reading answers it — and a person is the only judge here who does not share a
model family with the writer.

Worth also re-running the model preference properly: its own call, no defect
list, reports only. That gives a machine baseline to compare the human against.

## 3. Precedent retrieval

Never demonstrated. On 20 papers, predicting what referees would ask for from the
corpus beat predicting from the abstract alone 4.5 to 3.9 (sign test p=0.63).

Contamination is not the explanation — the model does no better on 2017–2020
papers than recent ones, so it is not reciting reports it memorised. On the seven
2025 papers base rates led 6.6 to 4.9, which is a post-hoc subgroup and so a
hypothesis, but it points where you would expect: the corpus should help most
where the model's prior is thinnest, which is the condition of every real
submission.

**126 papers published after the model's May 2026 cutoff are now in the corpus.**
Re-running the prediction test on those is the clean version of that test, and it
is cheap. ~$0.40.

Both arms hit about a third, which says more than the comparison: predicting a
referee is hard by any route.

## 4. Give it to a few postdocs

Once §1 and §2 land. This answers what no amount of measuring against the corpus
can — whether the comments are ones they would actually send, and whether the
report is worth waiting seven minutes for.

Worth doing while it still runs on this laptop: a handful of people need no
sign-in, no payment and no spending cap, and their reaction decides whether any
of that gets built.

## 5. Score on preprints — later

The principled version of the head-to-head. A ChemRxiv or bioRxiv preprint later
published with a transparent review file gives a manuscript and a referee report
describing *the same text*, which the corpus cannot.

Deferred because preprint coverage in chemistry is thin (a fifth to a third), the
posted version is not always what was submitted, and the preprint→publication
mapping is its own piece of work. Worth doing when the comparison is worth running
on twenty papers rather than three.

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

## 7. Switch off the router — automatic, then check

`MONO_ROURTER_API_KEY` runs out at the end of August 2026, Korean time.
`models.preferred()` turns every `r-` model into its direct equivalent from
2026-08-31 15:00 UTC, which is midnight on the 1st in Seoul. Both the web form
and the CLI go through it, so nothing needs changing at the moment it happens.

A date rather than a health check, because the failure cannot be detected any
other way: an expired key is still a key, and only spending a request finds out.
The date is known, so it is used.

**What to check on the 1st:**

- A review actually completes on `luna`. That needs credit on the OpenAI
  account, which had none as of the 30th.
- `ANTHROPIC_API_KEY` is set on Render if the Claude models are wanted. They are
  the only way to compare architecture against model without the router's
  caveats.
- The routed entries can stay in the registry. They cost nothing unused and
  document what a middleman takes away.

The direct path is where this was always going: one company between the
manuscript and the model instead of two, and the only path that can run under a
zero-retention agreement.

## 8. Smaller things

- **Citation content coverage.** The abstract check reaches about 7 of 46
  references on a real paper — the rest have no abstract in Crossref (Nature
  titles largely do not deposit them) or their citation position is not detected.
  Europe PMC would fill some.
- **LaTeX input.** The form accepts `.tex` but nothing has been through end to
  end — in particular whether `\input` files and the bibliography survive, since
  a `.tex` upload arrives without its directory.
- **Full-text corpus.** 3,853 papers, 139,703 comments, text only. The PDFs were
  not kept; `tools/download_corpus.py` is standalone and resumable if a machine
  with the disk turns up (~800 GB for everything, ~330 GB for reports alone).
- **No remote for this repository.** It used to sit in Drive and be backed up by
  it. `DECISIONS.md` is not reproducible.

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
