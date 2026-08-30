"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import backends, checkpoint as checkpoint_mod, config, history, loader, models, notify
from .passes import PASSES, system_prompt as passes_system_prompt
from .render import render, render_html, render_pass_notes
from .review import Reviewer

#: Rough per-call output for the pre-run estimate. Actual usage is reported
#: after the run.
ESTIMATED_OUTPUT_TOKENS_PER_CALL = 4_000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-peer-review",
        description="Review a manuscript the way a journal referee would.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Models:\n"
        + "\n".join(
            f"  {name:8} {models.REGISTRY[name].label:10} "
            f"${models.REGISTRY[name].input_per_mtok:g}/${models.REGISTRY[name].output_per_mtok:g} per Mtok"
            f"  — {models.QUALITY_NOTE[name]}"
            for name in models.ALIASES
        ),
    )
    parser.add_argument("manuscript", type=Path, help="PDF, DOCX, LaTeX, or plain text")
    parser.add_argument(
        "--supplementary", type=Path, metavar="PDF",
        help="Supplementary information. Not sent to the model — it is usually "
             "larger than the paper and mostly data the lenses have no use for. "
             "It is read by the consistency checks, which is where it earns its "
             "place: without it every 'Supplementary Fig. N' in the manuscript "
             "goes unverified.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Model to review with (default: {models.DEFAULT_MODEL}, or "
        "AI_PEER_REVIEWER_MODEL if set). See below.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Write the report here (default: stdout). A .html extension "
        "selects the HTML format.",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Render a self-contained HTML page instead of Markdown",
    )
    parser.add_argument(
        "--notes",
        action="store_true",
        help="Also write the raw per-pass findings alongside the report",
    )
    parser.add_argument(
        "--no-web-search",
        action="store_true",
        help="Skip citation verification. Faster and cheaper, but every citation "
        "comment will be marked unverified.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate cost and exit without calling the API",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore any saved passes from an interrupted run and start over",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    started = time.time()
    args = build_parser().parse_args(argv)

    if not args.manuscript.is_file():
        raise SystemExit(f"No such file: {args.manuscript}")
    if args.supplementary and not args.supplementary.is_file():
        raise SystemExit(f"No such file: {args.supplementary}")

    config.load()
    spec = models.resolve(models.preferred(
        args.model or config.configured_model() or models.DEFAULT_MODEL
    ))
    manuscript = loader.load(args.manuscript, spec)

    problem = models.retention_problem(spec, config.zero_retention())
    if problem:
        print(f"NOTE: {problem}\n", file=sys.stderr)
    if not args.no_web_search:
        print(
            "NOTE: web search is on. Search queries leave for a search engine and "
            "are logged there,\n      which no data-retention agreement covers. The "
            "literature pass is told to search\n      in its own words and never to "
            "quote the manuscript; use --no-web-search to be certain.\n",
            file=sys.stderr,
        )
    print(f"Manuscript : {manuscript.name}", file=sys.stderr)
    print(f"Model      : {spec.label}", file=sys.stderr)
    if spec.requires_data_retention:
        print(
            f"  note     : {spec.label} cannot run under zero data retention — "
            "requests are retained for 30 days.",
            file=sys.stderr,
        )
    for note in manuscript.notes:
        print(f"  note     : {note}", file=sys.stderr)
    print(file=sys.stderr)

    env_name = models.CREDENTIAL_ENV[spec.provider]
    if not config.has_credentials(spec.provider):
        where = (
            "https://platform.claude.com/settings/keys"
            if spec.provider == "anthropic"
            else "https://platform.openai.com/api-keys"
        )
        raise SystemExit(
            f"{spec.label} needs a {spec.provider.title()} key, and none was found.\n\n"
            f"  1. Put it in {config.CONFIG_ENV}\n"
            f"       {env_name}=...\n\n"
            "  2. Or export it in your shell:\n"
            f"       export {env_name}=...\n\n"
            f"Get a key at {where}\n\n"
            "Do not put the key in a .env inside this project if the project "
            "lives in a synced folder — it will be uploaded.\n\n"
            "Usage is billed to whichever key is set, so anyone running this "
            "tool pays for their own reviews."
        )

    with _api_errors(spec):
        backend = backends.build(spec)
        input_tokens = backend.count_input_tokens(
            passes_system_prompt("referee"), manuscript.parts
        )
    _print_estimate(spec, input_tokens)

    if input_tokens > spec.context_tokens * 0.8:
        raise SystemExit(
            f"\nThe manuscript is {input_tokens:,} tokens, too large for "
            f"{spec.label} ({spec.context_tokens:,} token context). "
            "Use a model with a bigger context window."
        )

    if args.dry_run:
        return 0


    resume = checkpoint_mod.Checkpoint(
        checkpoint_mod.run_id(
            args.manuscript.read_bytes(), "referee", spec.id
        )
    )
    if args.fresh:
        resume.clear()
        resume = checkpoint_mod.Checkpoint(resume.dir.name)

    reviewer = Reviewer(
        backend=backend,
        mode="referee",
        use_web_search=not args.no_web_search,
        checkpoint=resume,
    )
    reviewer.supplementary = args.supplementary

    print(file=sys.stderr)

    def progress(step: int, total: int, title: str) -> None:
        print(f"[{step}/{total}] {title}...", file=sys.stderr)

    with _api_errors(spec):
        result = reviewer.review(manuscript, progress=progress)

    as_html = args.html or (
        args.out is not None and args.out.suffix.lower() in (".html", ".htm")
    )
    renderer = render_html if as_html else render
    output = renderer(result, manuscript.name, "referee", spec.label)

    if args.out:
        args.out.write_text(output, encoding="utf-8")
        print(f"\nReport written to {args.out}", file=sys.stderr)
        if args.notes:
            # The per-pass notes are raw prose from the model, so they stay
            # Markdown even when the report itself is rendered as HTML.
            notes_path = args.out.with_name(args.out.stem + "-notes.md")
            notes_path.write_text(render_pass_notes(result), encoding="utf-8")
            print(f"Per-pass notes written to {notes_path}", file=sys.stderr)
    else:
        print(output)

    actual = result.usage.cost(spec)
    print(
        f"\nTokens: {result.usage.input_tokens:,} in "
        f"(+{result.usage.cache_write_tokens:,} cached, "
        f"{result.usage.cache_read_tokens:,} reused), "
        f"{result.usage.output_tokens:,} out",
        file=sys.stderr,
    )
    print(f"Cost:   ${actual:.2f}", file=sys.stderr)

    history.record_cli_run(
        filename=manuscript.name,
        html=output if as_html else render_html(result, manuscript.name, "referee", spec.label),
        markdown=output if not as_html else render(result, manuscript.name, "referee", spec.label),
        cost=actual,
        duration=time.time() - started,
        warnings=result.warnings,
        tokens=input_tokens,
        web_search=not args.no_web_search,
    )

    where = f" → {args.out}" if args.out else ""
    sent = notify.announce(
        manuscript.name,
        f"{len(result.report.comments)} comments · "
        f"{result.report.recommendation.replace('_', ' ')} · ${actual:.2f}{where}",
        subtitle="Review finished",
    )
    if sent:
        print(f"Notified: {', '.join(sent)}", file=sys.stderr)
    return 0


@contextmanager
def _api_errors(spec: models.ModelSpec):
    """Turn SDK exceptions into a one-line message instead of a traceback.

    Both SDKs raise the same family of names, so the handling is shared and only
    the remediation text differs by provider.
    """
    import anthropic
    import openai

    env_name = models.CREDENTIAL_ENV[spec.provider]
    upgrade = "anthropic" if spec.provider == "anthropic" else "openai"
    keys_url = (
        "https://platform.claude.com/settings/keys"
        if spec.provider == "anthropic"
        else "https://platform.openai.com/api-keys"
    )

    auth = (anthropic.AuthenticationError, openai.AuthenticationError)
    denied = (anthropic.PermissionDeniedError, openai.PermissionDeniedError)
    missing = (anthropic.NotFoundError, openai.NotFoundError)
    limited = (anthropic.RateLimitError, openai.RateLimitError)
    offline = (anthropic.APIConnectionError, openai.APIConnectionError)
    status = (anthropic.APIStatusError, openai.APIStatusError)

    try:
        yield
    except auth:
        raise SystemExit(
            f"The {spec.provider} API key was rejected. Check it is current and "
            "copied in full:\n"
            f"  {config.CONFIG_ENV}\n"
            f"  or the {env_name} in your shell\n"
            f"Keys are at {keys_url}"
        ) from None
    except denied:
        raise SystemExit(
            "The API key is valid but not permitted to use this model. "
            "Check the workspace or project it belongs to."
        ) from None
    except missing:
        raise SystemExit(
            f"Model {spec.id!r} was not found. Your SDK may predate it — try "
            f"`pip install -U {upgrade}`."
        ) from None
    except limited as exc:
        # The API's own message names which limit was hit and by how much, which
        # is the only way to tell "wait a minute" from "this request can never
        # fit in your tier". Never swallow it.
        detail = getattr(exc, "message", "") or str(exc)
        retry_after = None
        response = getattr(exc, "response", None)
        if response is not None:
            retry_after = response.headers.get("retry-after")
        raise SystemExit(
            f"Rate limited by {spec.provider}, and the automatic retries did not "
            f"clear it.\n\n{detail}\n"
            + (f"\nRetry-After: {retry_after}s\n" if retry_after else "")
            + "\nCompleted passes were saved, so running the same command again "
            "resumes instead of starting over."
        ) from None
    except offline:
        raise SystemExit("Could not reach the API. Check your network.") from None
    except status as exc:
        detail = getattr(exc, "message", "") or str(exc)
        raise SystemExit(f"API error {exc.status_code}: {detail}") from None


def _print_estimate(spec: models.ModelSpec, input_tokens: int) -> None:
    calls = len(PASSES) + 1
    rate = spec.input_per_mtok / 1_000_000
    # One cache write, then the prefix is reused by every later call.
    estimate = (
        input_tokens * 1.25 * rate
        + input_tokens * (calls - 1) * 0.1 * rate
        + ESTIMATED_OUTPUT_TOKENS_PER_CALL * calls * spec.output_per_mtok / 1_000_000
    )
    print(
        f"Manuscript is ~{input_tokens:,} tokens. "
        f"{calls} calls with caching, estimated ${estimate:.2f}.",
        file=sys.stderr,
    )
    if spec.provider == "openai":
        print(
            "  (OpenAI caching is automatic with no lifetime control, so the "
            "manuscript may be re-read at full price on some passes. Token "
            "counts are estimated locally, not measured.)",
            file=sys.stderr,
        )




if __name__ == "__main__":
    raise SystemExit(main())
