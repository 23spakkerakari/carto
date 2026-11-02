"""Which adapter serves which ``llm.provider`` (ADR 0054; spec 9.9).

``anthropic`` and ``openai`` are built; ``bedrock`` and ``vertex`` are named by ``LlmProvider``
and refused here until a partner asks for them. The fake provider is not in the table: tests
construct it, a service cannot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from carto_core.llm_gateway.egress import build_client
from carto_core.llm_gateway.providers.anthropic import AnthropicProvider
from carto_core.llm_gateway.providers.openai_compat import OpenAICompatibleProvider
from carto_core.llm_gateway.schemas import Provider, ProviderError

if TYPE_CHECKING:
    import httpx

    from carto_common.settings import LlmSettings

__all__ = ["BUILT_PROVIDERS", "DEFERRED_PROVIDERS", "provider_for"]

BUILT_PROVIDERS: Final[tuple[str, ...]] = ("anthropic", "openai")
DEFERRED_PROVIDERS: Final[tuple[str, ...]] = ("bedrock", "vertex")


def provider_for(settings: LlmSettings, client: httpx.Client | None = None) -> Provider:
    """The adapter for ``settings.provider`` over ``client`` (built from the settings when
    ``None``, so it can only reach ``llm.endpoint``)."""
    if settings.provider is None:
        msg = "llm.provider is not set"
        raise ProviderError(msg)
    if settings.provider in DEFERRED_PROVIDERS:
        msg = f"{settings.provider}: provider not built yet; see ADR 0054"
        raise ProviderError(msg)
    http = client if client is not None else build_client(settings)
    if settings.provider == "anthropic":
        return AnthropicProvider(settings, http)
    return OpenAICompatibleProvider(settings, http)
