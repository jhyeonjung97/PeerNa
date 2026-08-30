"""Provider backends.

Each backend takes the same three things — a system prompt, the manuscript as
neutral parts, and an instruction — and returns text or a validated object. The
orchestration in `review.py` never learns which provider it is talking to.

The one asymmetry worth knowing about is caching. Anthropic caching is explicit:
a breakpoint is placed on the manuscript with a one-hour lifetime, so all five
passes are guaranteed to reuse it. OpenAI caching is automatic and prefix-based
with no lifetime control, so the manuscript is placed first in the request to
give the prefix the best chance of matching, and that is all that can be done.
"""

from __future__ import annotations

import base64
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import BaseModel

from .models import CREDENTIAL_ENV, ModelSpec
from .parts import ImagePart, Part, PdfPart, TextPart, as_text_only
from .schema import strict_json_schema

MAX_TOKENS = 32_000

#: Sent on every request. See OpenAIBackend.__init__ for why this is not cosmetic.
USER_AGENT = "peerna/1.0"
CACHE_TTL = "1h"

#: A review is a handful of large requests, so a rate limit is common on lower
#: usage tiers. The SDKs honour Retry-After and back off exponentially; giving
#: them more attempts is cheaper than losing the run. Each attempt also gets a
#: generous timeout because a pass with high reasoning effort is slow.
MAX_RETRIES = 3

#: Long enough for a slow pass with web search and high reasoning effort, short
#: enough that a genuinely dead connection is not mistaken for a slow one. The
#: old 15-minute ceiling meant a stalled request looked identical to a working
#: one for a quarter of an hour.
REQUEST_TIMEOUT_SECONDS = 300.0

#: How often to print a still-working line while a call is in flight.
HEARTBEAT_SECONDS = 20

#: Pass-level retries after a rate limit, on top of the SDK's own. Each waits
#: the interval the server asked for rather than a guessed backoff.
RATE_LIMIT_ATTEMPTS = 6

#: Fallback wait when the API does not say how long to hold off.
DEFAULT_RETRY_SECONDS = 30.0
LEGACY_THINKING_BUDGET = 8_000
#: Each search's results are added to a context that already holds the
#: manuscript, so this trades directly against the per-minute token allowance.
#: Five is enough to check the load-bearing citations.
MAX_WEB_SEARCHES = 5


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def cost(self, spec: ModelSpec) -> float:
        rate = spec.input_per_mtok / 1_000_000
        return (
            self.input_tokens * rate
            + self.cache_write_tokens * rate * 1.25
            + self.cache_read_tokens * rate * 0.1
            + self.output_tokens * spec.output_per_mtok / 1_000_000
        )


