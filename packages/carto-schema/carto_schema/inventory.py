"""Edge inventory: what the edge is connected to, reported to core (plan M2 decision 8).

``POST /internal/inventory`` (mutual TLS, role ``edge-gateway``) carries one
:class:`EdgeInventory`: the systems and sources of the edge's sources file and its field policy
pins, so the console shows Systems, Setup > Sources and Setup > Field policies read-only from the
first login. The edge sends it on start and whenever the sources file changes; core replaces its
copy for the sending edge (``edge_id``) as a whole.

Secrets never travel: a source carries its ``secret_ref`` name only (spec 14.3), and
``config`` is the connector configuration with every secret-bearing key removed by the edge
before sending. This model is core's backstop for that (ADR 0069): ``secret_ref`` must be a
``<scheme>://<path>`` reference and never a literal secret, a config key that names a secret
(:func:`config_key_is_secret`, the rule of ``carto_common.logging.is_secret_key``) is refused,
and a config string value is under the same value-shape rule as an attribute value (a URL with
a password, an email, a key, a long hex or digit run are refused). Core still treats ``config``
as untrusted text when it renders it (spec 13). Writing configuration from core to the edge is
not part of this contract (the control plane is decided at M6 planning).
"""

from __future__ import annotations

import itertools
import re
from typing import Annotated, Final

from pydantic import AfterValidator, Field, field_validator

from carto_schema.bundle import FieldClassName, PolicyName
from carto_schema.event import (
    ContractModel,
    SchemaVersion,
    SourceId,
    SystemId,
    TenantId,
    UtcTimestamp,
    value_shape,
)

__all__ = [
    "MAX_INVENTORY_PINS",
    "MAX_INVENTORY_SOURCES",
    "MAX_INVENTORY_SYSTEMS",
    "SECRET_REF_SCHEMES",
    "EdgeInventory",
    "InventoryFieldPolicy",
    "InventorySource",
    "InventorySystem",
    "config_key_is_secret",
    "validate_secret_ref",
]

MAX_INVENTORY_SYSTEMS: Final = 1024
MAX_INVENTORY_SOURCES: Final = 4096
MAX_INVENTORY_PINS: Final = 4096
MAX_CONFIG_KEYS: Final = 64
MAX_CONFIG_KEY_LEN: Final = 64
MAX_CONFIG_VALUE_LEN: Final = 1024
MAX_SECRET_REF_LEN: Final = 1024

# The secret_ref rule and the secret-key rule mirror ``carto_common.settings`` and
# ``carto_common.logging`` (this package depends on pydantic only; a core test holds the
# mirrors together).
SECRET_REF_SCHEMES: Final[tuple[str, ...]] = ("vault", "aws-sm", "azure-kv", "gcp-sm", "local")
"""Allowed ``secret_ref`` schemes (spec 14.3), without the ``://``."""
_SCHEME_SEPARATOR: Final = "://"
_ALLOWED_SCHEMES_TEXT: Final = ", ".join(f"{scheme}://" for scheme in SECRET_REF_SCHEMES)

_SECRET_WORDS: Final = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "cookie",
    "session",
    "bearer",
    "credential",
    "pwd",
    "passphrase",
    "pass",
    "auth",
    "signature",
    "sig",
)
_SECRET_KEY_WORDS: Final = frozenset(_SECRET_WORDS) | frozenset(f"{w}s" for w in _SECRET_WORDS)
_SECRET_PAIRS: Final = (("api", "key"), ("private", "key"), ("client", "secret"), ("access", "key"))
_SECRET_KEY_PAIRS: Final = frozenset(_SECRET_PAIRS) | frozenset(
    (first, f"{second}s") for first, second in _SECRET_PAIRS
)
_SECRET_KEY_QUALIFIERS: Final = frozenset(
    {
        "count",
        "total",
        "len",
        "length",
        "size",
        "num",
        "number",
        "ref",
        "version",
        "name",
        "type",
        "kind",
        "present",
        "expires",
        "expiry",
        "ttl",
        "scheme",
    }
)
_CAMEL_BOUNDARY: Final = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=[0-9])"
)
_KEY_SEPARATORS: Final = re.compile(r"[._\s-]+")


def config_key_is_secret(name: str) -> bool:
    """Whether a configuration key names a secret: lower-cased and split on ``_``, ``-``,
    ``.``, whitespace, camelCase and letter-digit boundaries, a segment is one of the secret
    words (``password``, ``token``, ``auth``, ``session``, ...) or two adjacent segments are
    ``api key``, ``private key``, ``client secret`` or ``access key``, unless the last segment
    is a qualifier that describes the secret rather than holds it (``secret_ref``,
    ``token_count``, ``password_present``)."""
    spaced = _CAMEL_BOUNDARY.sub("_", name)
    segments = [segment for segment in _KEY_SEPARATORS.split(spaced.lower()) if segment]
    if not segments or segments[-1] in _SECRET_KEY_QUALIFIERS:
        return False
    if any(segment in _SECRET_KEY_WORDS for segment in segments):
        return True
    return any(pair in _SECRET_KEY_PAIRS for pair in itertools.pairwise(segments))


