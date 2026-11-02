"""Entity names through the LLM gateway, with the dictionary as the fallback (spec 9.9; ADR
0004, 0054; plan M2 packages C and G).

:class:`GatewayNamer` is an :data:`~carto_core.linker.families.EntityNamer`: given a family's
member ends it asks the gateway's ``name_entity`` task for one name. The payload is built by
:func:`carto_core.llm_gateway.payload.build_name_entity_payload` from the members' field
summaries (path, system name, top shapes, template text) read through a
:class:`~carto_core.llm_gateway.payload.MetadataReader`; the family is being built at that
moment, so its ``entity_fields`` rows do not exist yet and :class:`_MembersReader` answers the
``entity_members`` call with the members in hand. The answer is a suggestion
(``suggested_name``) the reviewer confirms (spec 2.3 invariant 5). When the gateway is disabled,
the provider fails, the audit row cannot be written, the payload cannot be built (no member in
the ``fields`` table yet) or the answer is empty, the fallback namer (the spec 9.9 dictionary,
:func:`carto_core.linker.naming.suggest_entity_name`) names the family instead, and the log
says so by exception class only (spec 2.3 invariant 7).

:func:`gateway_namer_for` builds the namer for a worker: ``None`` when ``llm.enabled`` is false
(the pass keeps its heuristic namer), else the registered provider over the egress-allowlisted
client, the ``llm_calls`` audit sink and the PostgreSQL metadata reader.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from carto_common.logging import get_logger
from carto_core.linker.families import EntityNamer, entity_id_for
from carto_core.linker.naming import MAX_NAME_LEN
from carto_core.llm_gateway.gateway import GatewayDisabledError, LlmGateway, PayloadRefusedError
from carto_core.llm_gateway.payload import MetadataReader, PayloadError, build_name_entity_payload
from carto_core.llm_gateway.schemas import FieldSummary, ProviderError

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from carto_core.settings import CoreSettings
    from carto_core.store.model import FieldForm

__all__ = ["GatewayNamer", "gateway_namer_for"]

log = get_logger(component="linker")


class _MembersReader:
    """A reader whose ``entity_members`` answers with the family in hand (module docstring)."""

    __slots__ = ("_members", "_reader")

    def __init__(self, reader: MetadataReader, members: Sequence[FieldForm]) -> None:
        self._reader = reader
        self._members = members

    def field_summaries(self, tenant_id: str, field_ids: Sequence[str]) -> list[FieldSummary]:
        return self._reader.field_summaries(tenant_id, field_ids)

    def entity_members(self, tenant_id: str, entity_id: str) -> list[FieldSummary]:
        del entity_id
        refs = list(dict.fromkeys(end.field_ref for end in self._members))
        return self._reader.field_summaries(tenant_id, refs)


class GatewayNamer:
    """See the module docstring."""

    __slots__ = ("_fallback", "_gateway", "_reader", "_tenant")

    def __init__(
        self,
        gateway: LlmGateway,
        reader: MetadataReader,
        tenant_id: str,
        *,
        fallback: EntityNamer | None = None,
    ) -> None:
        self._gateway = gateway
        self._reader = reader
        self._tenant = tenant_id
        self._fallback = fallback

    def __call__(self, members: Sequence[FieldForm]) -> str | None:
        entity_id = entity_id_for(members)
        try:
            request = build_name_entity_payload(
                _MembersReader(self._reader, members), self._tenant, entity_id
            )
            answer = self._gateway.suggest_entity_name(self._tenant, request)
        except (GatewayDisabledError, ProviderError, PayloadError, PayloadRefusedError) as exc:
            log.info("entity name from the dictionary", reason=type(exc).__name__)
            return self._fall_back(members)
        except Exception as exc:  # a reader or sink failure must never stop the family rebuild
            log.warning("entity naming failed; dictionary in use", error=type(exc).__name__)
            return self._fall_back(members)
        for suggestion in answer.suggestions:
            label = suggestion.label.strip()
            if suggestion.target_id == entity_id and label:
                return label[:MAX_NAME_LEN]
        return self._fall_back(members)

    def _fall_back(self, members: Sequence[FieldForm]) -> str | None:
        return self._fallback(members) if self._fallback is not None else None


def gateway_namer_for(
    settings: CoreSettings,
    engine: Engine,
    tenant_id: str,
    *,
    fallback: EntityNamer | None,
    clock: Callable[[], object] | None = None,
) -> GatewayNamer | None:
    """The worker's namer, or ``None`` when the gateway is off or its provider is not built
    (ADR 0054: the pass then names with the dictionary and the log says why)."""
    del clock
    if not settings.llm.enabled:
        return None
    from carto_core.llm_gateway.audit import PostgresCallAuditSink  # noqa: PLC0415
    from carto_core.llm_gateway.metadata_reader import PostgresMetadataReader  # noqa: PLC0415
    from carto_core.llm_gateway.registry import provider_for  # noqa: PLC0415

    try:
        provider = provider_for(settings.llm)
    except ProviderError as exc:
        log.warning("llm provider unavailable; entity names from the dictionary", error=str(exc))
        return None
    gateway = LlmGateway(settings.llm, provider, PostgresCallAuditSink(engine))
    return GatewayNamer(gateway, PostgresMetadataReader(engine), tenant_id, fallback=fallback)
