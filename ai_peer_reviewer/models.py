"""Model registry.

Each model exposes a slightly different API surface. Rather than scattering
`if model == ...` checks through the request code, every difference that
matters is declared here once and the request builders read from the spec.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str

    #: Which backend runs this model: "anthropic", "openai", or "monorouter".
    provider: str
    input_per_mtok: float
    output_per_mtok: float
    context_tokens: int

    #: Max pages the API accepts in a single PDF document block. Models with a
    #: 200K context window are capped at 100; 1M-context models allow 600.
    max_pdf_pages: int

    #: `output_config.effort` (Anthropic) / `reasoning.effort` (OpenAI).
    supports_effort: bool

    #: Your account's tokens-per-minute allowance for this model, used to pace
    #: passes so a run does not trip a 429 partway through. Read it off the
    #: provider's rate-limit page; the defaults here are conservative starting
    #: points, not promises. Set to 0 to disable pacing.
    tokens_per_minute: int = 200_000

    # -- Anthropic-only knobs; ignored by the OpenAI backend ------------------

    #: 4.6+ models take `thinking={"type": "adaptive"}`. Older models need an
    #: explicit `budget_tokens` integer instead.
    adaptive_thinking: bool = True

    #: Server-side refusal fallbacks (beta). Opus 5 / Fable 5 only.
    supports_fallbacks: bool = False

    #: The dynamic-filtering web search tool is only on Opus 4.6+ and Sonnet 4.6+.
    #: Everything older keeps the basic variant.
    web_search_tool_type: str = "web_search_20260209"

    #: Model to fall back to if a request is refused by a safety classifier.
    fallback_model: str | None = None

    #: Models that cannot run under zero data retention — the provider keeps the
    #: request whatever the account's agreement says. Declared here since 2025
    #: and never once shown to anyone, which made it a comment rather than a
    #: safeguard. `retention_problem` is what reads it.
    requires_data_retention: bool = False

    # -- routed models --------------------------------------------------------

    #: Where the OpenAI-compatible endpoint lives, when it is not OpenAI's own.
    base_url: str | None = None

    #: Whether this route can see a PDF. A router that only forwards text still
    #: reviews the manuscript, but the figures pass has nothing to look at, so
    #: the loader is told to extract text rather than attach pages.
    accepts_pdf: bool = True

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.input_per_mtok
            + output_tokens / 1_000_000 * self.output_per_mtok
        )


FABLE_5 = ModelSpec(
    provider="anthropic",
    id="claude-fable-5",
    label="Fable 5",
    input_per_mtok=10.00,
    output_per_mtok=50.00,
    context_tokens=1_000_000,
    max_pdf_pages=600,
    # Thinking is always on and cannot be configured; adaptive is accepted and
    # is what the shared request builder sends.
    adaptive_thinking=True,
    supports_effort=True,
    supports_fallbacks=True,
    web_search_tool_type="web_search_20260209",
    fallback_model="claude-opus-5",
    requires_data_retention=True,
)

OPUS_5 = ModelSpec(
    provider="anthropic",
    id="claude-opus-5",
    label="Opus 5",
    input_per_mtok=5.00,
    output_per_mtok=25.00,
    context_tokens=1_000_000,
    max_pdf_pages=600,
    adaptive_thinking=True,
    supports_effort=True,
    supports_fallbacks=True,
    web_search_tool_type="web_search_20260209",
    fallback_model="claude-opus-4-8",
)

SONNET_5 = ModelSpec(
    provider="anthropic",
    id="claude-sonnet-5",
    label="Sonnet 5",
    input_per_mtok=3.00,
    output_per_mtok=15.00,
    context_tokens=1_000_000,
    max_pdf_pages=600,
    adaptive_thinking=True,
    supports_effort=True,
    supports_fallbacks=False,
    web_search_tool_type="web_search_20260209",
)

HAIKU_45 = ModelSpec(
    provider="anthropic",
    id="claude-haiku-4-5",
    label="Haiku 4.5",
    input_per_mtok=1.00,
    output_per_mtok=5.00,
    context_tokens=200_000,
    max_pdf_pages=100,
    adaptive_thinking=False,
    supports_effort=False,
    supports_fallbacks=False,
    web_search_tool_type="web_search_20250305",
)

LUNA = ModelSpec(
    provider="openai",
    id="gpt-5.6-luna",
    label="GPT-5.6 Luna",
    input_per_mtok=0.20,
    output_per_mtok=1.20,
    context_tokens=1_050_000,
    # OpenAI caps a request at 50 MB of files rather than a page count; the
    # loader's byte check covers it, so leave the page gate effectively open.
    max_pdf_pages=10_000,
    supports_effort=True,
)

# --------------------------------------------------------------------------
# MonoRouter
#
# An OpenAI-compatible endpoint that fronts OpenAI, Anthropic and Google models
# behind one key. It exists here for one job: every result so far was measured
# on Luna alone, so "the extra passes do not help" could equally well be "Luna
# does not benefit from extra passes". Running the same ablation on Opus tells
# those apart, and this is the only route to a Claude model without a second
# account.
#
# It is not the default and should not become one. Three things are lost:
#
# It speaks the same Responses API as OpenAI, so files, structured output and
# server-side web search all work and the ordinary backend drives it unchanged.
# Its *chat-completions* endpoint does none of the three — no files, no web
# search, and `response_format` accepted and then silently ignored — which is
# worth writing down, because that endpoint is the obvious one to try first and
# every failure it produces looks like something else.
#
# One header matters: Cloudflare answers the SDK's default User-Agent with a
# bare `403 error code: 1010`, which is indistinguishable from a bad key.
#
# Prices are the upstream list prices. The router's own margin is unknown, so
# treat every cost this reports as a floor.
# --------------------------------------------------------------------------

MONOROUTER_URL = "https://monogpt.kr/api/monorouter/v1/"


def _routed(model_id: str, label: str, spec: ModelSpec) -> ModelSpec:
    """The same model, reached through the router instead of its own API."""
    from dataclasses import replace

    return replace(
        spec,
        id=model_id,
        label=f"{label} (router)",
        provider="monorouter",
        base_url=MONOROUTER_URL,
        accepts_pdf=True,
        supports_fallbacks=False,
        requires_data_retention=False,
    )


ROUTED_OPUS_5 = _routed("claude-opus-5", "Opus 5", OPUS_5)
ROUTED_SONNET_5 = _routed("claude-sonnet-5", "Sonnet 5", SONNET_5)
ROUTED_HAIKU_45 = _routed("claude-haiku-4.5", "Haiku 4.5", HAIKU_45)
ROUTED_LUNA = _routed("gpt-5.6-luna", "GPT-5.6 Luna", LUNA)

#: Order here is the order shown in `--help`.
ALIASES = ("luna", "fable", "opus", "sonnet", "haiku",
           "r-opus", "r-sonnet", "r-haiku", "r-luna")

REGISTRY: dict[str, ModelSpec] = {
    "fable": FABLE_5,
    "opus": OPUS_5,
    "sonnet": SONNET_5,
    "haiku": HAIKU_45,
    "luna": LUNA,
    FABLE_5.id: FABLE_5,
    OPUS_5.id: OPUS_5,
    SONNET_5.id: SONNET_5,
    HAIKU_45.id: HAIKU_45,
    LUNA.id: LUNA,
    "r-opus": ROUTED_OPUS_5,
    "r-sonnet": ROUTED_SONNET_5,
    "r-haiku": ROUTED_HAIKU_45,
    "r-luna": ROUTED_LUNA,
}

#: Luna, and deliberately so. It is the cheapest model here by an order of
#: magnitude, every measurement in DECISIONS.md was taken on it, and changing it
#: would invalidate all of them at once. The routed Claude models are for the
#: one experiment that needs them.
DEFAULT_MODEL = "luna"

#: Shown by `--help` and in the cost preview.
QUALITY_NOTE = {
    "fable": "Most capable. Worth it for a paper you cannot afford to misjudge. Needs 30-day data retention.",
    "opus": "Best value for real reviews. Catches subtle methodological problems.",
    "sonnet": "Solid. Good for iterating on your own drafts.",
    "haiku": "Fast and cheap, but shallow — expect generic comments. 200K context only.",
    "luna": "Cheapest by far (OpenAI). Bottom tier, so expect shallow findings. Needs OPENAI_API_KEY.",
    "r-opus": "Opus 5 through MonoRouter. Use to tell an architecture problem from a model one.",
    "r-sonnet": "Sonnet 5 through MonoRouter.",
    "r-haiku": "Haiku 4.5 through MonoRouter.",
    "r-luna": "Luna through MonoRouter — same model as `luna`, billed to the router key.",
}

#: Environment variable each provider's SDK reads.
CREDENTIAL_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "monorouter": "MONO_ROURTER_API_KEY",
}


def resolve(name: str) -> ModelSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise SystemExit(
            f"Unknown model {name!r}. Choose one of: " + ", ".join(ALIASES)
        ) from None


def retention_problem(spec: ModelSpec, zero_retention: bool) -> str | None:
    """Why this model is a poor fit for the confidentiality the operator claims.

    Returns a sentence to show the user, or None. Two separate things are at
    stake and only one of them is about retention:

    - A model that keeps requests cannot honour a zero-retention agreement, so
      choosing one silently would make the claim false.
    - A routed model puts a second company between the manuscript and the model.
      Its logging policy is its own, and we have no way to see it.
    """
    if zero_retention and spec.requires_data_retention:
        return (
            f"{spec.label} cannot run under a zero-retention agreement — the "
            "provider keeps requests for this model regardless. Choose another "
            "model for a manuscript under review."
        )
    if spec.provider == "monorouter":
        return (
            f"{spec.label} is reached through a router, so the manuscript passes "
            "through a second company before it reaches the model. Its retention "
            "policy is not ours to see. For a manuscript under review, prefer a "
            "model on its provider's own endpoint."
        )
    return None
