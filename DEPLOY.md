# Deploying

`git push` → Render builds the Dockerfile and restarts the service. That is the
whole loop; `render.yaml` holds the rest.

## Why a container and not Vercel

A review runs for minutes. Measured over twenty runs: median 425 s, 90th
percentile 536 s, one at 2303 s when the provider rate-limited it. The HTTP
request itself returns immediately with a job id and the work continues in a
background thread — which is exactly what a serverless platform ends, because it
freezes the instance once the response is sent.

Duration is not even the hardest part. Job state lives in a dict in the process
(`serve.py: JOBS`), and the browser polls for it; on serverless the poll can
reach a different instance that has never heard of the job. Uploads, checkpoints
and job history are files on local disk, and `/tmp` is not shared between
invocations. Moving to Vercel means Redis for the jobs, blob storage for the
files, somewhere to host the 381 MB corpus index, and a worker that is not a
Vercel function — at which point a container is running anyway.

## The address

`peerna.onrender.com`, from the service name in `render.yaml`. Render's
subdomains are global, so if someone has taken `peerna` it will append a suffix —
rename the service and the URL follows.

No custom domain for now. It is not a blocker for a private trial or for handing
the tool to a few people, and a domain can be pointed at the same service later
without changing anything here.

## First deploy

1. Push to GitHub. In Render, **New → Blueprint**, point it at the repository;
   `render.yaml` describes the service, the disk and the environment.
2. Set `OPENAI_API_KEY` in the Render dashboard. It is `sync: false` in the
   blueprint, so it never enters the repository or a build log.
3. Wait for the first build. Around five minutes, most of it numpy and PyMuPDF.

The service starts with no corpus index. That is fine — the precedent pass is
skipped when there is no index and the review is complete without it, just
without the field-specific layer. To add it, run `peerna-harvest` against the
mounted disk, or copy an index you have already built into `/data/.cache/`.

## What lives where

`HOME=/data` in the container, and everything the package keeps hangs off
`Path.home()`, so the mounted disk holds all of it:

    /data/.cache/ai-peer-reviewer/index/    corpus index and embeddings (381 MB)
    /data/.cache/ai-peer-reviewer/jobs/     finished reviews
    /data/.cache/ai-peer-reviewer/runs/     checkpoints for interrupted reviews

Without the disk these sit in the image layer and vanish on every deploy, taking
every review anyone has run with them.

## The corpus index

The index is built on the laptop and published as a release of this repository:

    python -m tools.corpus_bundle corpus-2026-10 --publish

A server with `AI_PEER_REVIEWER_CORPUS` set to that tag and
`AI_PEER_REVIEWER_CORPUS_TOKEN` set to a read-only token fetches it at start, in
the background; reviews started meanwhile run without precedents. The token is
a fine-grained one (GitHub → Settings → Developer settings → Personal access
tokens → Fine-grained tokens), limited to this repository with Contents:
Read-only. The production server keeps what it fetched on its disk and fetches
again only when the tag changes; the development server has no disk and fetches
at every start. The laptop's own database, the one with the review files in it,
is never overwritten.

## Sign-in

Off until `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set; the service
behaves exactly as before without them. With them, running a review requires a
Google account, the review history shows only your own, and a report can be
fetched only by the person who ran it.

Google rather than emailed links, and the reason decided it: a magic link has to
be delivered, delivery needs a sending service, and a sending service will not
mail strangers until you have verified a domain you own. Google needs no domain.

In the Google Cloud console:

1. Create a project.
2. **OAuth consent screen** → External. Leave it in **Testing** and add each
   person's address under Test users. Testing allows 100, which is more than a
   trial needs and skips review entirely.
3. **Credentials → OAuth client ID → Web application.**
4. Authorized redirect URI: `https://peerna.onrender.com/auth/callback` — exactly,
   including the scheme.
5. Put the client ID and secret in Render.

The scopes are `openid email`, both non-sensitive, so nothing here goes near the
review queue for sensitive scopes. Publishing the app later removes the 100-user
cap and is the point at which the spending ceiling stops being advice.

