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

## Implementation gaps

Found by reading the code after the checks went in, not by hitting them. Each
one is a real inconsistency between what the tool does and what it says or
offers.

1. **The progress bar lies.** `review.py` counts `len(PASSES) + 3` steps;
   `serve.py` tells the browser `len(PASSES) + 1`. The bar therefore never
   reaches the end, and now that a review has more stages than it did, it is
   further off than it was.

2. **The consistency checks are invisible.** They run between the lenses and the
   field check, take about ninety seconds — a Crossref lookup per reference and
   two model calls — and report no progress at all. Ninety silent seconds in the
   middle of a run reads as a hang.

3. **The CLI cannot take supplementary information.** The web form can, since
   yesterday. Same reviewer underneath, two different sets of capabilities.

4. **Nothing after the lenses is checkpointed.** `precedent` is; synthesis, the
   cutting pass and the consistency checks are not. A run interrupted after the
   lenses redoes the expensive half, which is the half most likely to be
   interrupted, since it comes last.

5. **The cost preview predates the checks.** It counts the lenses and the
   synthesis. The two verification calls and the Crossref time are not in it, so
   both the price and the ETA now read low.

6. ~~`Reviewer.decide` is written and unwired.~~ **Wired.** The reason it
   looked inert was the test: it had only been run on reports about published
   papers, and every one of those contains a comment saying some central claim
   is unestablished, which is what major revision means. Given a report whose
   comments are all presentation — a missing scale bar, an undefined acronym —
   it moves major revision to minor. There was nothing to change, not nothing
   it could change.

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

## 7. Smaller things

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

## 8. Licensing

The corpus is CC BY, which permits commercial use with attribution, but the
referee reports are the substance and were written by people who did not consent
to that. Selling this as a service needs a real answer. Not urgent while private.
