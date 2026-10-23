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

    #: Which backend runs this model: "anthropic" or "openai".
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
    #: passes so a run does not trip a 429 partway through. Set to 0 to disable
    #: pacing. It was 200,000, a guess; the response headers on 2026-10-22 said
    #: 2,000,000 for Luna and 10,000,000 input for Anthropic, and the guess was
    #: costing about 45 seconds a review in pauses nothing needed. A 429 is still
    #: retried after its Retry-After, so an account below this is slowed, not
    #: broken.
    tokens_per_minute: int = 2_000_000

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
    input_per_mtok=2.00,
    output_per_mtok=10.00,
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

# -- the 5.5 generation and Fable 5.1 (October 2026) -------------------------
#
# What the menu names now. None of them accepts disabled thinking or forced
# tool choice, and neither is sent: thinking is adaptive and structured output
# goes through output_config.format. Prices are list prices.

FABLE_51 = ModelSpec(
    provider="anthropic",
    id="claude-fable-5-1",
    label="Fable 5.1",
    input_per_mtok=10.00,
    output_per_mtok=50.00,
    context_tokens=1_000_000,
    max_pdf_pages=600,
    adaptive_thinking=True,
    supports_effort=True,
    supports_fallbacks=True,
    web_search_tool_type="web_search_20260209",
    fallback_model="claude-opus-4-8",
    requires_data_retention=True,
)

OPUS_55 = ModelSpec(
    provider="anthropic",
    id="claude-opus-5-5",
    label="Opus 5.5",
    input_per_mtok=4.00,
    output_per_mtok=20.00,
    context_tokens=1_000_000,
    max_pdf_pages=600,
    adaptive_thinking=True,
    supports_effort=True,
    supports_fallbacks=True,
    web_search_tool_type="web_search_20260209",
    fallback_model="claude-opus-4-8",
)

SONNET_55 = ModelSpec(
    provider="anthropic",
    id="claude-sonnet-5-5",
    label="Sonnet 5.5",
    input_per_mtok=2.00,
    output_per_mtok=10.00,
    context_tokens=1_000_000,
    max_pdf_pages=600,
    adaptive_thinking=True,
    supports_effort=True,
    # Sonnet 5.5 takes only the "default" form of fallbacks, not the list the
    # request builder sends, so it goes without.
    supports_fallbacks=False,
    web_search_tool_type="web_search_20260209",
)

HAIKU_55 = ModelSpec(
    provider="anthropic",
    id="claude-haiku-5-5",
    label="Haiku 5.5",
    # Up to 100K input tokens a request; beyond that $0.50 / $2.50. A review's
    # requests sit under it, so the lower rate is the one used for estimates.
    input_per_mtok=0.10,
    output_per_mtok=0.50,
    context_tokens=1_000_000,
    max_pdf_pages=600,
    adaptive_thinking=True,
    supports_effort=True,
    supports_fallbacks=False,
    # The dynamic-filtering search tool is not offered on Haiku.
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

#: Order here is the order shown in `--help`.
ALIASES = ("luna", "fable", "opus", "sonnet", "haiku")

#: The short names follow the newest model of each line. The older ones stay
#: reachable by their full ids, so a run can be repeated on what it used.
REGISTRY: dict[str, ModelSpec] = {
    "fable": FABLE_51,
    "opus": OPUS_55,
    "sonnet": SONNET_55,
    "haiku": HAIKU_55,
    "luna": LUNA,
    FABLE_51.id: FABLE_51,
    OPUS_55.id: OPUS_55,
    SONNET_55.id: SONNET_55,
    HAIKU_55.id: HAIKU_55,
    FABLE_5.id: FABLE_5,
    OPUS_5.id: OPUS_5,
    SONNET_5.id: SONNET_5,
    HAIKU_45.id: HAIKU_45,
    LUNA.id: LUNA,
}

#: Luna, and deliberately so. Every measurement in DECISIONS.md was taken on it,
#: and changing it would invalidate all of them at once. It was the cheapest by
#: an order of magnitude when chosen; Haiku 5.5 is now cheaper per token, and
#: whether it reviews as well is unmeasured.
DEFAULT_MODEL = "luna"

#: Shown by `--help` and in the cost preview.
QUALITY_NOTE = {
    "fable": "Most capable. Worth it for a paper you cannot afford to misjudge. Needs 30-day data retention.",
    "opus": "Best value for real reviews. Catches subtle methodological problems.",
    "sonnet": "Solid. Good for iterating on your own drafts.",
    "haiku": "Cheapest per token, fast. Untested here — no review quality measured yet.",
    "luna": "Cheap (OpenAI) and the model every measurement here was taken on. Expect shallow findings. Needs OPENAI_API_KEY.",
}

#: Environment variable each provider's SDK reads.
CREDENTIAL_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


#: Names that once reached a model through MonoRouter, a middleman whose account
#: ran out at the end of August 2026. Gone from every menu; kept here only so a
#: configuration still naming one runs on the same model directly instead of
#: failing.
RETIRED = {
    "r-luna": "luna",
    "r-opus": "opus",
    "r-sonnet": "sonnet",
    "r-haiku": "haiku",
}


def preferred(name: str) -> str:
    """The model to actually use, which is not always the one configured."""
    return RETIRED.get(name, name)


def resolve(name: str) -> ModelSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise SystemExit(
            f"Unknown model {name!r}. Choose one of: " + ", ".join(ALIASES)
        ) from None


def retention_problem(spec: ModelSpec, zero_retention: bool) -> str | None:
    """Why this model is a poor fit for the confidentiality the operator claims.

    Returns a sentence to show the user, or None.

    A model that keeps requests cannot honour a zero-retention agreement, so
    choosing one silently would make the claim false.
    """
    if zero_retention and spec.requires_data_retention:
        return (
            f"{spec.label} cannot run under a zero-retention agreement — the "
            "provider keeps requests for this model regardless. Choose another "
            "model for a manuscript under review."
        )
    return None