Nobody is turned away by email domain. Two limits already bound the damage: the
testing cap on how many accounts exist at all, and the ceiling on the provider
account. A domain rule would add a third and exclude the European institutions
that do not use Google.

## No key entry in the interface

Removed. The page used to offer a panel that wrote a provider key into the
server's config, which was sensible when the server was your own laptop and
became a hole the moment it was not: anyone who knew the address could replace
the credentials every review was billed to.

There is one key now and it belongs to whoever runs the service, set in the
platform's environment. Locally that is the `.env`, and the startup message
names both the file and the variables when neither is there.

## Development and release

Two services, two branches (`render.yaml`):

| | branch | deploys | who can sign in |
|---|---|---|---|
| `peerna` | `main` | only on release | anyone |
| `peerna-dev` (free plan) | `dev` | every push | the accounts in `AI_PEER_REVIEWER_ONLY` |

Work happens on `dev`:

    ./tests/deploy.sh      # tests, then push dev; peerna-dev rebuilds

A release is made only when the operator asks for one:

    ./tests/release.sh     # tests on 3.10 and 3.12, asks if a review is running,
                           # pushes dev to main, starts the real deploy

`peerna` has autoDeploy off, so a push to `main` on its own deploys nothing.
The release script triggers the deploy through a Render deploy hook if
`RENDER_DEPLOY_HOOK=` is set in `~/.config/ai-peer-reviewer/.env`
(peerna → Settings → Deploy Hook); otherwise it says to press Manual Deploy.

### Setting up peerna-dev

1. Sync the Blueprint in the Render dashboard; it offers to create peerna-dev.
2. In peerna-dev → Environment set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
   `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `AI_PEER_REVIEWER_CONTACT`,
   and `AI_PEER_REVIEWER_ONLY` and `AI_PEER_REVIEWER_UNLIMITED` to the
   operator's address.
3. In Google Cloud Console → APIs & Services → Credentials → the OAuth client,
   add `https://peerna-dev.onrender.com/auth/callback` to the authorised
   redirect URIs. Without it Google refuses the sign-in.

With `AI_PEER_REVIEWER_ONLY` set and the Google settings missing, the server
admits nobody rather than everybody: on any server without Google settings the
visitor is treated as the local operator, which on a development server would
let anyone spend the key.

The free plan has no disk, so reviews on peerna-dev do not survive a restart
and there is no corpus index there. Work that needs the corpus is tried on the
laptop.

## Free reviews

Three per Google account, then a 402. `AI_PEER_REVIEWER_FREE_REVIEWS` changes
the number; the count lives in `/data/.cache/ai-peer-reviewer/quota.json` on the
mounted disk, so it survives a deploy.

`AI_PEER_REVIEWER_UNLIMITED` takes a comma-separated list of addresses with no
limit — the operator's own belongs there, since they pay for every review on the
service and testing it is not a privilege to ration three at a time. In the
environment rather than the source, so that changing it is not a deploy and a
public repository carries no list of addresses.

Counted apart from the job history on purpose: that is capped at fifty files and
sweeps the oldest, so counting finished jobs would hand somebody a fresh
allowance once enough other people had used the service — the quota would leak
in exactly the conditions that make one matter.

Only completed reviews count. A run that failed, or that a deploy interrupted,
is not one anybody received. That leaves a way to run indefinitely by abandoning
every review just before it finishes, which is the better thing to be exposed to
than invoicing people for our own outages.

Nothing takes payment yet. When the free reviews are gone the button says so and
the footer says who to write to.

## Before the URL goes to anyone else

**There is no authentication.** Anyone with the address can spend the key. Two
things from `TODO.md` §6 are not optional once the address is shared:

- **A spending ceiling on the provider account.** Set it before, not after. Sign-in
  limits what one person can spend; the ceiling limits what a mistake or a script
  can spend.
- **Sign-in**, so that "three free reviews" is countable at all.

And the confidentiality note that the page already shows applies with more force
once other people's manuscripts are involved: they pass through whichever
provider the key belongs to. See the caution beside the model menu.

## The free plan

Render's free plan sleeps an idle service. A review in flight when it sleeps is
lost, and reviews take minutes, so `render.yaml` asks for `starter`. Free is fine
for looking at the page and nothing else.
