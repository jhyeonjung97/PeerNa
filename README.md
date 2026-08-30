# AI Peer Reviewer

Referee-style review of a scientific manuscript. Reads the paper through four
separate lenses, verifies its citations against the live web, and consolidates
everything into a journal referee report.

## Install

```bash
pip install -r requirements.txt

mkdir -p ~/.config/ai-peer-reviewer
cp .env.example ~/.config/ai-peer-reviewer/.env
chmod 600 ~/.config/ai-peer-reviewer/.env
# then put your key in that file
```

Get a key at https://platform.claude.com/settings/keys. `export
ANTHROPIC_API_KEY=...` in your shell works too and takes precedence.

**Whoever runs the tool pays for their own reviews.** Nothing is billed to
whoever gave you the code.

> **Do not put your key in a `.env` inside this project** if the project lives in
> Google Drive, Dropbox, or iCloud. It will be uploaded, and it will travel with
> anyone you share the folder with. `.gitignore` does not prevent this — it hides
> the file from git, not from the sync client. The tool warns you if it finds a
> key in a synced location.

## Use

```bash
# Review your own draft before submitting
python -m ai_peer_reviewer.cli manuscript.pdf --out review.md

# Produce a report for a paper you are refereeing
python -m ai_peer_reviewer.cli manuscript.pdf --mode referee --out report.md

# See what it will cost first
python -m ai_peer_reviewer.cli manuscript.pdf --dry-run

# As a formatted page you can read or print to PDF
python -m ai_peer_reviewer.cli manuscript.pdf --out review.html
```

| Flag | Effect |
|---|---|
| `--mode self` | Default. Finds what a hostile referee would find, while there is time to fix it. |
| `--mode referee` | A report for the editor and authors. Prompts a confidentiality check first. |
| `--model fable\|opus\|sonnet\|haiku\|luna` | Anthropic models, or `luna` for OpenAI's GPT-5.6 Luna. |
| `--no-web-search` | Skip citation verification. Cheaper, but all citation comments come back `unverified`. |
| `--html` | Render a self-contained HTML page instead of Markdown. A `.html` extension on `--out` does the same. |
| `--notes` | Also write the raw per-pass findings, in case consolidation dropped something. |
| `--fresh` | Ignore saved passes from an interrupted run and start over. |
| `--dry-run` | Estimate cost and stop. |

## Two things to know

**Send PDFs.** A referee who cannot see the figures cannot review the paper, and
text extraction destroys them. PDFs are sent whole, so the model sees the
rendered pages. DOCX and LaTeX are converted to text with images re-attached
separately — workable, but figure comments will be less precise. If you have a
compiled PDF, use it.

On the OpenAI side there is one wrinkle. PDFs go as `input_file`, which is what
the flagship models take, but not every model in the family documents that
content type — Luna lists its input modalities as "text, image" with no mention
of files. If a request is rejected for that reason, the backend renders each page
to an image and resends automatically, and says so in the report's warnings. The
figures reach the model either way; the difference is that text is then read from
the rendering rather than from the PDF's own text layer.

**Refereeing someone else's manuscript is a confidentiality question.** Elsevier,
ACS, Springer Nature, and NIH among others restrict or prohibit uploading
manuscripts under review to AI tools. That is about the agreement you made with
the journal, not about the API provider's data policy. `--mode referee` will
stop and make you confirm you have checked.

## How it works

The manuscript is sent once and cached, then read four times with a different
job each time:

1. **Claims vs. evidence** — does the data support what the paper asserts?
2. **Methods and reproducibility** — could someone repeat this from what is written?
3. **Figures and tables** — do the figures show what they claim to show?
4. **Framing and prior work** — is the novelty claim accurate, and do the load-bearing citations actually exist? (uses web search)

A fifth call consolidates the four into one report, merging duplicates and
sorting concerns by whether they threaten the conclusions.

Splitting the read is the main quality lever. One "review this paper" prompt
returns the same six generic comments for every paper; a pass with one job
returns specific ones. Prompt caching is what makes it affordable — passes after
the first read the manuscript at about a tenth of the input cost.

### If a run fails partway

Each pass is written to `~/.cache/ai-peer-reviewer/runs/` the moment it returns.
Run the same command again and it picks up where it stopped — the completed
passes are reused from disk and only the remaining work is re-requested. The
report's warnings say which passes were reused, and the reported cost includes
what the earlier attempt spent. The saved passes are deleted once a report is
produced; `--fresh` discards them instead of resuming.

A run is keyed by the manuscript's contents, the mode, and the model, so editing
the manuscript or switching model starts fresh on its own.

Rate limits are the common cause of a partial run, especially on a new API
account with a low usage tier. Both SDKs honour `Retry-After` and retry several
times, but a manuscript large enough to exceed your per-minute token allowance in
a single request will not succeed by retrying — check your limits at
https://platform.openai.com/settings/organization/limits or
https://platform.claude.com/settings/limits.

### Citations

The literature pass searches the web to check the citations the paper leans on,
and each result is labelled `verified`, `not_found`, `misattributed`, or
`unverified` in the report. Fabricated references are the characteristic failure
mode of AI review, so nothing is reported as verified unless a search confirmed
it. Treat `unverified` entries as leads, not findings.

### Cost

Roughly per paper, with caching:

| Model | Provider | Estimate |
|---|---|---|
| Fable 5 | Anthropic | $1.00 – $3.00 |
| Opus 5 | Anthropic | $0.50 – $1.50 |
| Sonnet 5 | Anthropic | $0.30 – $0.90 |
| Haiku 4.5 | Anthropic | $0.10 – $0.30 |
| GPT-5.6 Luna | OpenAI | $0.03 – $0.10 |

Actual usage and cost is printed after every run.

Two caveats on the cheap end. Haiku and Luna are bottom-tier models, and peer
review is the kind of task where model capability shows most — expect generic
comments rather than the subtle methodological catch you actually wanted. And
Luna's estimate is less trustworthy than the others: OpenAI has no token-counting
endpoint, so the preview is computed locally, and its prompt caching is automatic
with no lifetime control, so the manuscript may be re-read at full price on some
passes rather than reused.

Fable 5 cannot run under zero data retention — requests are kept for 30 days.
That matters if you are refereeing. Haiku has a 200K context window and caps out
at 100 PDF pages; the others hold 1M+.

## Building a benchmark corpus

Nature Communications publishes referee reports for papers whose authors opted
into transparent peer review. That makes it possible to check what this tool
says about a paper against what the real reviewers said.

```bash
# See what matches before downloading
python -m ai_peer_reviewer.fetch "electrocatalysis" --limit 20 --list

