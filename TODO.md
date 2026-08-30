# PeerNa — what is left

Ordered by what would change the output most, not by effort. Rationale for
anything already decided lives in `DECISIONS.md`; this file is only the open
work.

## 1. Comment length — done, partly

A separate cutting pass now runs after synthesis (`Reviewer.condense`). It may
not add, drop, merge or reorder anything, only rewrite, and a comment-count check
falls back to the draft if it tries. Measured on an 18-comment report:

| | median | shortest | total |
|---|---|---|---|
| draft | 84 w | 67 w | 1531 w |
| after cutting | 44–60 w | 25–40 w | 780–1011 w |
| real reports | 42 w | — | — |

The median is now roughly right. What is still missing is the **short tail**: real
reports put 16% of comments under 20 words and 32% between 21 and 40; PeerNa gets
to about 10% and 20%. It has learned to write medium comments instead of long
ones, not to write one-line ones.

Two things worth knowing before touching this again:

- **The pass is noisy.** Identical input and prompt, five runs: 44, 44, 44, 60,
  72. That spread is wider than the difference between any two prompt versions,
  so a single run tells you nothing. Three earlier rounds of tuning were probably
  read as regressions on the strength of one draw each.
- **Sampling does not fix it.** Best-of-three came back at 60, 42, 51 for three
  times the cost — the draws within a call are correlated.

## 1b. Is PeerNa better than just asking a model? — inconclusive, leaning yes

Three published Nature Communications papers, each put through the full pipeline
and through a single "review this manuscript" prompt on the same model with the
same PDF. A blind judge scored both against what the real referees had asked for.

| | PeerNa | one-shot |
|---|---|---|
| preferred by the judge | **3 / 3** | 0 / 3 |
| real concerns caught | 12 / 34 | 12 / 34 |
| caught that the other missed | 0 | 0 |
| grounded in the manuscript | 53/53 | 50/50 |
| substantive | 51/53 | 50/50 |
| cost per review | $0.10 | $0.017 |
| time | ~6 min | ~1.5 min |

**The only thing that separates them is the judge's preference**, and its reasons
were consistent across all three: PeerNa is more focused and proportionate, the
one-shot report "sprawling" and prone to demanding more controls than the claims
need. That is the cutting pass showing up, and it is a real difference — but it
is a subjective call made by the same model family that wrote both reports.

On the objective measure the two are indistinguishable: identical recall, neither
finding anything the other missed, both fully grounded. That is not evidence
PeerNa is no better — it is evidence this test cannot tell. **62% of the real
concerns (21 of 34) were marked already-addressed**, because the corpus holds
published papers while the referees were reading submissions. Thirteen usable
concerns across three papers cannot separate two systems.

So: six times the price buys a better-shaped report and no measurable increase in
what it finds. Two follow-ups, one running now and one deliberately deferred:

- **Planted defects** (§1c) — running. Sidesteps the version mismatch by
  breaking the paper ourselves.
- **Preprints** (§1d) — later. The principled fix, and the bigger job.
- **Have a human read a few blind.** The judge and the reviewer share a model;
  a preference measured that way is worth less than one postdoc's.

## 1c. Planted defects — in progress

Twenty-seven defects across the same three papers, written into the PDFs as
short in-line substitutions: numbers that now contradict a value stated
elsewhere, units off by a factor, cross-references pointing at the wrong panel,
one sign flip on a DFT adsorption energy. Nothing deleted, no paragraph
rewritten, figures untouched.

Deliberately none of them visible on a single read — every one requires checking
the paper against itself, which is exactly the claim four passes are making over
one. The answer sheet is exact, so recall means something here in a way it did
not above.

Two things this cannot tell us: whether the defects are representative of what
real submissions get wrong, and whether either system finds problems nobody
planted. Both matter; neither is a reason not to run it.

## 1d. Score on preprints — later

