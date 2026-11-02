"""Synthetic names, addresses and leak-test markers (spec 0.1 item 7, 18.3).

Every value here is invented. Emails and addresses embed a marker token ``mk<8 lowercase hex>``
drawn from the scenario RNG so the leak test (spec 18.3) can scan for it; the token is unique per
value and embedded unchanged, so ``token in value`` holds for every recorded token
(:class:`~carto_simulator.ground_truth.MarkerSet`). Names come from fixed lists so phonetic forms
(spec 8.4) are stable across runs.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

FIRST_NAMES: tuple[str, ...] = (
    "Jane",
    "John",
    "Maria",
    "Wei",
    "Aisha",
    "Carlos",
    "Priya",
    "Liam",
    "Olivia",
    "Noah",
    "Emma",
    "Lucas",
    "Sofia",
    "Mateo",
    "Amara",
    "Ethan",
    "Hana",
    "Daniel",
    "Fatima",
    "Victor",
    "Nora",
    "Samuel",
    "Chloe",
    "Omar",
    "Isabel",
    "Arjun",
    "Grace",
    "Tomas",
    "Yuki",
    "Elena",
    "Marcus",
    "Leila",
    "Felix",
    "Ingrid",
    "Rafael",
    "Zara",
    "Henry",
    "Mei",
    "Oscar",
    "Ruth",
)

LAST_NAMES: tuple[str, ...] = (
    "Smith",
    "Johnson",
    "Garcia",
    "Chen",
    "Okafor",
    "Martinez",
    "Patel",
    "Nguyen",
    "Walsh",
    "Kim",
    "Lopez",
    "Brown",
    "Miller",
    "Schmidt",
    "Rossi",
    "Novak",
    "Haddad",
    "Kowalski",
    "Fischer",
    "Silva",
    "Andersen",
    "Dubois",
    "Moreau",
    "Ivanova",
    "Tanaka",
    "Sato",
    "Mensah",
    "Abebe",
    "Costa",
    "Larsen",
    "Murphy",
    "O'Brien",
    "Reyes",
    "Castillo",
    "Khan",
    "Singh",
    "Jensen",
    "Berg",
    "Lindqvist",
    "Varga",
)

CITIES: tuple[tuple[str, str], ...] = (
    ("Austin", "TX"),
    ("Denver", "CO"),
    ("Portland", "OR"),
    ("Columbus", "OH"),
    ("Raleigh", "NC"),
    ("Boise", "ID"),
    ("Tucson", "AZ"),
    ("Madison", "WI"),
    ("Richmond", "VA"),
    ("Omaha", "NE"),
    ("Tampa", "FL"),
    ("Albany", "NY"),
)

CLERKS: tuple[str, ...] = (
    "jsmith",
    "mlopez",
    "rchen",
    "apatel",
    "dkim",
    "lnguyen",
    "bwalsh",
    "tokafor",
)

SERVICE_ACCOUNT = "svc_wms_integration"

MARKER_PREFIX = "mk"
MARKER_HEX_LENGTH = 8


@dataclass(frozen=True, slots=True)
class Person:
    """One synthetic customer: a name plus the marker-bearing contact fields."""

    name: str
    email: str
    email_marker: str
    address: str
    address_marker: str


class MarkerFactory:
    """Draws unique marker tokens from the scenario RNG (spec 18.3)."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._seen: set[str] = set()
        self.tokens: list[str] = []

    def new_token(self) -> str:
        """Return ``mk`` + 8 lowercase hex digits never returned before by this factory."""
        while True:
            value = self._rng.getrandbits(4 * MARKER_HEX_LENGTH)
            token = f"{MARKER_PREFIX}{value:0{MARKER_HEX_LENGTH}x}"
            if token not in self._seen:
                self._seen.add(token)
                self.tokens.append(token)
                return token


def _email_local_part(name: str) -> str:
    first, _, last = name.partition(" ")
    return "".join(ch for ch in f"{first}.{last}".lower() if ch.isalpha() or ch == ".")