def validate_secret_ref(value: str) -> str:
    """Accept ``<scheme>://<path>`` for the spec 14.3 schemes and reject everything else, so
    a literal secret in the ``secret_ref`` slot is refused at the contract. Checks shape only;
    the messages list the allowed schemes and never repeat the input."""
    scheme, separator, path = value.partition(_SCHEME_SEPARATOR)
    if not separator or scheme not in SECRET_REF_SCHEMES:
        msg = f"secret_ref must be '<scheme>://<path>' with one of {_ALLOWED_SCHEMES_TEXT}"
        raise ValueError(msg)
    if not path:
        msg = f"secret_ref needs a non-empty path after '{scheme}://'"
        raise ValueError(msg)
    if not value.isprintable() or any(char.isspace() for char in value):
        msg = "secret_ref must contain only printable, non-whitespace characters"
        raise ValueError(msg)
    return value


def _require_config_value(value: str) -> str:
    """A config string value carries no value shape; the message never echoes it."""
    shape = value_shape(value)
    if shape is not None:
        msg = (
            f"a config value holds {shape}, which is a value a secret could travel in; the edge "
            "sends configuration without secrets and core refuses it otherwise"
        )
        raise ValueError(msg)
    return value


SecretRef = Annotated[
    str, Field(max_length=MAX_SECRET_REF_LEN), AfterValidator(validate_secret_ref)
]
"""A reference to a secret in a customer store (spec 14.3), never the secret itself."""
ConfigText = Annotated[
    str, Field(max_length=MAX_CONFIG_VALUE_LEN), AfterValidator(_require_config_value)
]
"""A config string value: bounded and under the value-shape rule of an attribute value."""
ConfigValue = ConfigText | int | float | bool | None | list[ConfigText]
"""A connector configuration value as shown in the console: scalars and lists of strings only,
no nesting, so a secret cannot hide inside a structure core would not look at."""


class InventorySystem(ContractModel):
    id: SystemId
    name: Annotated[str, Field(min_length=1, max_length=128)]
    owner_group: Annotated[str, Field(max_length=128)] = ""
    criticality: Annotated[str, Field(pattern=r"^(low|medium|high)$")] = "medium"


class InventorySource(ContractModel):
    id: SourceId
    system_id: SystemId
    type: Annotated[str, Field(min_length=1, max_length=32)]
    enabled: bool = True
    config: Annotated[
        dict[Annotated[str, Field(max_length=MAX_CONFIG_KEY_LEN)], ConfigValue],
        Field(max_length=MAX_CONFIG_KEYS),
    ] = Field(default_factory=dict)
    secret_ref: SecretRef | None = None
    backfill_days: Annotated[int | None, Field(ge=0, le=400)] = None
    schedule: Annotated[str | None, Field(max_length=128)] = None
    """How the edge polls it, as text ("every 60 s", "push"); informational."""
    last_test: Annotated[str | None, Field(max_length=1024)] = None
    """The last read-only verification result (connector ``test()``), redacted by the edge."""
    last_test_at: UtcTimestamp | None = None

    @field_validator("config", mode="before")
    @classmethod
    def _keys_name_no_secret(cls, config: object) -> object:
        """Config keys under :func:`config_key_is_secret` are refused here, not per key, so a
        rejected key never appears in an error location (a dict key is part of the location);
        the message names the rule, never the key."""
        if isinstance(config, dict):
            for key in config:
                if isinstance(key, str) and config_key_is_secret(key):
                    msg = (
                        "a config key names a secret; the edge removes such keys before "
                        "sending and core refuses them otherwise"
                    )
                    raise ValueError(msg)
        return config


class InventoryFieldPolicy(ContractModel):
    """One admin pin of the sources file (spec 8.3; ``FieldPolicyPin`` on the edge)."""

    field: Annotated[str, Field(min_length=3, max_length=512)]
    field_class: FieldClassName
    policy: PolicyName
    forms: Annotated[list[Annotated[str, Field(max_length=32)]], Field(max_length=16)] = Field(
        default_factory=list
    )
    reason: Annotated[str, Field(max_length=256)] = ""


class EdgeInventory(ContractModel):
    """One ``POST /internal/inventory`` body."""

    schema_version: SchemaVersion
    tenant_id: TenantId
    edge_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
    """Which edge reports (its certificate subject or configured name); core keeps one inventory
    per edge and merges them for the console."""
    sent_at: UtcTimestamp
    sources_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    systems: Annotated[list[InventorySystem], Field(max_length=MAX_INVENTORY_SYSTEMS)]
    sources: Annotated[list[InventorySource], Field(max_length=MAX_INVENTORY_SOURCES)]
    field_policies: Annotated[list[InventoryFieldPolicy], Field(max_length=MAX_INVENTORY_PINS)] = (
        Field(default_factory=list)
    )