The principled version of §1b. A ChemRxiv or bioRxiv preprint that was later
published with a transparent peer review file gives a manuscript and a referee
report describing *the same text*, which is the one thing the corpus cannot give
us.

Why it is deferred rather than done: preprint coverage in chemistry is thin
(roughly a fifth to a third of papers), the preprint on file is not always the
version that was submitted, and the preprint→publication mapping is its own
piece of work — Crossref relations and the bioRxiv `/pubs/` endpoint both give
part of it. Worth doing when the comparison is worth running on twenty papers
rather than three.

## 1e. Consistency checks — built, and smaller than promised

`checks.py` compares the manuscript against itself in code: dangling figure and
table references, panel letters outside what a caption defines, captions never
discussed, citations past the end of the reference list. Wired into synthesis as
claims for the model to verify against the manuscript rather than as findings to
repeat.

It does **not** catch what motivated it. The planted cross-reference defects
pointed at panels that exist — Fig. 5g changed to Fig. 5j, where Fig. 5j is a
real panel — and nothing mechanical can see that. Score on the planted set: 0 of
27. What it does catch is references to things that are not there, which is a
different and rarer error.

Noise: 103 findings across 200 published papers, 0.52 per paper. Down from 77 on
three papers in the first version; the fixes are recorded in `DECISIONS.md`.
Whether the remainder are real errors in published work is unknown.

Worth doing next, in order:
- **Resolve the bibliography against Crossref.** Every DOI checked for existence
  and for retraction. Entirely mechanical, no model, and no chat session does it.
  This is the part of the "citation checking" claim that actually holds up.
- **Numbers that disagree between the abstract and the body.** The one planted
  defect class that is mechanically findable and is not yet checked.

## 1f. Web search was off for all five experiments

Every comparison so far ran with `use_web_search=False` on both arms. That is
even-handed, but it is not neutral: with search off the literature pass is
gagged — it is explicitly told not to assert what it could not verify — so
PeerNa was running three passes against a one-shot prompt, and the missing pass
is the only one doing work a single prompt never attempts.

The scoring never touched `citation_checks`, so that part was never at stake.
What was at stake is novelty, which the literature pass writes into the numbered
comments like any other finding.

Re-running with search on for both arms is now possible through the router and
is in flight. Until it lands, read every result above as "PeerNa minus its
literature pass, against a one-shot prompt".

## 2. Verdict variance — withdrawn; the yardstick was wrong

Every run came back `major_revision` — nine for nine, then twelve for twelve,
including on published papers. That was recorded here as a defect on the
reasoning that a published paper deserves at worst a minor revision.

**It does not.** Of 600 corpus review files, 95% show a revision round and 88%
carry six or more reviewer headings — three referees across two or more rounds.
Nature Communications papers are published *after* major revision, so that is
the modal outcome, and a tool that says major revision to a paper as submitted
is matching the base rate rather than dodging the question.

The fix built for this changed nothing, and was right not to. `Reviewer.decide`
settles the recommendation in its own call, against the finished comment list,
starting from minor revision and requiring the escalating comments to be named —
with a code check that demotes to minor when nothing is named. Run on six
reports it left all six at major, and the comments it named were the right kind:
"the dissolved-species assignment is unestablished", "'mediator' is not
established". Given those comments, major revision is correct.

The code stays, unwired. The naming guard is worth having when a real
submission produces a report with nothing but presentation comments in it, and
there is no cost to leaving it in place. Wire it in when there is a reason.

**What this invalidates.** Several judgements in this file and in `DECISIONS.md`
leaned on "major revision on a published paper is obviously wrong". That
premise is gone. It does not change any of the head-to-head results — both arms
were scored the same way — but it removes an argument that was used to call the
recommendation field worthless.

**What is still unknown**, and is the question this one should have been: does
the recommendation vary at all? Nine for nine on papers that all deserved major
is consistent with a working judgement *and* with a constant. Telling them apart
needs papers that deserve different answers — a rejected preprint, an early
draft, something already through revision — and the corpus contains only one
kind.

