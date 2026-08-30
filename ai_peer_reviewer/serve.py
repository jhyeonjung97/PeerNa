"""A local web UI for dropping in a manuscript.

Deliberately a local server rather than a page that calls the API from the
browser. Three reasons: the review logic already exists in Python and is reused
here unchanged, the API key stays in the config file instead of being pasted
into a web page, and the manuscript never travels anywhere except to the model
provider you picked.

It binds to the loopback interface only. Reviews cost money to run, so the
server must not be reachable from the network.
"""

from __future__ import annotations

import base64
import json
import os
import mimetypes
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import backends, checkpoint as checkpoint_mod, config, loader, models, notify
from .passes import PASSES, system_prompt
from .render import render, render_html
from .review import Reviewer, total_steps

WEB_ROOT = Path(__file__).parent / "web"
MAX_UPLOAD_BYTES = 40 * 1024 * 1024

#: job id -> progress and result. Bounded by hand; this is a single-user tool.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS = 20

#: Finished reviews are also written here. Keeping them only in memory meant a
#: browser refresh — or a server restart — threw away work that took ten minutes
#: and real money to produce.
from .history import JOB_DIR, KEEP as KEEP_JOBS


def _persist(job_id: str, job: dict) -> None:
    try:
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        keep = dict(job)
        keep["job_id"] = job_id
        (JOB_DIR / f"{job_id}.json").write_text(json.dumps(keep), encoding="utf-8")
        stale = sorted(JOB_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in stale[:-KEEP_JOBS]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # a failed cache write must not lose the review itself


def _load_persisted(job_id: str) -> dict | None:
    path = JOB_DIR / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


#: Whether this process is serving anyone but the person who started it.
#: Set from the bind address: 127.0.0.1 can only be reached from this machine,
#: anything else is on a network. Two things depend on it, and the second is not
#: cosmetic — a visitor must not be able to replace the operator's credentials.
HOSTED = False


#: Roughly what the consistency checks add, measured end to end on a paper with
#: 46 references. Almost independent of manuscript length.
CHECK_SECONDS = 90


def estimated_seconds(tokens: int, web_search: bool) -> int | None:
    """How long this will take, learned from runs that already happened.

    A formula fitted to two data points would be a guess dressed up as a number.
    Past runs of a similar size are real evidence, so they are used when they
    exist; only the first few runs fall back to a rough rate.
    """
    similar = []
    for job in _finished_jobs():
        prior = job.get("tokens") or 0
        if not job.get("duration") or not prior:
            continue
        if job.get("web_search") != web_search:
            continue
        if 0.5 * tokens <= prior <= 2.0 * tokens:
            similar.append(job["duration"] * tokens / prior)
    if len(similar) >= 2:
        return int(statistics.median(similar))
    # Fallback rate, measured on a 27-page manuscript: ~6 ms per input token
    # plus fixed overhead, and a further 90 seconds for the consistency checks,
    # which are a network call per reference and two model calls and so barely
    # depend on the length of the paper.
    rough = 30 + CHECK_SECONDS + tokens * 0.006
    return int(rough if web_search else rough * 0.75)


def _finished_jobs() -> list[dict]:
    if not JOB_DIR.is_dir():
        return []
    out = []
    for path in JOB_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("status") == "done":
            out.append(data)
    return out


def mark_orphans() -> int:
    """Flag jobs that were running when the server last stopped.

    Their worker thread died with the process, so they will never finish. Saying
    so is better than showing a progress bar that will not move — and the passes
    they completed are still in the checkpoint, so re-running resumes.
    """
    fixed = 0
    for path in JOB_DIR.glob("*.json") if JOB_DIR.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("status") in ("running", "queued"):
            data["status"] = "error"
            data["error"] = (
                "The server stopped while this review was running, so it was "
                "interrupted. Completed passes were saved — starting the same "
                "manuscript again resumes from where it left off."
            )
            try:
                path.write_text(json.dumps(data), encoding="utf-8")
                fixed += 1
            except OSError:
                pass
    return fixed


def _recent_jobs(limit: int = 15) -> list[dict]:
    if not JOB_DIR.is_dir():
        return []
    rows = []
    for path in sorted(JOB_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({
            "job_id": data.get("job_id", path.stem),
            "filename": data.get("filename", "?"),
            "status": data.get("status", "?"),
            "cost": data.get("cost"),
            "finished": int(path.stat().st_mtime),
            # Carried so the list can show a running job's progress in place,
            # rather than needing a separate panel for whatever is in flight.
            "step": data.get("step"),
            "total": data.get("total"),
            "stage": data.get("stage"),
            "started_at": data.get("started_at"),
            "duration": data.get("duration"),
        })
    return rows


def _job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    return job if job is not None else _load_persisted(job_id)


def _update(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)
            snapshot = dict(JOBS[job_id])
        else:
            return
    # Written on every change, not only at the end. A job that exists only in
    # memory disappears if the server restarts, taking with it the fact that it
    # ever ran — and the user is left staring at a page that forgot everything.
    _persist(job_id, snapshot)


#: LibreOffice filter names, and the MIME type each format is served as.
FORMATS = {
    "html": "text/html",
    "md": "text/markdown",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def convert(job: dict, fmt: str) -> bytes | None:
    """Render the stored report as PDF or Word.

    PDFs are printed by a headless browser so they match what the page shows;
    Word files are built from the report structure. Neither goes through
    LibreOffice's HTML importer, which flattens the layout.
    """
    from . import export

    if fmt == "pdf":
        return export.to_pdf(job["html"])
    if fmt == "docx":
        stored = job.get("report")
        if not stored:
            return None  # job predates structured storage
        from .schema import RefereeReport

        return export.to_docx(
            RefereeReport.model_validate(stored),
            job.get("filename", "manuscript"),
            job.get("model_label", ""),
            job.get("warnings", []),
        )
    return None


def _verify_key(provider: str, key: str) -> None:
    """Ask the provider whether this key is real. Raises with a readable reason."""
    if not key:
        raise ValueError("Paste a key first.")
    if provider == "openai":
        import openai

        client = openai.OpenAI(api_key=key, max_retries=0, timeout=30.0)
        try:
            client.models.list()
        except openai.AuthenticationError:
            raise ValueError("OpenAI rejected that key.") from None
        except openai.APIConnectionError:
            raise ValueError("Could not reach OpenAI. Check your network.") from None
    else:
        import anthropic

        client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=30.0)
        try:
            client.models.list()
        except anthropic.AuthenticationError:
            raise ValueError("Anthropic rejected that key.") from None
        except anthropic.APIConnectionError:
            raise ValueError("Could not reach Anthropic. Check your network.") from None


#: upload id -> (filename, path on disk). The browser sends a manuscript once
#: and refers to it by id after that; re-encoding a 3 MB PDF to base64 for every
#: cost estimate is what made the preview feel slow.
UPLOADS: dict[str, tuple[str, Path]] = {}
MAX_UPLOADS = 10


def _remember(filename: str, data: bytes) -> str:
    path = _spill_to_disk(filename, data)
    upload_id = uuid.uuid4().hex
    with JOBS_LOCK:
        for stale in list(UPLOADS)[: max(0, len(UPLOADS) - MAX_UPLOADS + 1)]:
            _, old = UPLOADS.pop(stale)
            old.unlink(missing_ok=True)
        UPLOADS[upload_id] = (filename, path)
    return upload_id


def _spill_to_disk(filename: str, data: bytes) -> Path:
    """The loader works on paths, so uploads land in a temp file."""
    suffix = Path(filename).suffix or ".pdf"
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.write(data)
    handle.close()
    return Path(handle.name)


def _run_review(job_id: str, path: Path, spec, web_search: bool,
                supplementary: Path | None = None) -> None:
    started = time.time()
    try:
        manuscript = loader.load(path, spec)
        backend = backends.build(spec)

        # Checkpointed, which matters more here than on the command line. A
        # deployed service restarts for reasons nobody chose — a new commit, an
        # environment variable saved, the platform moving the instance — and each
        # of those kills the worker thread mid-review. mark_orphans() has always
        # claimed the completed passes were still on disk; they were not, because
        # this was the one path that never passed a checkpoint. Keyed on the
        # manuscript, so re-uploading the same file resumes rather than restarts.
        resume = checkpoint_mod.Checkpoint(
            checkpoint_mod.run_id(path.read_bytes(), "referee", spec.id)
        )
        reviewer = Reviewer(backend=backend, mode="referee",
                            use_web_search=web_search, checkpoint=resume)
        # The supplementary file is not sent to the model — it is usually larger
        # than the manuscript and most of it is data the passes have no use for.
        # It is read by the consistency checks, which is where it earns its place:
        # every "Supplementary Fig. N" in the manuscript is otherwise unverifiable
        # and has to be skipped.
        reviewer.supplementary = supplementary

        total = total_steps()

        def progress(step: int, of: int, title: str) -> None:
            _update(job_id, step=step, total=of, stage=title)

        _update(job_id, status="running", step=0, total=total, stage="Starting")
        result = reviewer.review(manuscript, progress=progress)
        if reviewer.reused:
            _update(job_id, resumed=list(reviewer.reused))

        display_name = _job(job_id).get("filename", manuscript.name)
        _update(
            job_id,
            status="done",
            step=total,
            stage="Complete",
            html=render_html(result, display_name, "referee", spec.label),
            markdown=render(result, display_name, "referee", spec.label),
            report=result.report.model_dump(),
            model_label=spec.label,
            cost=round(result.usage.cost(spec), 4),
            warnings=list(dict.fromkeys(result.warnings)),
            duration=int(time.time() - started),
        )
        _persist(job_id, _job(job_id) or {})
        notify.announce(
            display_name,
            f"{len(result.report.comments)} comments · "
            f"{result.report.recommendation.replace('_', ' ')} · "
            f"${result.usage.cost(spec):.2f}",
            subtitle="Review finished",
        )
    except SystemExit as exc:
        _update(job_id, status="error", error=str(exc))
        _persist(job_id, _job(job_id) or {})
    except Exception as exc:  # surfaced in the UI rather than only the console
        _update(job_id, status="error", error=f"{type(exc).__name__}: {exc}")
        _persist(job_id, _job(job_id) or {})
    finally:
        path.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "AIPeerReviewer"

    def log_message(self, fmt, *args):  # quieter console
        pass

    # -- helpers ---------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD_BYTES:
            raise ValueError("Upload too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            # A platform polls this every few seconds to decide whether the
            # service is alive. Answering with the full page would send the
            # whole interface each time and, worse, would report healthy on a
            # process that can serve a file but has lost its credentials. This
            # checks the two things a review actually needs.
            reviews = [
                name for name in models.ALIASES
                if config.has_credentials(models.REGISTRY[name].provider)
            ]
            healthy = bool(reviews) and WEB_ROOT.is_dir()
            return self._json(
                200 if healthy else 503,
                {
                    "ok": healthy,
                    "models": reviews,
                    "running": sum(
                        1 for job in JOBS.values() if job.get("status") == "running"
                    ),
                },
            )

        if path in ("/", "/index.html"):
            return self._serve_file(WEB_ROOT / "index.html")

        if path == "/logo.svg":
            return self._serve_file(WEB_ROOT / "logo.svg")

        if path == "/api/models":
            return self._json(
                200,
                {
                    "hosted": HOSTED,
                    "models": [
                        {
                            "key": name,
                            "superseded": models.preferred(name) != name,
                            "label": models.REGISTRY[name].label,
                            "provider": models.REGISTRY[name].provider,
                            "note": models.QUALITY_NOTE[name],
                            "caution": models.retention_problem(
                                models.REGISTRY[name], config.zero_retention()
                            ),
                            "ready": config.has_credentials(
                                models.REGISTRY[name].provider
                            ),
                            "hint": config.key_hint(
                                models.REGISTRY[name].provider
                            ),
                        }
                        for name in models.ALIASES
                    ],
                    "default": config.configured_model() or models.DEFAULT_MODEL,
                },
            )

        if path == "/api/jobs":
            if HOSTED:
                # The history is one list for the whole process, and there is no
                # sign-in to divide it by. Served on a shared deployment it hands
                # every visitor the filenames of everyone else's manuscripts and
                # the job ids that fetch their reports — for papers that are, by
                # the nature of this tool, unpublished and under review.
                #
                # A job id is a random 32-hex string, so a review remains
                # reachable by whoever ran it and holds the link. It is the
                # listing that leaks, and the listing is what stops.
                return self._json(200, {"jobs": [], "private": True})
            return self._json(200, {"jobs": _recent_jobs()})

        if path.startswith("/api/download/"):
            job_id = path.rsplit("/", 1)[-1]
            fmt = urllib.parse.parse_qs(
                self.path.split("?", 1)[1] if "?" in self.path else ""
            ).get("format", ["pdf"])[0]
            return self._download(job_id, fmt)

        if path.startswith("/api/job/"):
            job_id = path.rsplit("/", 1)[-1]
            job = _job(job_id)
            if not job:
                return self._json(404, {"error": "No such job."})
            return self._json(
                200,
                {
                    k: v
                    for k, v in job.items()
                    if k in ("status", "step", "total", "stage", "error", "cost",
                             "warnings", "html", "started_at", "duration",
                             "tokens", "web_search", "filename", "supplementary",
                             "resumed")  # report/markdown
                             # are large and only needed by the download endpoint
                },
            )

        self._json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            payload = self._read_json()
        except Exception as exc:
            return self._json(400, {"error": str(exc)})

        if path == "/api/upload":
            try:
                filename, data = self._decode_upload(payload)
            except Exception as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(
                200, {"upload_id": _remember(filename, data), "filename": filename}
            )
        if path == "/api/key":
            if HOSTED:
                # On a shared deployment this endpoint would let anyone who knows
                # the address overwrite the key every review is billed to. There
                # is no sign-in yet, so the only safe answer is no.
                return self._json(
                    403,
                    {"error": "This deployment is shared. Its API key is set by "
                              "whoever runs it, not from this page."},
                )
            return self._save_key(payload)
        if path == "/api/estimate":
            return self._estimate(payload)
        if path == "/api/review":
            return self._review(payload)
        self._json(404, {"error": "Not found."})

    # -- handlers --------------------------------------------------------

    def _resolve(self, payload: dict) -> tuple[str, Path]:
        """Return (filename, path), accepting either an upload id or raw data."""
        upload_id = payload.get("upload_id")
        if upload_id:
            found = UPLOADS.get(upload_id)
            if not found:
                raise ValueError("That upload expired — choose the file again.")
            return found
        filename, data = self._decode_upload(payload)
        return filename, _spill_to_disk(filename, data)

    def _decode_upload(self, payload: dict) -> tuple[str, bytes]:
        filename = payload.get("filename") or "manuscript.pdf"
        raw = payload.get("data") or ""
        if "," in raw[:100] and raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        data = base64.b64decode(raw)
        if not data:
            raise ValueError("No file received.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("File is too large.")
        return filename, data

    def _save_key(self, payload: dict) -> None:
        """Check a key works before storing it.

        Saving an unverified key just moves the failure to five minutes into a
        review, by which point the user has stopped watching.
        """
        provider = payload.get("provider")
        key = (payload.get("key") or "").strip()
        if provider not in models.CREDENTIAL_ENV:
            return self._json(400, {"error": "Unknown provider."})
        try:
            _verify_key(provider, key)
        except Exception as exc:
            return self._json(400, {"error": str(exc)})
        try:
            config.save_key(provider, key)
        except (OSError, ValueError) as exc:
            return self._json(400, {"error": f"Could not save the key: {exc}"})
        return self._json(
            200,
            {
                "ok": True,
                "provider": provider,
                "hint": config.key_hint(provider),
                "path": str(config.CONFIG_ENV),
            },
        )

    def _estimate(self, payload: dict) -> None:
        try:
            asked = payload.get("model") or config.configured_model() or models.DEFAULT_MODEL
            spec = models.resolve(models.preferred(asked))
            if not config.has_credentials(spec.provider):
                env = models.CREDENTIAL_ENV[spec.provider]
                return self._json(
                    400,
                    {"error": f"{spec.label} needs {env} set in {config.CONFIG_ENV}."},
                )
            filename, path = self._resolve(payload)
            manuscript = loader.load(path, spec)
            backend = backends.build(spec)
            tokens = backend.count_input_tokens(system_prompt(), manuscript.parts)

            # Calls that carry the manuscript and calls that do not are priced
            # separately. The lenses, the field check and the synthesis all send
            # the paper; the cutting pass and the two verification calls send
            # only text they were handed, so charging them a manuscript each —
            # which the old estimate did by counting calls alone — overstated
            # the total by more than the checks actually cost.
            heavy = len(PASSES) + 2          # lenses, field expectations, synthesis
            light = 4                        # cutting, deciding, cross-references, citations
            rate = spec.input_per_mtok / 1_000_000
            out_rate = spec.output_per_mtok / 1_000_000
            estimate = (
                tokens * 1.25 * rate                     # first read, uncached
                + tokens * (heavy - 1) * 0.1 * rate      # the rest, from cache
                + 8_000 * light * rate                   # a report, not a paper
                + 4_000 * (heavy + light) * out_rate
            )
            calls = heavy + light
            self._json(
                200,
                {
                    "tokens": tokens,
                    "estimate": round(estimate, 3),
                    "calls": calls,
                    "notes": manuscript.notes,
                    "eta": estimated_seconds(
                        tokens, not payload.get("no_web_search")
                    ),
                },
            )
        except SystemExit as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(400, {"error": f"{type(exc).__name__}: {exc}"})

    def _review(self, payload: dict) -> None:
        try:
            asked = payload.get("model") or config.configured_model() or models.DEFAULT_MODEL
            spec = models.resolve(models.preferred(asked))

            if not config.has_credentials(spec.provider):
                env = models.CREDENTIAL_ENV[spec.provider]
                return self._json(
                    400,
                    {"error": f"{spec.label} needs {env} set in {config.CONFIG_ENV}."},
                )

            filename, source = self._resolve(payload)
            # The worker deletes what it is handed, so give it a copy and leave
            # the remembered upload intact for a re-run.
            path = _spill_to_disk(filename, source.read_bytes())

            si_path = None
            si_id = payload.get("supplementary_id")
            if si_id:
                found = UPLOADS.get(si_id)
                # A stale supplementary id costs a check, not the review, so it
                # is dropped quietly rather than refused.
                if found:
                    si_path = _spill_to_disk(found[0], found[1].read_bytes())
            job_id = uuid.uuid4().hex

            with JOBS_LOCK:
                for stale in list(JOBS)[: max(0, len(JOBS) - MAX_JOBS + 1)]:
                    JOBS.pop(stale, None)
                JOBS[job_id] = {
                    "status": "queued",
                    "step": 0,
                    "total": total_steps(),
                    "stage": "Queued",
                    "filename": filename,
                    "started_at": time.time(),
                    "web_search": not payload.get("no_web_search"),
                    "supplementary": found[0] if si_path else None,
                }

            try:
                manuscript = loader.load(path, spec)
                backend = backends.build(spec)
                _update(job_id, tokens=backend.count_input_tokens(
                    system_prompt(), manuscript.parts))
            except Exception:
                pass  # an ETA is a nicety; never let it block the review

            threading.Thread(
                target=_run_review,
                args=(job_id, path, spec, not payload.get("no_web_search"), si_path),
                daemon=True,
            ).start()
            self._json(200, {"job_id": job_id})
        except Exception as exc:
            self._json(400, {"error": f"{type(exc).__name__}: {exc}"})

    def _download(self, job_id: str, fmt: str) -> None:
        if fmt not in FORMATS:
            return self._json(400, {"error": f"Unknown format {fmt!r}."})
        job = _job(job_id)
        if not job or not job.get("html"):
            return self._json(404, {"error": "No report for that job."})

        stem = Path(job.get("filename", "review")).stem
        if fmt == "html":
            body = job["html"].encode()
        elif fmt == "md":
            body = (job.get("markdown") or "").encode()
            if not body:
                return self._json(404, {"error": "Markdown is not stored for this run."})
        else:
            data = convert(job, fmt)
            if data is None:
                return self._json(
                    503,
                    {"error": "Could not produce that format. PDF needs Chrome or "
                              "LibreOffice; Word is only available for reviews run "
                              "after this feature was added."},
                )
            body = data

        self.send_response(200)
        self.send_header("Content-Type", FORMATS[fmt])
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{stem}-review.{fmt}"'
        )
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            return self._json(404, {"error": f"Missing {path.name}."})
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), kind)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="ai-peer-review-web",
        description="Serve the manuscript drop-in page on localhost.",
    )
    # A host outside the container cannot reach 127.0.0.1 inside it, and a
    # platform assigns the port rather than letting the process pick one. Both
    # default to the safe local values, so nothing changes when run by hand.
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT") or 8765))
    parser.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1",
                        help="0.0.0.0 to accept connections from outside this "
                             "machine. There is no authentication yet, so only "
                             "do that where something else is guarding the door.")
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open a browser window"
    )
    args = parser.parse_args(argv)

    config.load()

    ready = [n for n in models.ALIASES if config.has_credentials(models.REGISTRY[n].provider)]
    if not ready:
        # Named both ways round on purpose. Run by hand the answer is the .env;
        # run in a container there is no .env and the answer is the platform's
        # environment, and a message that only mentions the file sends someone
        # looking for a path that does not exist.
        wanted = ", ".join(sorted(set(models.CREDENTIAL_ENV.values())))
        raise SystemExit(
            f"No API credentials found. Set one of {wanted} in the environment, "
            f"or put a key in {config.CONFIG_ENV}."
        )

    orphans = mark_orphans()
    if orphans:
        print(f"Marked {orphans} interrupted review(s) from a previous run.")

    url = f"http://127.0.0.1:{args.port}/"
    global HOSTED
    HOSTED = args.host not in ("127.0.0.1", "localhost", "::1")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"AI Peer Reviewer — {url}")
    print(f"Models ready: {', '.join(ready)}")
    print("Ctrl-C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
