# A container rather than a serverless function, because a review runs for
# minutes rather than seconds. Measured over twenty runs: median 425 s, 90th
# percentile 536 s, and one at 2303 s when the provider rate-limited it. The
# request itself returns immediately with a job id — the work happens in a
# background thread — and a platform that freezes the instance once the response
# is sent would kill that thread. So the process has to stay up.

FROM python:3.12-slim

# PyMuPDF ships its own binaries; these are what it links against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so that editing the package does not reinstall 124 MB of
# numpy and PyMuPDF on every deploy.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ai_peer_reviewer ./ai_peer_reviewer

# Everything the package keeps hangs off Path.home() — the corpus index, the
# checkpoints and the job history — so pointing HOME at the mounted disk moves
# all three at once. Without it they would live in the image layer: the index is
# 381 MB and would be rebuilt from nothing on every deploy, taking every review
# anyone had run with it.
#
# API keys are read from the environment. config.load() fills gaps from a .env
# and never overrides a real variable, so setting them on the platform works and
# no secret goes into the image.
ENV HOME=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8765

# 0.0.0.0 because the platform's router is outside the container. There is no
# authentication in front of this yet — see TODO §6 before the URL is given to
# anyone who is not you.
CMD ["python", "-m", "ai_peer_reviewer.serve", "--host", "0.0.0.0", "--no-browser"]