## 3. Precedent retrieval

Working, but only proof-of-concept validated on 200 papers.

- Re-measure once the 5,000-paper index is built.
- Compare retrieval on abstract alone against abstract + methods. Abstract-only
  was chosen for cost, never tested against the alternative.
- Check that retrieved comments actually change the output — currently unknown
  whether they earn their tokens.

## 5. Corpus

- 5,000-paper text harvest is running; embeddings rebuild automatically after.
- Full PDF collection (~800 GB for everything, ~330 GB for reports alone) needs
  a machine with the disk. `tools/download_corpus.py` is standalone and
  resumable — copy that one file across and run it.

## 6. Sharing it with other people

**Changed decision.** Users no longer bring their own API key. Everything runs on
one account key: three reviews on Luna free, and anything beyond that — more
reviews, or a different model — requires paying.

That is a better product and a worse liability, and the difference is worth being
explicit about, because it is now possible for strangers to spend real money:

- **Free reviews cost about $0.30 each.** A hundred people taking their three
  free runs is roughly $90. That is affordable; a script taking them is not.
- **The free tier has to be counted per person**, and a counter is only as good
  as the thing identifying the person. See below.
- **A hard ceiling on total spend is needed regardless**, at the account level.
  Sign-in limits what one person can spend; the ceiling limits what an
  unforeseen thing can spend, which is a different problem. Set it before the
  first stranger sees the URL, not after.
- **Payment is a real piece of work** — a processor, a plan record per account,
  and a way to turn access off when a payment fails. It is the largest single
  item on this list and should wait until the postdoc trial (below) says the
  tool is worth paying for.

### Sign-in: institutional email now, Google later

**Decided: verify an emailed link, and accept institutional addresses only.**
Google sign-in is the eventual upgrade, not the starting point.

Email verification wins on setup cost — Google OAuth needs a registered client, a
consent screen and a privacy-policy URL, while `notify.py` already sends mail
over SMTP and a magic link is one more template on code that exists.

It loses badly on abuse, though, and this is the part that matters: verifying
*an* email address gates nothing, because disposable addresses are free and
unlimited. **The domain restriction is doing the real work here, not the
verification.** A `.edu` / `.ac.*` / research-institute allowlist stops throwaway
addresses outright, and it costs nothing in reach, because the people this tool
is for all have one. It reads as a qualification rather than a restriction.

Two details that are easy to miss:

- **Normalise `+` addressing and dots before storing**, or one Gmail account
  becomes unlimited accounts.
- **Keep the allowlist editable without a deploy.** Institutional domains are a
  long tail — national labs, hospital groups, foreign universities — and the
  first person turned away at the door will be someone who should have been let
  in.

Google sign-in comes later and replaces only the identity step: same quota, same
ceiling, one less email to send. Worth keeping the account record keyed on the
verified address so that a Google account can be attached to an existing one
rather than starting a fresh free tier.

Email notification already works but is single-user; it becomes per-account at
the same time.

## 6b. Give it to a few postdocs

Once the cutting pass, the baseline comparison and the Chrome bug are settled,
hand it to a handful of postdocs and watch what happens. This is the step that
answers questions no amount of measuring against the corpus can: whether the
comments are ones they would actually send, whether the report is worth waiting
six minutes for, and whether anyone would pay for the fourth one.

Worth doing while it still runs on this laptop and costs nothing per user — a
small trial group needs no sign-in, no payment and no spending cap, and their
reaction decides whether any of that gets built.

## 7. LaTeX input

The web form accepts `.tex`, but nothing has been tested end to end with a real
LaTeX submission — in particular whether `\input`/`\include` files and the
bibliography survive, since a `.tex` upload arrives without its directory.

## 8. Licensing

The corpus is CC BY, which permits commercial use with attribution, but the
referee reports are the substance of the product and were written by people who
did not consent to that. Selling this as a service needs a real answer, not an
assumption. Not urgent while it stays private.
