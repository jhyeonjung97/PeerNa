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

While `MONO_ROURTER_API_KEY` still has credit and the OpenAI account does not,
set that instead and `AI_PEER_REVIEWER_MODEL=r-luna`. It expires at the end of
August 2026 — see `TODO.md` §7 for the switch back, which is also the better
home for a manuscript.

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

## Before the URL goes to anyone else

**There is no authentication.** Anyone with the address can spend the key. Two
things from `TODO.md` §6 are not optional once the address is shared:

- **A spending ceiling on the provider account.** Set it before, not after. Sign-in
  limits what one person can spend; the ceiling limits what a mistake or a script
  can spend.
- **Sign-in**, so that "three free reviews" is countable at all.

And the confidentiality note that the page already shows applies with more force
once other people's manuscripts are involved: they pass through whichever
provider the key belongs to, and through the router as well if a routed model is
selected. See the caution beside the model menu.

## The free plan

Render's free plan sleeps an idle service. A review in flight when it sleeps is
lost, and reviews take minutes, so `render.yaml` asks for `starter`. Free is fine
for looking at the page and nothing else.
