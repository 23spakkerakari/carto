"""PII detection for the classifier and the template redactor (spec 8.3 rule 2; ADR 0020).

Two detectors behind one :class:`PiiDetector` protocol:

- :class:`PresidioDetector`: Presidio's ``AnalyzerEngine`` over an **explicit**
  ``RecognizerRegistry`` (:func:`build_registry`) and a spaCy ``NlpEngine`` for the configured
  model (``en_core_web_sm`` is a locked dependency). The registry holds e-mail, phone, US SSN,
  credit card (Luhn), IBAN, US passport, US driver licence, US medical licence and spaCy
  ``PERSON`` and ``LOCATION``; nothing else. ``load_predefined_recognizers`` is never called
  and the URL recognizer is never registered. The stock e-mail recognizer validates its matches
  through ``tldextract``, which fetches the public suffix list from the internet on first use;
  the registry carries a subclass whose validation is a local syntax check instead, and
  :func:`build_registry` refuses a registry that still has a network path (spec 2.3 invariant
  4). The spaCy model must already be installed: Presidio's own loader would otherwise call
  ``spacy.cli.download``, so the model is checked first and a missing one degrades to the regex
  detector. The engine loads lazily, once, under a lock; a load failure logs one warning and
  switches the instance to :class:`RegexDetector` for good.
- :class:`RegexDetector`: e-mail, E.164 and North American phone numbers, US SSN, credit card
  numbers (13 to 19 digits, Luhn-valid, Presidio's leading-digit rule) and IBANs (mod-97). Used
  when ``pii.enabled`` is false or the model cannot load (ADR 0020). It knows nothing about
  names: name hints in :mod:`carto_edge.pipeline.classify` carry that load then.

Every detector sees at most :data:`MAX_TEXT_BYTES` per call (:func:`truncate_text`).
:func:`detect_many` runs one detector over a list of sample values in as few calls as fit the
budget and maps the hits back to each sample. :func:`mask` replaces hits by ``<ENTITY_TYPE>``.
:func:`looks_like_secret` is the rule 1 value check (JWT, PEM block, AWS access key id, long
high-entropy base64), shared by the classifier and the attribute hygiene in
:mod:`carto_edge.pipeline.redact`.

Nothing in this module logs or raises with a piece of the analysed text.
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from carto_common.logging import get_logger
from carto_edge.config import PiiSettings

__all__ = [
    "MAX_TEXT_BYTES",
    "PRESIDIO_ENTITIES",
    "SECRET_MIN_LEN",
    "PiiDetector",
    "PiiHit",
    "PiiSetupError",
    "PresidioDetector",
    "RegexDetector",
    "build_registry",
    "detect_many",
    "detector_from_settings",
    "looks_like_secret",
    "mask",
    "truncate_text",
]

MAX_TEXT_BYTES: Final = 4096
"""Longest input one detector call sees, in UTF-8 bytes (ADR 0020)."""

PRESIDIO_ENTITIES: Final[tuple[str, ...]] = (
    "PERSON",
    "LOCATION",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "CREDIT_CARD",
    "IBAN_CODE",
    "MEDICAL_LICENSE",
)
"""Entity types the registry recognizes; ``analyze`` is restricted to exactly these."""

SPACY_LABELS_IGNORED: Final[tuple[str, ...]] = (
    "CARDINAL",
    "DATE",
    "EVENT",
    "FAC",
    "LANGUAGE",
    "LAW",
    "MONEY",
    "NORP",
    "ORDINAL",
    "ORG",
    "PERCENT",
    "PRODUCT",
    "QUANTITY",
    "TIME",
    "WORK_OF_ART",
)
"""spaCy NER labels the registry does not use (only PERSON, GPE and LOC feed PERSON and
LOCATION); listed as ignored so Presidio neither maps nor warns about them per call."""

Backend = Literal["unloaded", "presidio", "regex"]


class PiiSetupError(RuntimeError):
    """The Presidio engine cannot be built the way spec 2.3 invariant 4 requires."""


@dataclass(frozen=True, slots=True, order=True)
class PiiHit:
    """One detected entity: ``[start, end)`` offsets into the analysed text."""

    entity_type: str
    start: int
    end: int
    score: float


class PiiDetector(Protocol):
    def detect(self, text: str) -> list[PiiHit]:
        """Hits in ``text`` (after truncation), sorted by start, non-overlapping."""
        ...


def _logger() -> Any:
    return get_logger(component="carto_edge.pipeline.pii")


# ---------------------------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------------------------


def _utf8(text: str) -> tuple[str, bytes]:
    """``text`` and its UTF-8 bytes; a lone surrogate (which has no UTF-8 form) is replaced by
    U+FFFD first, so no detector input can raise."""
    try:
        return text, text.encode("utf-8")
    except UnicodeEncodeError:
        clean = text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
        return clean, clean.encode("utf-8")


def truncate_text(text: str) -> str:
    """Cut ``text`` to :data:`MAX_TEXT_BYTES` UTF-8 bytes without splitting a character. A lone
    surrogate becomes U+FFFD."""
    text, encoded = _utf8(text)
    if len(encoded) <= MAX_TEXT_BYTES:
        return text
    return encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")


def _normalize(hits: list[PiiHit], length: int) -> list[PiiHit]:
    """Clip to the text, drop empty spans, sort by start and remove overlaps (the earlier,
    then longer, hit wins)."""
    kept: list[PiiHit] = []
    last_end = -1
    for hit in sorted(hits, key=lambda item: (item.start, -item.end, -item.score)):
        start = max(0, hit.start)
        end = min(length, hit.end)
        if start >= end or start < last_end:
            continue
        kept.append(PiiHit(hit.entity_type, start, end, hit.score))
        last_end = end
    return kept


def mask(text: str, hits: Sequence[PiiHit]) -> tuple[str, int]:
    """Replace every hit by ``<ENTITY_TYPE>``; overlapping hits merge into one replacement.
    Returns the masked text and the number of replacements."""
    spans: list[tuple[int, int, str]] = []
    for hit in sorted(hits, key=lambda item: (item.start, -item.end)):
        start = max(0, hit.start)
        end = min(len(text), hit.end)
        if start >= end:
            continue
        if spans and start < spans[-1][1]:
            previous = spans[-1]
            spans[-1] = (previous[0], max(previous[1], end), previous[2])
            continue
        spans.append((start, end, hit.entity_type))
    if not spans:
        return text, 0
    pieces: list[str] = []
    cursor = 0
    for start, end, entity_type in spans:
        pieces.append(text[cursor:start])
        pieces.append(f"<{entity_type}>")
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), len(spans)


_SAMPLE_MAX_BYTES: Final = 1024
_SEPARATOR: Final = "\n"


def detect_many(detector: PiiDetector, texts: Sequence[str]) -> list[list[PiiHit]]:
    """Run ``detector`` over ``texts`` and return the hits per text, offsets relative to it.

    Texts are joined with newlines into chunks of at most :data:`MAX_TEXT_BYTES` so that a
    batch of samples costs a few calls, not one per value (the spaCy pipeline dominates per
    call). Each text is cut at 1 KB first. A hit that spans a separator is attributed to the
    text it starts in and clipped there.
    """
    results: list[list[PiiHit]] = [[] for _ in texts]
    if not texts:
        return results
    chunk: list[tuple[int, str]] = []
    chunk_bytes = 0

    def flush() -> None:
        if not chunk:
            return
        joined = _SEPARATOR.join(piece for _index, piece in chunk)
        hits = detector.detect(joined)
        starts: list[tuple[int, int, int]] = []  # start, end and the index of the text piece
        offset = 0
        for index, piece in chunk:
            starts.append((offset, offset + len(piece), index))
            offset += len(piece) + len(_SEPARATOR)
        position = 0
        for hit in hits:
            while position < len(starts) and starts[position][1] <= hit.start:
                position += 1
            if position >= len(starts):
                break
            start, end, index = starts[position]
            if hit.start < start:
                continue
            results[index].append(
                PiiHit(hit.entity_type, hit.start - start, min(hit.end, end) - start, hit.score)
            )
        chunk.clear()

    for index, text in enumerate(texts):
        piece = _utf8(text)[1][:_SAMPLE_MAX_BYTES].decode("utf-8", errors="ignore")
        size = len(piece.encode("utf-8")) + len(_SEPARATOR)
        if chunk and chunk_bytes + size > MAX_TEXT_BYTES:
            flush()
            chunk_bytes = 0
        chunk.append((index, piece))
        chunk_bytes += size
    flush()
    return results


# ---------------------------------------------------------------------------------------------
# Secret-shaped values (spec 8.3 rule 1)
# ---------------------------------------------------------------------------------------------

_JWT: Final = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_PEM: Final = re.compile(r"-----BEGIN [A-Z ]+-----")
_AWS_KEY: Final = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
_ENTROPY_RUN: Final = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_ENTROPY_MIN_BITS: Final = 3.5
SECRET_MIN_LEN: Final = 20
"""Shortest secret-shaped value: an AWS access key id is exactly 20 characters."""


def _shannon_bits(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def _is_high_entropy(run: str) -> bool:
    """Long base64-looking run with upper and lower case letters and digits and at least 3.5
    bits of entropy per character. Hex-only strings (UUIDs, digests) and upper-case-only
    strings (ULIDs, reference numbers) are identifiers, not secrets, and never qualify."""
    has_upper = any(char.isupper() for char in run)
    has_lower = any(char.islower() for char in run)
    has_digit = any(char.isdigit() for char in run)
    return has_upper and has_lower and has_digit and _shannon_bits(run) >= _ENTROPY_MIN_BITS


def looks_like_secret(value: str) -> bool:
    """Spec 8.3 rule 1 value patterns: JWT, PEM block, AWS access key id, high-entropy run.
    Nothing shorter than :data:`SECRET_MIN_LEN` can match, so short values cost one
    comparison."""
    if len(value) < SECRET_MIN_LEN:
        return False
    if _JWT.search(value) or _PEM.search(value) or _AWS_KEY.search(value):
        return True
    return any(_is_high_entropy(match.group(0)) for match in _ENTROPY_RUN.finditer(value))


# ---------------------------------------------------------------------------------------------
# Regex fallback detector
# ---------------------------------------------------------------------------------------------

_EMAIL: Final = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b"
)
_PHONE_E164: Final = re.compile(r"(?<![\d+])\+[1-9]\d{0,2}(?:[ -]?\(?\d{1,4}\)?){2,5}(?!\d)")
_PHONE_NANP: Final = re.compile(r"(?<![\d-])(?:\(\d{3}\)\s?|\d{3}[-. ])\d{3}[-. ]\d{4}(?![\d-])")
_SSN: Final = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")
_DIGIT_RUN: Final = re.compile(r"(?<![\d-])\d(?:[ -]?\d)+(?![\d-])")
_DIGITS: Final = re.compile(r"\d+")
_IBAN_RUN: Final = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]){0,40}")
"""An IBAN candidate: country code, check digits, then up to 40 characters each with an optional
space before it. One way to match each step, so the search is linear (spec 2.3 invariant 8)."""
_IBAN_ALNUM: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_IBAN_PREFIX_LEN: Final = 4
_IBAN_GROUP_LEN: Final = 4
_IBAN_MIN_GROUPS: Final = 2
_IBAN_MAX_GROUPS: Final = 8
_CARD_MIN: Final = 13
_CARD_MAX: Final = 19
_CARD_LEADING: Final = frozenset("13456")
_PHONE_MIN_DIGITS: Final = 8


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = ord(char) - 48
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _iban_valid(iban: str) -> bool:
    compact = iban.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(str(int(char, 36)) for char in rearranged)
    return int(numeric) % 97 == 1


def _iban_end(text: str, start: int, candidate: str) -> int | None:
    """The end of the longest IBAN-shaped prefix of ``candidate`` (found at ``start``): it ends
    where a space-separated chunk ends and no letter or digit follows, and its body after the
    first four characters fits in two to eight groups of one to four characters. ``None`` when
    no prefix qualifies. Same spans as the earlier pattern (two letters, two digits, two to
    eight groups of one to four characters each after an optional space, no letter or digit
    after the last), without its nested quantifiers and their backtracking."""
    best: int | None = None
    groups = 0
    body = 0
    offset = 0
    for index, chunk in enumerate(candidate.split(" ")):
        if index:
            offset += 1
        offset += len(chunk)
        part = len(chunk) - _IBAN_PREFIX_LEN if index == 0 else len(chunk)
        groups += -(-part // _IBAN_GROUP_LEN)
        body += part
        if groups > _IBAN_MAX_GROUPS:
            break
        end = start + offset
        if body >= _IBAN_MIN_GROUPS and (end >= len(text) or text[end] not in _IBAN_ALNUM):
            best = end
    return best


def _iban_hits(text: str) -> list[PiiHit]:
    """Mod-97-valid IBANs, printed in groups or compact. A linear scan: each candidate run is
    read once, and a start that leads nowhere moves the search on by one character, where the
    lookbehind rejects every position inside a run in constant time."""
    hits: list[PiiHit] = []
    position = 0
    while True:
        match = _IBAN_RUN.search(text, position)
        if match is None:
            return hits
        start = match.start()
        end = _iban_end(text, start, match.group(0))
        if end is None:
            position = start + 1
            continue
        if _iban_valid(text[start:end]):
            hits.append(PiiHit("IBAN_CODE", start, end, 1.0))
        position = end


def _card_hits(text: str) -> list[PiiHit]:
    """Luhn-valid groups of 13 to 19 digits with Presidio's leading-digit rule inside a run of
    digits separated by spaces or dashes; a run too long on its own is searched for a valid
    contiguous sub-run of groups."""
    hits: list[PiiHit] = []
    for run in _DIGIT_RUN.finditer(text):
        groups = [
            (m.start() + run.start(), m.end() + run.start()) for m in _DIGITS.finditer(run.group(0))
        ]
        for first in range(len(groups)):
            for last in range(first, len(groups)):
                span_start, span_end = groups[first][0], groups[last][1]
                digits = "".join(char for char in text[span_start:span_end] if char.isdigit())
                if len(digits) > _CARD_MAX:
                    break
                if (
                    len(digits) >= _CARD_MIN
                    and digits[0] in _CARD_LEADING
                    and len(set(digits)) > 1
                    and _luhn(digits)
                ):
                    hits.append(PiiHit("CREDIT_CARD", span_start, span_end, 1.0))
                    break
            else:
                continue
            break
    return hits


class RegexDetector:
    """Pattern-only fallback (ADR 0020): no names, no locations, no model, no network."""

    def detect(self, text: str) -> list[PiiHit]:
        text = truncate_text(text)
        hits: list[PiiHit] = [
            PiiHit("EMAIL_ADDRESS", m.start(), m.end(), 1.0) for m in _EMAIL.finditer(text)
        ]
        hits.extend(PiiHit("US_SSN", m.start(), m.end(), 0.85) for m in _SSN.finditer(text))
        hits.extend(
            PiiHit("PHONE_NUMBER", m.start(), m.end(), 0.7)
            for m in _PHONE_E164.finditer(text)
            if sum(char.isdigit() for char in m.group(0)) >= _PHONE_MIN_DIGITS
        )
        hits.extend(
            PiiHit("PHONE_NUMBER", m.start(), m.end(), 0.7) for m in _PHONE_NANP.finditer(text)
        )
        hits.extend(_card_hits(text))
        hits.extend(_iban_hits(text))
        return _normalize(hits, len(text))


# ---------------------------------------------------------------------------------------------
# Presidio
# ---------------------------------------------------------------------------------------------


def _offline_email_recognizer() -> Any:
    """The stock e-mail recognizer with a local domain check in place of ``tldextract``."""
    from presidio_analyzer.predefined_recognizers import EmailRecognizer  # noqa: PLC0415

    class OfflineEmailRecognizer(EmailRecognizer):
        """Validates the domain part syntactically: at least two non-empty labels and an
        alphabetic top-level label of two or more characters. No suffix list, no network."""

        def validate_result(self, pattern_text: str) -> bool:
            _local, at, domain = pattern_text.rpartition("@")
            if not at:
                return False
            labels = domain.split(".")
            return (
                len(labels) >= 2 and all(labels) and labels[-1].isalpha() and len(labels[-1]) >= 2
            )

    return OfflineEmailRecognizer


def _assert_offline(registry: Any) -> None:
    """Refuse a registry with a known network path (spec 2.3 invariant 4)."""
    from presidio_analyzer.predefined_recognizers import (  # noqa: PLC0415
        EmailRecognizer,
        UrlRecognizer,
    )

    for recognizer in registry.recognizers:
        if isinstance(recognizer, UrlRecognizer) or type(recognizer).__name__ == "UrlRecognizer":
            msg = "the URL recognizer is not allowed in the edge registry (ADR 0020)"
            raise PiiSetupError(msg)
        if (
            isinstance(recognizer, EmailRecognizer)
            and type(recognizer).validate_result is EmailRecognizer.validate_result
        ):
            msg = "stock EmailRecognizer uses tldextract (network); register the offline one"
            raise PiiSetupError(msg)


def build_registry(language: str) -> Any:
    """An explicit ``RecognizerRegistry`` with exactly the recognizers of ADR 0020."""
    from presidio_analyzer import RecognizerRegistry  # noqa: PLC0415
    from presidio_analyzer.predefined_recognizers import (  # noqa: PLC0415
        CreditCardRecognizer,
        IbanRecognizer,
        MedicalLicenseRecognizer,
        PhoneRecognizer,
        SpacyRecognizer,
        UsLicenseRecognizer,
        UsPassportRecognizer,
        UsSsnRecognizer,
    )

    registry = RecognizerRegistry(supported_languages=[language])
    recognizers = (
        _offline_email_recognizer()(supported_language=language),
        PhoneRecognizer(supported_language=language),
        UsSsnRecognizer(supported_language=language),
        CreditCardRecognizer(supported_language=language),
        IbanRecognizer(supported_language=language),
        UsPassportRecognizer(supported_language=language),
        UsLicenseRecognizer(supported_language=language),
        MedicalLicenseRecognizer(supported_language=language),
        SpacyRecognizer(supported_language=language, supported_entities=["PERSON", "LOCATION"]),
    )
    for recognizer in recognizers:
        registry.add_recognizer(recognizer)
    _assert_offline(registry)
    return registry


def _build_engine(settings: PiiSettings) -> tuple[Any, Any]:
    """The ``AnalyzerEngine`` and its registry. The spaCy model must be installed already:
    Presidio would otherwise try to download it."""
    import spacy.util  # noqa: PLC0415
    from presidio_analyzer import AnalyzerEngine  # noqa: PLC0415
    from presidio_analyzer.nlp_engine import NlpEngineProvider  # noqa: PLC0415

    model = settings.spacy_model
    if not spacy.util.is_package(model) and not Path(model).exists():
        msg = "the configured spaCy model is not installed; the edge never downloads models"
        raise PiiSetupError(msg)
    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": settings.language, "model_name": model}],
            "ner_model_configuration": {"labels_to_ignore": list(SPACY_LABELS_IGNORED)},
        }
    )
    nlp_engine = provider.create_engine()
    registry = build_registry(settings.language)
    engine = AnalyzerEngine(
        registry=registry,
        nlp_engine=nlp_engine,
        supported_languages=[settings.language],
        default_score_threshold=settings.score_threshold,
    )
    return engine, registry


class PresidioDetector:
    """Presidio over the explicit registry; loads lazily, once, thread-safely; degrades to
    :class:`RegexDetector` with one warning when the engine cannot be built."""

    def __init__(self, settings: PiiSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._engine: Any = None
        self._registry: Any = None
        self._fallback: RegexDetector | None = None

    @property
    def settings(self) -> PiiSettings:
        return self._settings

    @property
    def backend(self) -> Backend:
        if self._engine is not None:
            return "presidio"
        if self._fallback is not None:
            return "regex"
        return "unloaded"

    @property
    def registry(self) -> Any | None:
        """The ``RecognizerRegistry`` once loaded (tests inspect it); ``None`` before."""
        return self._registry

    def _loaded(self) -> bool:
        return self._engine is not None or self._fallback is not None

    def load(self) -> None:
        """Build the engine if not done yet. Never raises: a failure selects the fallback."""
        if self._loaded():
            return
        with self._lock:
            if self._loaded():
                return
            try:
                engine, registry = _build_engine(self._settings)
            except Exception as exc:  # any load failure degrades to regex, never crashes
                _logger().warning(
                    "pii_detector_fallback",
                    reason="model_load_failed",
                    error=type(exc).__name__,
                    spacy_model=self._settings.spacy_model,
                    language=self._settings.language,
                )
                self._fallback = RegexDetector()
                return
            self._engine = engine
            self._registry = registry

    def detect(self, text: str) -> list[PiiHit]:
        self.load()
        text = truncate_text(text)
        if self._fallback is not None:
            return self._fallback.detect(text)
        if not text.strip():
            return []
        results = self._engine.analyze(
            text=text,
            language=self._settings.language,
            entities=list(PRESIDIO_ENTITIES),
            score_threshold=self._settings.score_threshold,
        )
        hits = [
            PiiHit(str(result.entity_type), int(result.start), int(result.end), float(result.score))
            for result in results
        ]
        return _normalize(hits, len(text))


def detector_from_settings(settings: PiiSettings) -> PiiDetector:
    """The detector the edge runs: Presidio when enabled, else the regex fallback (warned)."""
    if not settings.enabled:
        _logger().warning("pii_detector_fallback", reason="pii_disabled")
        return RegexDetector()
    return PresidioDetector(settings)