def draw_person(rng: random.Random, markers: MarkerFactory) -> Person:
    """Draw a name, an email and a shipping address, consuming the RNG in a fixed order."""
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    name = f"{first} {last}"
    email_marker = markers.new_token()
    email = f"{_email_local_part(name)}+{email_marker}@example.com"
    number = rng.randint(1, 9999)
    address_marker = markers.new_token()
    city, state = rng.choice(CITIES)
    zip_code = f"{rng.randint(0, 99999):05d}"
    address = f"{number} {address_marker} Street, {city}, {state} {zip_code}"
    return Person(
        name=name,
        email=email,
        email_marker=email_marker,
        address=address,
        address_marker=address_marker,
    )


# ---------------------------------------------------------------------------------------------
# Secret-shaped values planted into noise lines (spec 18.3 ``secret_values``, ADR 0068)
# ---------------------------------------------------------------------------------------------

PEM_HEADER = "-----BEGIN RSA PRIVATE KEY-----"
PEM_FOOTER = "-----END RSA PRIVATE KEY-----"
_UPPER_DIGITS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_MIXED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_BASE64 = _MIXED + "+/"
SECRET_NOISE_SHARE = 0.05
"""Share of noise lines that carry one of the planted secrets (one shape per line)."""


@dataclass(frozen=True, slots=True)
class PlantedSecrets:
    """One run's secrets: an AWS access key id, a bearer token, a password assigned in prose,
    a PEM private key on one line and a DSN with userinfo. Every value is invented, drawn from
    the scenario RNG, and recorded in ``markers.json`` under ``secret_values``; the same line
    repeats verbatim, which is how a retry loop logs a secret."""

    aws_key_id: str
    bearer_token: str
    password: str
    pem_body: str
    dsn_password: str
    dsn: str

    @property
    def values(self) -> list[str]:
        """What the leak test scans for: the secrets themselves, the PEM header line and the
        whole DSN."""
        return [
            self.aws_key_id,
            self.bearer_token,
            self.password,
            PEM_HEADER,
            self.pem_body,
            self.dsn_password,
            self.dsn,
        ]

    def messages(self) -> list[str]:
        """The five noise messages, in a fixed order (a caller picks one by index)."""
        return [
            f"rotating object store credentials, access key {self.aws_key_id} retired",
            f"upstream call failed Authorization: Bearer {self.bearer_token} status 502",
            f"connecting to sftp as user=svc_sync password={self.password} attempt",
            f"loaded key material {PEM_HEADER} {self.pem_body} {PEM_FOOTER}",
            f"db reconnect {self.dsn} ok",
        ]


def _mixed(rng: random.Random, alphabet: str, length: int) -> str:
    """A string over ``alphabet`` that holds an upper-case letter, a lower-case letter and a
    digit when the alphabet has them (so entropy-based secret detectors see all classes)."""
    while True:
        text = "".join(rng.choices(alphabet, k=length))
        classes = (
            any(c.isupper() for c in text) or not any(c.isupper() for c in alphabet),
            any(c.islower() for c in text) or not any(c.islower() for c in alphabet),
            any(c.isdigit() for c in text) or not any(c.isdigit() for c in alphabet),
        )
        if all(classes):
            return text


def draw_secrets(rng: random.Random, host: str) -> PlantedSecrets:
    """Draw the secrets of one run, consuming the RNG in a fixed order."""
    aws_key_id = "AKIA" + _mixed(rng, _UPPER_DIGITS, 16)
    bearer_token = _mixed(rng, _MIXED, 40)
    password = _mixed(rng, _MIXED, 16)
    pem_body = "MII" + _mixed(rng, _BASE64, 60)
    dsn_password = _mixed(rng, _MIXED, 16)
    dsn = f"postgres://svc_app:{dsn_password}@{host}:5432/app"
    return PlantedSecrets(
        aws_key_id=aws_key_id,
        bearer_token=bearer_token,
        password=password,
        pem_body=pem_body,
        dsn_password=dsn_password,
        dsn=dsn,
    )
