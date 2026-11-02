# carto-common

Runtime pieces every carto service shares (spec Section 20, ADR 0008): ULIDs, structlog
configuration with the redaction processor, and the settings schemas. No I/O, no network, no
secrets: a `secret_ref` is checked for shape only and never resolved here.

| Module | What it provides | Spec |
| --- | --- | --- |
| `carto_common.ids` | `new_ulid`, `ulid_from_parts`, `derive_ulid`, `ulid_timestamp_ms`, `is_ulid`, `ULID_REGEX`, `ULID_CHARS`, `ULID_PATTERN` | 5.4, 7.1, 8.1, ADR 0006 |
| `carto_common.logging` | `configure_logging`, `get_logger`, the `redact` processor, `mask_string`, `identifier_shape` | 14.12, 2.3 invariant 7, 14.3 |
| `carto_common.settings` | `SecretRef`, `validate_secret_ref`, `CartoBaseSettings`, `RetentionSettings`, `LlmSettings`, `TelemetrySettings`, `ProductSettings` | 20, 14.3, 14.10, 9.9, 2.3 invariants 4 and 5, ADR 0004 |

The entry points (`configure_logging`, `get_logger`, `redact`, the id functions and the settings
models) are re-exported from `carto_common`; helpers and constants such as `mask_string`,
`identifier_shape`, `ULID_REGEX` and the mask markers live in their modules. Tests live in
`tests/test_common_*.py`; run them with `uv run pytest packages/carto-common/tests`.

## Ids

A ULID is a 48-bit millisecond timestamp followed by 80 bits, rendered as 26 Crockford base32
characters (`0-9A-HJKMNP-TV-Z`, first character `0` to `7`). Lexical order follows timestamp order.
Only the canonical upper-case rendering is accepted; `ULID_REGEX` is the pattern to put in
`pydantic.Field(pattern=...)` and in JSON Schema, `ULID_CHARS` the same pattern without anchors for
embedding in larger expressions.

- `new_ulid()`: current time plus 10 bytes from `secrets.token_bytes`.
- `ulid_from_parts(timestamp_ms, randomness)`: timestamp `0..2**48-1`, exactly 10 bytes.
- `derive_ulid(timestamp_ms, *parts)`: deterministic. The 80 bits are the first 10 bytes of SHA-256
  over the UTF-8 encoded parts joined with one `0x00` byte. At least one part is required. M1's edge
  derives `event_id = derive_ulid(observed_at_ms, source_id, locator)` so a record read twice gets
  the same id and core dedupes on it (spec 8.1); the locator is the ADR 0006 key.
- `ulid_timestamp_ms(ulid)` reads the timestamp back; `is_ulid(value)` checks the shape.

## Logging

```python
from carto_common import configure_logging, get_logger

configure_logging("edge", level="INFO", json_output=True)  # once, at process start
log = get_logger(component="gateway")
log.info("batch forwarded", source_id="src_orders_log", events=412)
```

`configure_logging` installs this processor chain: `merge_contextvars`, a processor that stamps
`service=<name>` on every event, `add_log_level`, `TimeStamper(fmt="iso", utc=True)`,
`format_exc_info` (so traceback text goes through the masks), `redact`, then `JSONRenderer` (one
JSON object per line) or `ConsoleRenderer` when `json_output=False`. Output goes to `sys.stdout`
unless `stream=` is given (tests pass an `io.StringIO`). Levels: `critical`, `error`, `warning`,
`info`, `debug`. If `get_logger` is called before `configure_logging`, it configures the defaults
with `service=unconfigured` first: a forgotten call can never produce unredacted output.

The same chain, with the originating logger's name added as `logger`, becomes the only handler of
the standard library's root logger, replacing whatever was installed before. Records from
third-party libraries (uvicorn, httpx, asyncpg, SQLAlchemy) and stray `logging.getLogger()` calls
are therefore rendered through `redact` as well and never reach `logging.lastResort`. Libraries
that install their own handlers must be told not to: start uvicorn with `log_config=None` so its
`uvicorn.*` loggers propagate to the root.

