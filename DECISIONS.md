# PeerNa — decisions and what they cost to learn

Why the tool is built the way it is. Most of these were settled by measurement,
and several overturned what seemed obvious beforehand — those are the ones worth
reading before changing anything.

Last updated 2026-08-26.

---

## What it is

A referee-style review of a scientific manuscript, run locally, billed to
whoever runs it. Named for **Peer** review + **Na**ture Communications, and for
피어나 — to bloom.

Five readings of the paper through different lenses, plus a sixth that checks it
against what referees demand of similar work, then one consolidation into a
report shaped like a real referee report.

---

## Architecture

**Local CLI and a local web server, not a hosted service.** The web UI binds to
`127.0.0.1` only. Two reasons, and the second is the one that matters: reviews
cost money to run, and a hosted version would mean other people's unpublished
manuscripts passing through infrastructure we operate. Google login and stored
per-user keys only make sense once hosted, and hosting is the thing being
avoided.

**Sharing the tool means sharing the code.** Each person runs it on their own
machine with their own key in `~/.config/ai-peer-reviewer/.env`, so no login is
needed — it is their computer.

**Three seams, so a change lands in one place.** Adding a model is one entry in
`models.py`; adding a file format is one function in `loader.py`; adding a
provider is one class in `backends.py`. `review.py` never learns which provider
it is talking to.

---

## Reading the manuscript

**PDFs go to the model whole, never text-extracted.** A referee who cannot see
the figures cannot review the paper, and extraction destroys them. DOCX and
LaTeX are converted with their images re-attached in document order, so each
figure sits next to the caption that names it — dumping the text and then a pile
of unlabelled images loses the figure-to-caption binding, and a comment about
"Figure 3" is worthless if the model had to guess which image that was.

**Two passes get text only.** The claims pass and the literature pass do not
need to see the pages, and a 27-page PDF of figures costs four times what its
text does (54k tokens vs 13.5k). This matters most on the literature pass, where
search results pile onto a context that already holds the manuscript.

---

## Models

Registry in `models.py` carries every per-model difference — thinking style,
effort support, web-search tool version, context and page limits, pricing — so
the request code stays uniform. Swapping in a model by ID alone would 400.

| | cost/paper | notes |
|---|---|---|
| Fable 5 | ~$2.00 | most capable; needs 30-day data retention |
| Opus 5 | ~$0.75 | best value for a review you act on |
| Sonnet 5 | ~$0.60 | |
| Haiku 4.5 | ~$0.15 | 200K context, 100 PDF pages |
| **GPT-5.6 Luna** | **~$0.08** | current default; cheapest, bottom tier |

**Anthropic vs OpenAI is roughly a wash on price.** Measured tier by tier, the
flagships are within ~13% of each other; only at the bottom tier is OpenAI
clearly cheaper, and there the absolute difference is about 12 cents a paper.
There is no cost argument for switching providers — only a distribution one, for
reaching people who have one kind of key and not the other.

**OpenAI does read PDFs including figures.** An early note in this project said
otherwise; it was wrong. Both providers accept PDFs and send page images to
vision models.

---

## Things that were measured, not assumed

### Concurrency is 2, and 3 is worse

| | wall clock | cost | cache reuse |
|---|---|---|---|
| sequential | 9m 03s | $0.08 | — |
| **2 at a time** | **5m 54s** | **$0.07** | 87k tokens |
| 3 at a time | 7m 00s | $0.10 | 21k tokens |

Three loses because prompt caching needs a prefix to already be cached. Fire
three requests together and all three start cold, so the manuscript is billed and
processed in full for each. Rate limits were never the constraint — the cache
was. Raising `CONCURRENCY` past 2 will make things slower and more expensive
until the caching behaviour changes.

### Reasoning effort, not the model, made the literature pass slow

At `high` the literature pass ran past nine minutes and kept tripping the rate
limit; the run never completed. At `low` it finished in 29s but listed DOIs with
no indication of where they came from — recall dressed as verification. At
`medium` it took 128s and returned fewer citations, each with the URL a search
actually returned. Fewer checked claims, honestly sourced, beat more claims of
unknown provenance, so the literature pass runs at `medium`.

### Three attempts to shorten the comments, three failures

The report runs about 3.6× the length of a real referee report. What was tried:

1. **Spelling out the real length distribution** ("a sixth run under 25 words,
   the median is 52…") — made comments *longer*. The model anchored on the
   100-word allowance mentioned in the same paragraph. Median 94 → 110 words.
2. **One problem per numbered comment** — worked on the symptom. Median fell
   156 → 79 words and nothing exceeded 100. The total did not move: 8 long
   comments became 18 short ones.
3. **"Do not restate the paper back to its authors"** — changed the sentence
   structure as intended (comments now open with the objection) but not the
   length. 79 → 84 words.

The floor appears to be around 80 words per comment regardless of instruction.
**The cause is structural, not stylistic:** this tool reads the paper five times
and reports what all five passes found, so it produces roughly twice the prose a
human referee writes. Matching the human shape would mean discarding findings —
a content decision, not a formatting one, and not one to smuggle in through a
prompt. Leave it unless someone decides deliberately to cut coverage.

---

## The report format

Learned from 758 real reviews parsed out of the corpus, not invented:

| | median | 25–75% |
|---|---|---|
| opening paragraph | 104 words | 71–149 |
| number of comments | 6 | 5–9 |
| length of one comment | 52 words | 30–114 |

Openings are overwhelmingly "In this manuscript, the authors…", and 40% state
the verdict explicitly in the opening paragraph. There are no section headings,
no strengths list, no "Why this matters" labels — adding them makes the output
easy to spot as machine-written and harder to paste into a journal's form.

**Always checked, whatever the field:**

- **Novelty.** Search for the specific claim, not the topic, and re-read the
  paper's own bibliography with the same question — the closest prior work is
  very often something the authors already cite without noticing how much of
  their claim it covers. Where the claim survives, say so briefly.
- **Housekeeping.** Axes and units, figure panels referred to by the right
  letters, figures never discussed, spelling, unexpanded acronyms, `[ref]`
  placeholders, numbers that disagree between text and figures. Minor by nature,
  so it is the last comment, and brief — locations, not prose.

**Citations are reported separately**, below the numbered list, with a coverage
line saying how many of the bibliography were examined. A referee does not
normally audit a bibliography, so presenting it as part of the review would
misrepresent it; burying it as a footnote would waste the one thing here a human
reviewer rarely does. When web search is off, the pass is told it cannot claim
what prior work exists — left unsaid, it asserts prior art from memory, which is
exactly how fabricated references get into reports.

---

## The corpus

200 Nature Communications papers, each with its referee report and supplementary
information, in `corpus/`. All research articles — the `HAS_SUPPL:Y` filter
excludes reviews and corrections as a side effect, which is luck rather than
design; add an explicit article-type filter before trusting it at scale.

Collected through **Europe PMC**, which distributes the open-access corpus for
automated use. The referee report is found by its JATS label, not by filename:
Nature Communications numbers supplementary files inconsistently, so `MOESM6` on
one paper is `MOESM4` on the next. The label itself varies — "Supplementary
Information", "Supporting Information", "Supplementary Material", "Supplementary
file" all appear, while "Description of Additional Supplementary **Files**" is an
index of the other attachments and must not match.

**Do not fetch article PDFs from nature.com.** It serves a bot challenge to
non-browser clients — a ~3KB HTML page with a `_fs-ch-` asset path. Europe PMC
renders a byte-identical PDF at `europepmc.org/articles/{PMCID}?pdf=render`, and
that route works with an honest User-Agent.

### Scaling it

| | papers |
|---|---|
| Nature Communications, total | 90,675 |
| with full text in Europe PMC | 83,230 |
| with supplementary files | 78,062 |
| **with a published referee report** | **~50,000** |

Transparent peer review is opt-in and was not offered before 2016. Sampled by
year: 2014 0%, 2016 50%, 2020 75%, 2024 100%.

**What the index actually needs is small.** Only the title, abstract, and
reviewer comment text are used, so the full 50,000 comes to about **2.8 GB** —
not the 900 GB the PDFs would take. The binding constraint is collection time:
roughly 15–30 hours at one request per second, with about 80 GB of transient
download that is discarded after the text is extracted.

Recommended order: settle quality on the 200 first; then ~5,000 papers in the
relevant field (about two hours, 0.3 GB); only go wider when reviewing outside
that field.

---

## Finding what referees expect of similar work

Given a manuscript, embed its title and abstract, find the nearest corpus papers,
and pass their referee comments in as reference material for a dedicated pass.

**Papers are not sorted into fields.** Fields have no edges — a paper on Re-doped
RuO₂ for acidic OER is close to both "ruthenium oxide stability" and "acidic
water electrolysis" work, and any taxonomy would have to pick one. Nearest
neighbours do not have to.

**No vector database.** 50,000 abstracts is a 300 MB array and one matrix
multiply searches all of it in under a tenth of a second. Embedding the whole
corpus costs about $0.30. A server would add operational weight and no speed.

**Abstracts only, for now.** Embedding a whole 65,000-character paper into one
vector dilutes it toward the generic. Abstract-only retrieved sensibly in the one
test run — all five nearest neighbours to the test manuscript were RuO₂ doping,
covalency, and durability papers — but abstract vs abstract+methods has not been
compared. Worth measuring before assuming.

**The failure mode this pass has to avoid:** carrying a comment about another
paper across as if it were a finding about this one. "A reviewer asked another
paper for ICP data" is not evidence that this paper lacks ICP data. The pass is
required to locate the gap in *this* manuscript by page or figure, to ask whether
its absence undermines a claim this paper actually makes, and to say explicitly
which expectations the manuscript already satisfies.

---

## Operational lessons

**Save completed work immediately.** Each pass is written to
`~/.cache/ai-peer-reviewer/runs/` the moment it returns, and finished reports to
`~/.cache/ai-peer-reviewer/jobs/`. Both exist because work was lost: three
completed passes discarded by a rate limit, and a finished report that lived only
in server memory when the process was restarted. A job that exists only in memory
disappears silently and the user is left staring at a page that forgot everything.

**Rate limits: wait the interval the server names.** The SDKs' exponential
backoff starts far shorter than the `Retry-After` they are given and gives up
before reaching it. Passes are also paced apart, since five requests carrying the
same 54k-token manuscript will otherwise exceed a 200k tokens-per-minute
allowance partway through.

**Show elapsed time during long calls.** A 15-minute timeout with no output is
indistinguishable from a hang; a counter every 20 seconds is the difference
between "working" and "dead".

**Never script an app the user is using.** An AppleScript
`close (every document whose name is …)` against Microsoft Word closed every open
document, not only the targeted one, with `saving no`. LibreOffice
(`soffice --headless`) does document conversion without touching anything the
user has open.

**Restarting the server kills running reviews.** Check for in-flight jobs before
deploying a change, and tell the user a hard refresh is needed afterwards —
browsers keep the old JavaScript in memory.

---

## Environment

- `brotlipy 0.7.0` breaks every `httpx2` request with
  `Decompressor.decompress() got an unexpected keyword argument
  'output_buffer_limit'`, surfacing as a misleading `APIConnectionError`. Fix:
  `pip uninstall brotlipy && pip install "Brotli>=1.2.0"`. Check this first if
  SDK calls start failing with connection errors.
- LibreOffice is required only for DOCX→PDF conversion and the Word/PDF download
  buttons. Without it those degrade with a message rather than failing.

---

## Licensing

Nature Communications is CC BY 4.0, and so are the peer review files — the
licence is printed in each one, with the note that anonymous referees are to be
credited as "Anonymous Referee". CC BY permits commercial use with attribution.

The harder question for any commercial version is not the licence but custody of
other people's unpublished manuscripts. That needs real legal advice, not a note
in a repository.

---

## Calibration: it is one grade harsh, and possibly stuck

Three published corpus papers, run through the tool as though they were drafts.
They had already been through review, so a well-calibrated reviewer should mostly
return "minor revision".

| paper | PeerNa | what the real referees said |
|---|---|---|
| s41467-025-63722-6 | major revision | minor revision, recommend publication |
| s41467-025-58346-9 | major revision | minor revision, suitable for publication |
| s41467-025-65330-w | major revision | after major revision |

**No rejections** — the tool does not throw out papers that were published, so the
verdict is not simply broken. But it graded two of three one step harsher than
the real referees, and returned the same verdict all three times with nearly the
same comment count (17, 18, 18). That is consistent with the length finding: a
tool that reports twice what a human referee writes will also reach for the
heavier verdict.

The worry this raises is **variance, not severity**. If the answer is always
"major revision", the verdict carries no information whichever way it leans.
Three papers cannot separate "always major" from coincidence; ten would, at about
$1 and an hour.

---

## Parsing the corpus is code, not a model

Text comes out of PDFs with `pymupdf` — `page.get_text()`, deterministic and
free. No model is involved, so parsing quality is a matter of the regexes around
it, and those have been wrong twice.

**A quarter of every comment was contaminated with the authors' reply.** Peer
review files interleave each referee comment with the response to it, and
splitting on the reviewer heading alone leaves the reply attached. The index was
being fed sentences describing what the authors *did* as though they were what a
referee *asked for*. Cutting at the reply marker took contamination from 24.9% to
5.2%, and the median comment from 55 words to 39 — the excess was the reply.

This is why the harvester **stores the whole extracted text**, not only the parsed
comments. Re-fetching is the slow, rate-limited step; re-parsing is free. The
parser has already improved twice, and each time the stored text is what saved
the harvest from being run again.

Only the images cannot be recovered that way. About 7.6% of comments point at a
figure the referee pasted in — the demand is still legible from the words around
it, but if those images are ever needed, those papers have to be fetched again.
`--keep-pdfs` stores them at ~3.5 MB each if that risk is not acceptable.

---

## Collection rates, measured

4.2s of network per paper (1.3s for the JATS, 2.9s for the review PDF), plus a
2s courtesy pause, three workers.

| | wall clock | transferred | stored |
|---|---|---|---|
| 5,000 papers | ~2.9 h | 17 GB | 0.3 GB |
| 50,000 papers | ~1.2 days | 174 GB | 2.8 GB |

Most of what is transferred is discarded after the text is extracted.

---

## Open questions

- Comment length: 3.6× a real report, floor around 80 words per comment. Only
  fixable by deciding to report less, which is a content decision.
- Verdict variance: three papers all returned "major revision". Ten would show
  whether the verdict discriminates at all.
- Abstract-only vs abstract+methods for retrieval — untested.
- Chrome behaves differently from Safari in the web UI; unresolved.

## Comment length: a cutting pass, not a better prompt

Three attempts to get short comments out of the drafting prompt failed (recorded
in `passes.py`). Measuring the failure against the corpus showed why, and it was
not what the earlier note assumed. The problem was never the average:

|                | median | ≤20 w | 21–40 w | 41–80 w | 81–150 w |
|----------------|--------|-------|---------|---------|----------|
| corpus (n=46k) | 42 w   | 16%   | 32%     | 25%     | 13%      |
| PeerNa draft   | 84 w   | 0%    | 0%      | 33%     | 67%      |

PeerNa's comments were not uniformly too long, they were uniformly the *same*
length. Every point got the full three-move treatment — context, objection,
remedy — whether it was a broken causal claim or a missing axis label. Half of
every real report is under 40 words because half of every real report is a
one-line remark.

That is a drafting problem that cannot be fixed while drafting: deciding what to
say and deciding how little to say it in are different jobs, and asking for both
at once means the model does the first and pays lip service to the second. So
they are separated. Synthesis writes the report; `Reviewer.condense` does nothing
but cut, and is forbidden to add, drop, merge or reorder anything. The comment
count is checked in code, and a mismatch falls back to the draft — the worst case
is a verbose report, never a lost finding.

Result: median 84 → 44–60 words, total length 51–66% of the draft, at about
$0.016 and twenty seconds on a review that costs thirty times that.

### What the tuning actually taught

**Every stated allowance gets anchored on.** A version that said the biggest
points could keep "a hundred words, more if the argument needs it" came back at
median 74 — worse than the draft-relative baseline and the same failure the
earlier abandoned attempt hit. The instruction that works states only floors
("half the list under 40 words, several under 20") and never a ceiling.

**Single runs cannot tell prompt versions apart.** Five identical runs of the
same instruction on the same draft: 44, 44, 44, 60, 72. Comparing two prompts on
one draw each measures noise. This very likely explains at least one of the three
earlier "failed" attempts.

**Best-of-N does not help.** The obvious response to that noise — draw three, keep
the shortest — returned 60, 42 and 51 for three times the cost. Draws within one
call are correlated. Two attempts is what the evidence supports, and its real job
is recovering from a wrong comment count rather than a shorter result.

### Still open

The short tail. Real reports put 16% of comments under 20 words; PeerNa reaches
about 10% on a good run and 0% on a bad one. It has learned to write medium
comments instead of long ones, not to write one-line ones.

## Consistency checks in code, and what they can and cannot do

Three tests found no difference between PeerNa and a single plain prompt in what
either turned up. One result inside the third pointed somewhere: of five planted
cross-reference errors and two mangled citation numbers, both systems caught
zero. That is a whole category neither a model nor a bigger prompt was touching,
so `checks.py` does it in code instead.

### What it catches

Dangling references (the text points at Fig. 7 and there is no Fig. 7), panel
letters outside what a caption defines, captions never referred to in the text,
and citations numbered past the end of the reference list.

### What it does not catch, including the errors that motivated it

**Wrong-but-valid references.** The planted defects changed Fig. 5g to Fig. 5j
where Fig. 5j exists, and Fig. 2a–e to Fig. 3a–e where Fig. 3 exists. Nothing
mechanical can see that: both point at something real. The checker scores 0 of 27
on the planted set. The claim that a regular expression handles this class of
error was wrong, and only half of it was ever true — it handles references to
things that are *not there*, not references to the *wrong* thing.

### Getting the noise out

The first version produced 77 findings across three published papers, all false.
In order of what they cost:

- **Supplementary references.** "Supplementary Fig. 7" contains "Fig. 7", and
  supplementary figures live in a file that is not open. A lookbehind is not
  enough — "Supplementary Figs. 9, 13, 15" says the word once and the numbers
  three times — so the whole run is masked out before anything is scanned.
- **Hyphenated line breaks.** Two-column typesetting splits "Supplementary" into
  "Supple-\nmentary", and no pattern matching the whole word survives it.
  Rejoining these was worth more than everything else combined.
- **Masking before reading captions.** A caption can begin on the line a body
  sentence ends on, so masking first blanks the caption marker too, and the
  checker concludes the figure has no caption. Captions are read first now.
- **Panel ranges.** Captions write "a–c SEM images", so b and c never appear
  alone, the run stops at 'a', and every later panel looks undefined. Two thirds
  of the panel false positives.
- **Subscripts and crystallographic indices.** Citation superscripts are read off
  the PDF's font metrics, and "RuO2" and "durability7" are the same shape at the
  same size. So is "Ru (101)". Size cannot separate them; height can — a
  superscript is raised above the baseline and a subscript is dropped below it.
  This took the citation check from firing on 40% of published papers to 3%.

Across all 200 published papers with PDFs: 103 findings, 0.52 per paper, with
63 of 200 papers drawing at least one. How many of those are real errors in
published work and how many are parsing artefacts is not known.

### How it is used

The findings go into synthesis as claims to verify, not facts to repeat. The
model has the manuscript, is told a script produced these and that about one
paper in two draws a false one, and is asked to look each up and stay silent
about the ones that do not hold. Residual noise costs a dropped comment rather
than a wrong accusation in a referee report.

## Four experiments that found nothing, and why

Superseded by the ten-paper ablation recorded further down; kept because
the reasons each one failed are the useful part.

Every comparison below ran with **web search disabled on both arms**
(`use_web_search=False`), and the one-shot arm has no tools at all. Nothing here
turns on one side being allowed to look things up.

The one-shot arm is also stronger than it sounds. It is the same model, the same
PDF, and the same `RefereeReport` schema — an opening paragraph, comments ordered
by importance, a citations field, a recommendation. It is PeerNa's output format
with PeerNa's process removed, which makes it the right control for "do the
passes help" and the wrong control for "is this better than pasting a PDF into a
chat window". That second comparison has not been run.

### 1. Against what the real referees said — no difference

Three papers, blind judge. Recall 12/34 both ways; neither arm found anything the
other missed; both fully grounded. Preference 3/3 for PeerNa, on consistent
reasoning — the one-shot report is "sprawling" and asks for more controls than
the claims need, which is the cutting pass showing up.

Fatal flaw: **62% of the referees' concerns were already addressed** in the
published text they were scored against. Thirteen usable concerns cannot separate
two systems.

### 2. Against planted defects — no difference

27 defects written into three PDFs as short substitutions. PeerNa 9/27, one-shot
7/27, McNemar p=0.69. Both missed 16.

Neither caught a single one of the five cross-reference errors or two citation
errors — the finding that produced `checks.py`.

### 3. Against the referees' priorities — no difference

The measure the first two missed. Detection was never the claim; the complaint
about a general model is that it raises the wrong things. Precision survives the
version mismatch that ruined recall, because a concern referees never raised is
still evidence they did not care about it, whichever draft you hold.

Comments matching a real referee concern: PeerNa 49%, one-shot 54%. Consensus
concerns (raised by two or more referees): both 12/12, at mean position 6.7 and
7.0. **45% of both reports is legitimate but nobody raised it** — the judge noted
both spend far more on flow-cell and technoeconomic questions than the real
referees did. PeerNa does not fix this.

### 4. Ablation, three papers — the first daylight (superseded by the ten-paper run below)

Adding the cutting pass to the one-shot arm isolates the passes from the pass
that was known to work. PeerNa 11/27 against 7/27, preference 3/3, p=0.29.

Two caveats, and the second is the serious one. The sample is too small. And the
**same PeerNa reports scored 9/27 in experiment 2 and 11/27 here** — judge noise
is ±2 where the effect is 4.

### Predicting what referees will ask for — the corpus, tested at last

The corpus had never actually been tested; the precedent pass feeds comments in
as prose and asks the model to judge, which is not the same as counting. Asking
what referees *will demand* rather than what a report *found* removes the version
mismatch entirely, because the referee report is the answer key.

20 papers, 12 predictions each, one arm given the abstract plus real comments from
the eight nearest corpus papers and the other the abstract alone:

| | mean hits / 12 | papers won |
|---|---|---|
| base rates | 4.5 | 10 |
| prior alone | 3.9 | 7 |

Sign test p=0.63. Not a result.

Two things worth keeping from it. **Contamination is not the explanation** — the
prior arm does no better on 2017–2020 papers than on recent ones, so it is not
reciting reports it memorised. And on the seven 2025 papers, the largest bin, base
rates led 6.6 to 4.9. A post-hoc subgroup, so a hypothesis rather than a finding —
but it points where you would expect: the corpus should help most where the
model's own prior is thinnest, which is also the condition of every real
submission.

The absolute numbers say more than the comparison. **Both arms hit about a third**,
which means predicting a referee is hard by any route, and the field-average part
of a review is a third of it.

### What all five share

Every one ran on Luna and only Luna. "The extra passes do not help" and "Luna does
not benefit from extra passes" are indistinguishable from this evidence, which is
why the router was added.

## The router, and an afternoon lost to the wrong endpoint

MonoRouter is an OpenAI-compatible endpoint fronting OpenAI, Anthropic and
Google models behind one key. It was added for one reason: every measurement
above was taken on Luna alone, so "the extra passes do not help" and "Luna does
not benefit from extra passes" are the same evidence. Reaching a Claude model
settles that, and this is the only route to one without a second account.

It turned out to matter for a second reason. When the OpenAI account ran out of
credit mid-experiment, the router was already there and the same model —
`gpt-5.6-luna` — was reachable through it. Nothing had to stop.

### Use `/responses`, never `/chat/completions`

Both endpoints exist. Only one is usable, and the difference is not documented
anywhere we could find:

| | chat completions | responses |
|---|---|---|
| PDF input | rejected outright | works |
| structured output | **accepted and silently ignored** | works |
| server-side web search | no such tool | works, with real citations |

The middle row is the trap. Ask for a JSON schema and you get a 200, a normal
`finish_reason`, and prose. The only symptom is a parse failure several frames
away, which reads as a model failure rather than an endpoint one.

Because `/responses` works, the ordinary `OpenAIBackend` drives the router
unchanged — it already speaks that API. A separate `RouterBackend` was written
against chat completions, with tool-calling standing in for structured output,
and then deleted once the right endpoint was found. The lesson worth keeping is
the order of operations: chat completions is the obvious thing to try first and
the wrong thing to build on.

### One header

Cloudflare answers the OpenAI SDK's default User-Agent with a bare
`403 error code: 1010`. No message, no hint. It is indistinguishable from a
rejected key, and that is what it was taken for at first. Any other agent is
accepted, so one is now sent on every request.

### Prices

The router publishes a model list and no pricing. The specs carry upstream list
prices, so every cost this tool reports on a routed model is a floor, not a
figure.

## The verdict is not biased; the yardstick was

Nine runs, then twelve, all `major_revision`, including on published papers. That
was written up as the recommendation field being worthless, on the reasoning that
a published paper deserves at worst minor revision.

That reasoning was never checked, and it is wrong. Across 600 corpus review
files, **95% show a revision round** and 88% carry six or more reviewer headings —
three referees over two or more rounds. Nature Communications papers reach print
*through* major revision. Saying major revision to a manuscript as submitted is
the base rate, not an evasion.

The fix was built before the check: `Reviewer.decide` takes the recommendation in
its own call, against the finished comments, starting at minor revision and
requiring the comments that force an escalation to be named, with a code check
that demotes to minor when none are. It left all six test reports at major, and
named comments of exactly the right kind — central claims asserted to be
unestablished. It was working; there was nothing to fix.

Two things worth carrying:

**Check the yardstick before building against it.** The whole exercise —
schema, instruction, guard, twelve test calls — followed from an assumption that
would have taken one query to test, using data already on disk.

**A separated decision is not automatically a better one.** The same split fixed
comment length dramatically (median 84 → 44), which is what made it look like a
general remedy. It works when the two jobs genuinely compete for the model's
attention. Here they did not: the verdict already followed from the comments, and
giving it its own call changed nothing because nothing was being crowded out.

## The ablation that settled it — ten papers, checks not yet wired

This is the baseline for everything that follows, taken **before** `checks.py`
was wired into the review. Ten published papers, 79 defects planted as short
in-line substitutions, web search on for both arms, scored blind against the
answer sheet.

| | defects caught | comments | median | cost | time |
|---|---|---|---|---|---|
| full pipeline | **31 / 79 (39%)** | 180 | 54 w | $1.10 | 71 min |
| one pass + cutting | 16 / 79 (20%) | 163 | 52 w | $0.40 | 25 min |

McNemar on the 19 discordant defects: 17 to 2, **p = 0.001**. Nine of ten papers
went to the full pipeline; the one that did not (`58297-1`) was 1 against 2 on a
paper where both arms found almost nothing.

### Where the difference is

| defect kind | planted | full | one pass |
|---|---|---|---|
| number conflict | 26 | 13 | 9 |
| wrong cross-reference | 20 | 5 | 1 |
| wrong unit | 16 | 7 | 1 |
| overstated claim | 9 | 5 | 5 |
| reference mismatch | 8 | 1 | 0 |

**Overstated claims are level.** One sentence settles those, and one reading
finds them. Every point of the gap is in defects that need two distant places in
the paper compared — a number against the number it contradicts, a unit against
the quantity it belongs to, a reference against the caption it names. That is
what reading four times buys, and it is a narrower claim than "better reviews".

Both arms still missed 46 of 79. Winning is not the same as doing well.

### Why the three earlier comparisons found nothing

Not because the effect was absent. Because they were built wrong, in two ways
that each hid it:

- **The control had no cutting pass.** Comparing the full pipeline against a bare
  single prompt tangles the cutting pass's effect with the passes'. The cutting
  pass was known to work, so it flattered the control's *shape* while the passes'
  contribution to *findings* stayed invisible. Adding it to both arms is what
  isolated the question.
- **Web search was off.** With it off the literature pass is explicitly gagged —
  told not to assert what it could not verify — so PeerNa was fighting with three
  passes, and the missing one is the only one doing work a single prompt never
  attempts.

Both were pointed out by Hyeonjung, not found by measurement.

### Two metrics that measured nothing

Reported alongside the result at first, and both are artefacts:

**Preference, 9 to 1.** Asked in the same call as the defect scoring, after it,
with the planted list in the prompt. Nine of the ten stated reasons cite the
defect count — *"Report A catches four planted defects, whereas B catches only
the barrier discrepancy"*. It is the defect result counted a second time. To ask
it honestly the reports have to go out in their own call with no answer sheet.

**Unplanted real problems, 174 to 159.** The judge accepted 97% and 98% of all
comments; six of ten papers scored 100%. The absolute numbers differ because the
full pipeline writes more comments, not better ones. "Is this a real problem" is
almost always yes for a review comment — the question worth asking is whether it
is an *important* one, and this did not ask it.

So one result, not three. The defect count is the only one with an answer key
that does not depend on the judge's taste.