# Fetch article text + referee reports into ./corpus
python -m ai_peer_reviewer.fetch "M-N-C oxygen reduction" --limit 20 --out corpus

# Referee reports only
python -m ai_peer_reviewer.fetch "" --year 2025 --limit 50 --reviews-only
```

Everything routes through [Europe PMC](https://europepmc.org/), which
distributes the open-access corpus for automated use, with a one-second pause
between requests. The referee report is found by its JATS label rather than by
guessing filenames — the supplementary file numbering differs from paper to
paper, so `MOESM6` on one paper is `MOESM4` on the next.

**Two limitations worth knowing before you plan around this.**

Article text comes from Europe PMC's JATS, not the publisher's PDF, so
**figures arrive as captions only**. nature.com serves a bot challenge to
automated PDF requests; getting around that would violate their terms and would
get the IP blocked. The figures pass will correctly report that it cannot see
the figures. If you want a full-fidelity PDF for a particular paper, download
that one by hand from the article page.

Not every paper has a report — transparent peer review is opt-in. The run
summary says how many of the matched papers actually had one.

## Layout

```
ai_peer_reviewer/
├── cli.py       entry point, cost preview, confidentiality gate
├── config.py    API key resolution, cloud-sync warning
├── models.py    per-model capability registry (provider, effort, limits, pricing)
├── loader.py    manuscript → provider-neutral parts
├── parts.py     the neutral content types
├── backends.py  Anthropic and OpenAI request/response handling
├── passes.py    the four lenses and the system prompts
├── schema.py    the review structure the model fills
├── review.py    pass orchestration and consolidation
├── render.py    structure → Markdown or self-contained HTML
```

Three seams, so a change lands in one place:

- **Adding a model** is one entry in `models.py`.
- **Adding a file format** is one function in `loader.py` emitting neutral parts.
- **Adding a provider** is one class in `backends.py`.

`review.py` never learns which provider it is talking to.

### On caching

Anthropic caching is explicit: a breakpoint is set on the manuscript with a
one-hour lifetime, so all five passes are guaranteed to reuse it. OpenAI caching
is automatic and prefix-based with no lifetime control, so the manuscript is
placed first in the request to give the prefix the best chance of matching, and
that is the most that can be done. This is why the Luna cost estimate carries a
caveat the others do not.