class Backend(ABC):
    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec
        self.usage = Usage()
        self.warnings: list[str] = []
        #: Tokens sent on the last call, used to pace the next one.
        self.last_call_tokens = 0
        #: Passes run concurrently, so every mutation of the counters above has
        #: to be serialised. Without this the reported cost is silently wrong.
        self._lock = threading.Lock()

    def note(self, message: str) -> None:
        with self._lock:
            if message not in self.warnings:
                self.warnings.append(message)

    def add_usage(self, **counts: int) -> None:
        with self._lock:
            for name, value in counts.items():
                setattr(self.usage, name, getattr(self.usage, name) + value)

    def take_usage(self) -> Usage:
        """Hand back everything counted so far and start a fresh tally.

        Under concurrency this cannot attribute tokens to a particular pass —
        it returns whatever has accumulated. That is fine for the only thing it
        is used for, which is keeping a resumed run's total honest.
        """
        with self._lock:
            taken, self.usage = self.usage, Usage()
            return taken

    # -- rate limiting ----------------------------------------------------

    def _rate_limit_error(self) -> tuple:
        """The provider's rate-limit exception class."""
        raise NotImplementedError

    @staticmethod
    @contextmanager
    def _heartbeat(label: str):
        """Print elapsed time while a long call is in flight.

        A pass can legitimately run for minutes, and silence is
        indistinguishable from a hang. This makes the difference visible.
        """
        stop = threading.Event()

        def tick() -> None:
            waited = 0
            while not stop.wait(HEARTBEAT_SECONDS):
                waited += HEARTBEAT_SECONDS
                print(
                    f"    {label} — {waited}s elapsed", file=sys.stderr, flush=True
                )

        thread = threading.Thread(target=tick, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)

    def _retry_on_rate_limit(self, call, label: str = "working"):
        """Run `call`, waiting out rate limits for as long as the API asks.

        A review sends the whole manuscript once per pass, so a per-minute token
        allowance is spent quickly and a 429 is routine rather than exceptional.
        The server says exactly how long to wait; the SDK's own exponential
        backoff starts far shorter than that and gives up before reaching it. So
        this waits the full requested interval instead of guessing.
        """
        for attempt in range(RATE_LIMIT_ATTEMPTS):
            try:
                with self._heartbeat(label):
                    return call()
            except self._rate_limit_error() as exc:
                if attempt == RATE_LIMIT_ATTEMPTS - 1:
                    raise
                delay = _retry_after_seconds(exc)
                self.note(f"Rate limited; waited {delay:.0f}s and retried.")
                print(
                    f"    rate limited — waiting {delay:.0f}s "
                    f"(attempt {attempt + 1}/{RATE_LIMIT_ATTEMPTS})",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable")

    def pace(self) -> None:
        """Pause between passes so a per-minute token budget can refill.

        Without this, five back-to-back passes over a 50k-token manuscript
        exceed a 200k tokens-per-minute allowance partway through the run.
        """
        if self.last_call_tokens <= 0:
            return
        budget = self.spec.tokens_per_minute
        if not budget:
            return
        seconds = 60.0 * self.last_call_tokens / budget
        if seconds >= 1:
            print(f"    pacing {seconds:.0f}s", file=sys.stderr, flush=True)
            time.sleep(min(seconds, 90.0))

    @abstractmethod
    def run_text(
        self, system: str, parts: list[Part], instruction: str, web_search: bool,
        effort: str = "high", label: str = "working",
    ) -> str:
        """Answer the instruction about the manuscript, as prose."""

    @abstractmethod
    def run_structured(
        self, system: str, parts: list[Part], instruction: str,
        model_cls: type[BaseModel], web_search: bool = False,
    ) -> BaseModel:
        """Answer the instruction as a validated instance of model_cls."""

    @abstractmethod
    def count_input_tokens(self, system: str, parts: list[Part]) -> int:
        """Token count of the manuscript, for the pre-run estimate."""


# ---------------------------------------------------------------- Anthropic


class AnthropicBackend(Backend):
    def __init__(self, spec: ModelSpec) -> None:
        super().__init__(spec)
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(
            max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT_SECONDS
        )

    def _rate_limit_error(self):
        return (self._anthropic.RateLimitError,)

    @property
    def _api(self):
        return (
            self.client.beta.messages
            if self.spec.supports_fallbacks
            else self.client.messages
        )

    def _content(self, parts: list[Part], instruction: str, cache: bool) -> list[dict]:
        blocks: list[dict] = []
        for part in parts:
            if isinstance(part, TextPart):
                blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.media_type,
                            "data": base64.standard_b64encode(part.data).decode(),
                        },
                    }
                )
            elif isinstance(part, PdfPart):
                blocks.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(part.data).decode(),
                        },
                    }
                )
        if cache and blocks:
            blocks[-1] = {
                **blocks[-1],
                "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
            }
        blocks.append({"type": "text", "text": instruction})
        return blocks

    def _base_kwargs(self, system: str) -> dict:
        kwargs: dict = {
            "model": self.spec.id,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "thinking": (
                {"type": "adaptive"}
                if self.spec.adaptive_thinking
                else {"type": "enabled", "budget_tokens": LEGACY_THINKING_BUDGET}
            ),
        }
        if self.spec.supports_fallbacks and self.spec.fallback_model:
            kwargs["betas"] = ["server-side-fallback-2026-06-01"]
            kwargs["fallbacks"] = [{"model": self.spec.fallback_model}]
        return kwargs

    def _send(self, system: str, content: list[dict], extra: dict, label: str = "working") -> str:
        kwargs = self._base_kwargs(system)
        kwargs.update(extra)
        history: list[dict] = [{"role": "user", "content": content}]
        collected: list[str] = []

        for _ in range(10):  # resumptions after server-tool pauses
            def _call(k=kwargs, h=history):
                with self._api.stream(**k, messages=h) as stream:
                    return stream.get_final_message()

            message = self._retry_on_rate_limit(_call, label)

            usage = message.usage
            self.add_usage(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
            self.last_call_tokens = (usage.input_tokens or 0) + (
                usage.output_tokens or 0
            )

            if message.stop_reason == "refusal":
                details = getattr(message, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                raise SystemExit(
                    "The request was declined by a safety classifier"
                    + (f" (category: {category})" if category else "")
                    + ". This is unusual for a manuscript; check the file."
                )

            collected.extend(b.text for b in message.content if b.type == "text")

            if message.stop_reason == "pause_turn":
                history = history + [
                    {"role": "assistant", "content": message.content}
                ]
                continue
            if message.stop_reason == "max_tokens":
                self.note("A response hit the output limit and may be truncated.")
            break
        else:
            self.note("A request paused repeatedly and was cut off.")

        return "\n".join(chunk for chunk in collected if chunk.strip())

    def run_text(self, system, parts, instruction, web_search, effort="high",
                 label="working") -> str:
        extra: dict = {}
        if web_search:
            extra["tools"] = [
                {
                    "type": self.spec.web_search_tool_type,
                    "name": "web_search",
                    "max_uses": MAX_WEB_SEARCHES,
                }
            ]
        if self.spec.supports_effort:
            extra["output_config"] = {"effort": effort}
        return self._send(
            system, self._content(parts, instruction, cache=True), extra, label
        )

    def run_structured(self, system, parts, instruction, model_cls,
                       web_search: bool = False) -> BaseModel:
        output_config: dict = {
            "format": {"type": "json_schema", "schema": strict_json_schema(model_cls)}
        }
        if self.spec.supports_effort:
            output_config["effort"] = "high"
        raw = self._send(
            system,
            self._content(parts, instruction, cache=True),
            {"output_config": output_config},
            "Consolidating",
        )
        return _validate(model_cls, raw)

    def count_input_tokens(self, system, parts) -> int:
        result = self.client.messages.count_tokens(
            model=self.spec.id,
            system=system,
            messages=[
                {"role": "user", "content": self._content(parts, "review", cache=False)}
            ],
        )
        return result.input_tokens


# ------------------------------------------------------------------- OpenAI


#: Rendering a page at this DPI keeps axis labels and error bars legible while
#: staying well under the per-request size cap.
PAGE_RENDER_DPI = 150


class OpenAIBackend(Backend):
    """OpenAI Responses API.

    PDFs are sent as `input_file` first, which is what the flagship models take
    and which lets the API extract text and page images itself. Not every model
    in the family documents that content type, though — Luna lists its input
    modalities as "text, image" with no mention of files. Rather than guess, the
    first rejected request switches to rendering each page to an image and
    resending. Images are a modality every vision model states outright, so the
    figures reach the model either way.
    """

    def __init__(self, spec: ModelSpec) -> None:
        super().__init__(spec)
        import openai

        self._openai = openai
        self.client = openai.OpenAI(
            api_key=os.environ.get(CREDENTIAL_ENV[spec.provider]),
            base_url=spec.base_url,          # None means OpenAI's own endpoint
            max_retries=MAX_RETRIES,
            timeout=REQUEST_TIMEOUT_SECONDS,
            # Cloudflare in front of the router answers the SDK's default agent
            # with a bare `403 error code: 1010` and no explanation, which reads
            # exactly like a rejected key. Any other agent is accepted. Harmless
            # against OpenAI itself, so it is sent unconditionally.
            default_headers={"User-Agent": USER_AGENT},
        )
        #: Flipped the first time a native PDF request is rejected.
        self.rasterise_pdfs = False

    def _rate_limit_error(self):
        return (self._openai.RateLimitError,)

    @staticmethod
    def _image_block(media_type: str, data: bytes) -> dict:
        encoded = base64.b64encode(data).decode()
        return {
            "type": "input_image",
            "image_url": f"data:{media_type};base64,{encoded}",
        }

    def _content(self, parts: list[Part], instruction: str) -> list[dict]:
        blocks: list[dict] = []
        for part in parts:
            if isinstance(part, TextPart):
                blocks.append({"type": "input_text", "text": part.text})
            elif isinstance(part, ImagePart):
                blocks.append(self._image_block(part.media_type, part.data))
            elif isinstance(part, PdfPart):
                if self.rasterise_pdfs:
                    for number, png in _render_pages(part.data):
                        blocks.append({"type": "input_text", "text": f"[Page {number}]"})
                        blocks.append(self._image_block("image/png", png))
                else:
                    encoded = base64.b64encode(part.data).decode()
                    blocks.append(
                        {
                            "type": "input_file",
                            "filename": part.filename,
                            "file_data": f"data:application/pdf;base64,{encoded}",
                        }
                    )
        blocks.append({"type": "input_text", "text": instruction})
        return blocks

    def _call(self, kwargs: dict, parts: list[Part], instruction: str,
              label: str = "working", **extra):
        """Run a request, switching PDFs to page images if the API rejects them."""
        try:
            return self._request(kwargs, label=label, **extra)
        except self._openai.BadRequestError as exc:
            if self.rasterise_pdfs or not _looks_like_file_rejection(exc):
                raise
            if not any(isinstance(p, PdfPart) for p in parts):
                raise
            self.rasterise_pdfs = True
            self.note(
                f"{self.spec.label} rejected the PDF as a file; pages were rendered "
                "to images instead. Figures are still visible to the model, but "
                "text is read from the rendering rather than the PDF's own text."
            )
            kwargs = {
                **kwargs,
                "input": [
                    {"role": "user", "content": self._content(parts, instruction)}
                ],
            }
            return self._request(kwargs, label=label, **extra)

    def _request(self, kwargs: dict, label: str = "working", text_format=None):
        """Always streamed, even though nothing here consumes the events.

        A pass that searches the web runs well past two minutes, and the router
        sits behind a proxy that gives up on a silent connection after 120
        seconds — `524 origin_response_timeout`, which arrives looking like the
        model failed rather than the transport. A streamed response puts bytes
        on the wire continuously and the timer never starts. Against OpenAI's
        own endpoint this changes nothing except that very long requests stop
        being at the mercy of whatever sits in front of them.
        """
        def _call():
            # The heartbeat comes from _retry_on_rate_limit, which wraps this.
            if text_format is not None:
                with self.client.responses.stream(
                    **kwargs, text_format=text_format
                ) as stream:
                    for _ in stream:
                        pass
                    return stream.get_final_response()
            with self.client.responses.stream(**kwargs) as stream:
                for _ in stream:
                    pass
                return stream.get_final_response()

        return self._retry_on_rate_limit(_call, label)

    def _kwargs(self, system: str, parts: list[Part], instruction: str,
                effort: str = "high") -> dict:
        kwargs: dict = {
            "model": self.spec.id,
            "instructions": system,
            "max_output_tokens": MAX_TOKENS,
            "input": [{"role": "user", "content": self._content(parts, instruction)}],
        }
        if self.spec.supports_effort:
            kwargs["reasoning"] = {"effort": effort}
        return kwargs

    def _record(self, response) -> None:
        usage = getattr(response, "usage", None)
        if not usage:
            return
        cached = 0
        details = getattr(usage, "input_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        self.add_usage(
            input_tokens=(getattr(usage, "input_tokens", 0) or 0) - cached,
            cache_read_tokens=cached,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
        self.last_call_tokens = (getattr(usage, "input_tokens", 0) or 0) + (
            getattr(usage, "output_tokens", 0) or 0
        )

    def run_text(self, system, parts, instruction, web_search, effort="high",
                 label="working") -> str:
        kwargs = self._kwargs(system, parts, instruction, effort=effort)
        if web_search:
            kwargs["tools"] = [{"type": "web_search"}]
        response = self._call(kwargs, parts, instruction, label=label)
        self._record(response)
        if getattr(response, "status", None) == "incomplete":
            self.note("A response was cut short by the output limit.")
        return response.output_text

    def run_structured(self, system, parts, instruction, model_cls,
                       web_search: bool = False) -> BaseModel:
        kwargs = self._kwargs(system, parts, instruction)
        if web_search:
            kwargs["tools"] = [{"type": "web_search"}]
        response = self._call(
            kwargs, parts, instruction, label="Consolidating", text_format=model_cls
        )
        self._record(response)
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            return _validate(model_cls, response.output_text)
        return parsed

    def count_input_tokens(self, system, parts) -> int:
        """OpenAI has no token-counting endpoint, so estimate locally.

        Text is counted with the model's tokenizer where available; PDFs and
        images are approximated per page or per image, which is enough for a
        cost preview but is not exact.
        """
        text = system + "".join(p.text for p in parts if isinstance(p, TextPart))
        try:
            import tiktoken

            tokens = len(tiktoken.get_encoding("o200k_base").encode(text))
        except Exception:
            tokens = len(text) // 4

        for part in parts:
            if isinstance(part, ImagePart):
                tokens += 1_000
            elif isinstance(part, PdfPart):
                tokens += _pdf_pages(part.data) * 2_000
        return tokens


#: Substrings that mark a rejection as "this model will not take a file input",
#: as opposed to some other bad request we should not paper over.
_FILE_REJECTION_MARKERS = (
    "input_file",
    "file_data",
    "unsupported content",
    "not supported",
    "invalid content type",
    "does not support",
    "expected one of",
)


def _looks_like_file_rejection(exc: Exception) -> bool:
    message = str(getattr(exc, "message", "") or exc).lower()
    return any(marker in message for marker in _FILE_REJECTION_MARKERS)


def _render_pages(data: bytes) -> list[tuple[int, bytes]]:
    """Rasterise a PDF so its pages can be sent as images."""
    import pymupdf

    rendered: list[tuple[int, bytes]] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for index, page in enumerate(doc, start=1):
            rendered.append((index, page.get_pixmap(dpi=PAGE_RENDER_DPI).tobytes("png")))
    return rendered


def _retry_after_seconds(exc: Exception) -> float:
    """How long the server asked us to wait, with a small safety margin."""
    response = getattr(exc, "response", None)
    if response is not None:
        header = response.headers.get("retry-after")
        if header:
            try:
                return float(header) + 2.0
            except ValueError:
                pass
    # Some providers only state the interval in the message body.
    message = str(getattr(exc, "message", "") or exc)
    match = re.search(r"try again in ([\d.]+)\s*(ms|s)\b", message, re.I)
    if match:
        value = float(match.group(1))
        return (value / 1000 if match.group(2).lower() == "ms" else value) + 2.0
    return DEFAULT_RETRY_SECONDS


def _pdf_pages(data: bytes) -> int:
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return 1


def _validate(model_cls: type[BaseModel], raw: str) -> BaseModel:
    try:
        return model_cls.model_validate_json(raw)
    except Exception as exc:
        raise SystemExit(
            f"Could not parse the structured response: {exc}\n\n"
            f"Raw response:\n{raw[:2000]}"
        ) from None


def build(spec: ModelSpec) -> Backend:
    if spec.provider == "anthropic":
        return AnthropicBackend(spec)
    # The router speaks the same Responses API — files, structured output and
    # server-side web search all work through it. Its chat-completions endpoint
    # does none of those, which cost an afternoon before the other one was tried.
    if spec.provider in ("openai", "monorouter"):
        return OpenAIBackend(spec)
    raise SystemExit(f"Unknown provider {spec.provider!r}")