Python warnings and uncaught exceptions take the same path (ADR 0040). `configure_logging`
replaces `warnings.showwarning`, so a warning becomes a WARNING record of the `py.warnings`
logger, and installs `sys.excepthook`, `threading.excepthook` and `sys.unraisablehook`, so an
uncaught exception, one raised in a thread, and one ignored in `__del__` become CRITICAL events
(`uncaught exception`, `uncaught exception in thread`, `unraisable exception`) with the traceback
in `exception`, masked like any other string. The interpreter's default hooks, which print the
raw message to stderr, are never called; a hook someone else installed first (a test runner's)
is called after logging.

### Redaction rules (`redact`, spec 14.12 and 2.3 invariant 7)

Keys are checked first, then every string value, the `event` message and dictionary keys included.
Value rules run in the order of the table.

| Rule | Trigger | Result |
| --- | --- | --- |
| Secret-named key | Lower-case the key, split on `_`, `-`, `.`, space, camelCase and letter-digit boundaries. Any segment is `password`, `passwd`, `passphrase`, `pass`, `pwd`, `secret`, `token`, `apikey`, `authorization`, `auth`, `cookie`, `session`, `bearer`, `credential`, `signature` or `sig` (singular or plural), or two adjacent segments are `api key`, `private key`, `client secret` or `access key`; unless the last segment is a qualifier: `count`, `total`, `len`, `length`, `size`, `num`, `number`, `ref`, `version`, `name`, `type`, `kind`, `present`, `expires`, `expiry`, `ttl`, `scheme` | value becomes `[REDACTED]`, whatever its type |
| Body key | Normalised key is `body`, `request_body`, `response_body`, `payload`, `raw`, `raw_value`, `raw_values`, `values` or `record` | value becomes `[DROPPED]` |
| PEM block | `-----BEGIN` through the closing `-----END ...-----`, across lines | `[PEM]` |
| JWT | Three base64url segments joined by dots, the first starting with `eyJ`, at a word boundary | `[JWT]` |
| carto token | `t<1-4 digits>.<22 base64url characters>` (spec 8.4) | `[TOKEN]` |
| AWS access key id | `AKIA` or `ASIA` followed by 16 characters `[0-9A-Z]` | `[AWS_KEY]` |
| URL userinfo | `scheme://user:password@host` (the DSNs and SFTP URLs of spec 8.1) | `scheme://user:[REDACTED]@host` |
| E-mail address | `local@domain.tld`, not preceded by `/` (a DSN user name is not a mailbox) | `[EMAIL]` |
| High-entropy run | 32 or more characters of `[A-Za-z0-9+/=_-]`, at least three of the classes upper, lower, digit, other, Shannon entropy of at least 3.5 bits per character | `[HIGH_ENTROPY]` |
| Secret assignment in text | A secret word from the key rule (pairs joined by `_`, `-`, `.` or space) optionally closed by a quote (escaped or not), followed by `=` or `:` and a value: `password=hunter2`, `x-api-key: ...`, `Authorization: Basic ...`, `?token=...&next=`, `{"password": "..."}`, `{'password': '...'}`, `;PWD={...};`. A quoted value right after the word (`with secret "..."`) counts too. The short words `pwd`, `pass`, `auth` and `sig` must start a word or a camelCase hump (`dbPass=` matches, `bypass=` and `oauth=` do not) | the value becomes `[REDACTED]`; a value an earlier rule masked (`token=[JWT]`) keeps that mask |
| Hex run | Eight or more lower-case hex characters holding at least one letter and one digit (UUIDs, content hashes, the simulator's `mk<8 hex>` leak markers) | its shape |
| Identifier-shaped token | A whitespace-delimited token with a run of four or more digits, or four or more digits in total when it also contains `-` or `_` (`88-210`, `X9-0442`), that is not an ISO date (`YYYY-MM-DD`), time or timestamp (`YYYY-MM-DDTHH:MM:SS[.fff][Z or +hh:mm]`) with real month, day, hour, minute and second ranges | its shape: digits to `9`, letters to `A`, punctuation kept (spec 8.3); mask markers and ULIDs inside the token are kept |
| Number (ADR 0040) | An `int` or `float` (not `bool`) whose decimal text holds four or more digits, leading zeros not counted, unless the last segment of its own key is a metric qualifier: `count`, `counts`, `attempt`, `attempts`, `retries`, `rows`, `records`, `events`, `batches`, `samples`, `bytes`, `ms`, `seconds`, `second`, `days`, `depth`, `size`, `len`, `length`, `limit`, `port`, `status`, `pid`, `version`, `versions`. The qualifier covers the number directly under the key, not the items of a list | its shape as a string: `customer_id=48812213` logs `"99999999"`, `total=207.81` logs `"999.99"` |
| Folded keys | Two keys that mask to the same shape | the second becomes `<shape>#2` |
| Nesting deeper than 16 containers, or a value that raises while walked | | `[REDACTION_ERROR]` for that key |

Examples: `SO-0004471` logs as `AA-9999999`, `order 4471 failed` as `order 9999 failed`, the
warehouse `po_num` `88-210` as `99-999`, `user:1234:t1.<token>` as `AAAA:9999:[TOKEN]`,
`postgres://carto:hunter2@db/carto` as `postgres://carto:[REDACTED]@db/carto`,
`jane.roe+mk0f1e2d3c@example.com` as `[EMAIL]` and `12 Mk0f1e2d3c Street` as
`12 Mk9A9A9A9A Street`. `order 471 failed`, `c-881`, `DC-03`, `took 0.123s`, `python 3.12.15`,
`10.0.0.1`, `1,234`, `2026-10-06`, `2026-10-06T12:34:56.789Z` and a ULID such as
`01ARZ3NDEKTSV4RRFFQ69G5FAV` (the product's own `event_id`, kept so a record can be traced across
services) are left alone. `token_count=3` survives (its last segment is a qualifier); `auth_token`,
`secret_value`, `password_hash`, `session_id`, `cookies` and `x-api-key` are redacted; `secret_ref`
survives because a reference is not a value.

Other types: `None`, `bool` and numbers of fewer than four digits pass through untouched. Log a
count under a key ending in a metric qualifier (`events=412`, `duration_ms=12`,
`order_count=4471`); any other number of four or more digits is treated as a value and masked
(`total` is deliberately not a qualifier: `total=207.81` is an order amount). Dates and times are rendered
ISO 8601, `bytes` are decoded and masked, enums contribute their value, pydantic models and
dataclasses are walked like mappings, sets and tuples become lists, and any other object is
`str()`-ed and masked. The processor returns only JSON-native values and never raises.

The identifier trigger is the stated "run of four or more digits" plus the hyphen or underscore
case: the spec's own examples of identifiers (`PO_num 88-210`, `merchant_ref X9-0442`, spec 1.1)
carry four digits without a run of four, and leaving them in clear would fail the spec 18.3 leak
test, while versions, durations, IP addresses and grouped counts stay readable. ULIDs are left in
clear on purpose, so a customer identifier that happens to be a canonical ULID would be logged as
is.

### What is not masked

- Person names, street addresses, free text and any PII that is neither an e-mail address nor
  identifier-shaped. Do not log them: the spec 18.3 leak test plants markers in exactly those
  fields and fails the build when one appears in a service log.
- A secret shorter than 32 characters, or made of fewer than three character classes, under a key
  that is not secret-named and outside a URL userinfo or `key=value` context. Name the key after
  what it holds (`password`, `token`, `api_key`) and the key rule hides it whatever the value.
- Numbers of fewer than four digits, numbers under a metric key, and canonical ULIDs (see above).
- A hex run made only of letters (`deadbeef`), which cannot be told from a word.
- Records emitted through a handler a library installed for itself (uvicorn without
  `log_config=None`): `configure_logging` owns the root logger, nothing else.

## Settings

All models forbid unknown fields, are frozen, and hide inputs from validation errors (a secret
pasted where a reference belongs is never echoed). Environment variables use the `CARTO_` prefix
and `__` between nesting levels. An unknown key inside a known section (`CARTO_RETENTION__BOGUS`)
and an unknown constructor keyword are rejected. A top-level variable that matches no field
(`CARTO_TENNANT_ID`, or `CARTO_RETENTION_EVENTS_DAYS` with a single underscore) is ignored, because
pydantic-settings reads only declared fields from the environment and one Compose `.env` is shared
by every service; log the effective settings at startup so a typo is visible.

| Setting | Environment variable | Default | Allowed |
| --- | --- | --- | --- |
| `tenant_id` | `CARTO_TENANT_ID` | `default` | `^[a-z0-9][a-z0-9_-]{0,63}$` |
| `retention.events_days` | `CARTO_RETENTION__EVENTS_DAYS` | 30 | 7 to 400 |
| `retention.txn_membership_days` | `CARTO_RETENTION__TXN_MEMBERSHIP_DAYS` | 90 | 30 to 400, at least `events_days` |
| `retention.aggregates_months` | `CARTO_RETENTION__AGGREGATES_MONTHS` | 13 | 3 to 36 |
| `retention.alerts_months` | `CARTO_RETENTION__ALERTS_MONTHS` | 13 | 3 to 36 |
| `retention.audit_days` | `CARTO_RETENTION__AUDIT_DAYS` | 365 | 90 or more |
| reveal vault entries | none | follows `events_days` | spec 14.10 |
| `llm.enabled` | `CARTO_LLM__ENABLED` | `false` | requires `llm.provider` when `true` |
| `llm.provider` | `CARTO_LLM__PROVIDER` | none | `anthropic`, `bedrock`, `vertex` |
| `llm.endpoint` | `CARTO_LLM__ENDPOINT` | none | must start with `https://` |
| `llm.model` | `CARTO_LLM__MODEL` | none | free text |
| `telemetry.enabled` | `CARTO_TELEMETRY__ENABLED` | `false` | health metrics only, never event data |

`ProductSettings` holds these shared fields; a service subclasses it (or `CartoBaseSettings`) to add
its own. Defaults are the ADR 0004 answers to spec Section 23 questions 3 and 5.

## SecretRef

`SecretRef` is `Annotated[str, AfterValidator(validate_secret_ref)]`: a reference to where a
secret lives, never the secret (spec 14.3, 8.1). Accepted shape is `<scheme>://<path>` with a
non-empty path, only printable non-whitespace characters (no control, zero-width or no-break
characters), at most 1024 characters, lower-case scheme:

| Scheme | Store |
| --- | --- |
| `vault://` | HashiCorp Vault |
| `aws-sm://` | AWS Secrets Manager |
| `azure-kv://` | Azure Key Vault |
| `gcp-sm://` | GCP Secret Manager |
| `local://` | KMS-wrapped store in PostgreSQL, Compose pilots only |

Everything else (`http://`, bare paths, upper-case schemes, missing path) is rejected with a message
that lists the allowed schemes and does not repeat the input. Resolution happens in the service that
owns the connector (M1), fetched at use and cached in memory for at most 15 minutes. Models that
carry a `SecretRef` should set `hide_input_in_errors=True` as the settings models here do.
