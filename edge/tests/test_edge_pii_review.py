"""Review fixes in carto_edge.pipeline.pii (M1 review, package A2): the regex fallback's IBAN
search runs in linear time (spec 2.3 invariant 8: no ReDoS) and finds what the old pattern
found."""

from __future__ import annotations

import re
import time

from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.pipeline.pii import (
    MAX_TEXT_BYTES,
    RegexDetector,
    _iban_hits,
    _iban_valid,
    detect_many,
    truncate_text,
)

# The pattern the detector used before the fix: nested quantifiers that backtrack on long
# runs. Kept here only as the reference the new scan must agree with on short inputs.
_OLD_IBAN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{1,4}){2,8}(?![A-Z0-9])")


def _old_iban_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _OLD_IBAN.finditer(text) if _iban_valid(m.group(0))]


def _iban_spans(text: str) -> list[tuple[int, int]]:
    return [(hit.start, hit.end) for hit in _iban_hits(text)]


def _detected(text: str) -> list[tuple[int, int]]:
    hits = RegexDetector().detect(text)
    return [(hit.start, hit.end) for hit in hits if hit.entity_type == "IBAN_CODE"]


def _best_of_three(text: str) -> float:
    detector = RegexDetector()
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        detector.detect(text)
        timings.append(time.perf_counter() - start)
    return min(timings)


def test_iban_search_does_not_backtrack_on_long_alphanumeric_runs() -> None:
    # The reviewer's input: 4 KB of "AB12" followed by 36 letters took about 290 ms a call.
    text = (("AB12" + "C" * 36 + " ") * 100)[:MAX_TEXT_BYTES]
    assert _best_of_three(text) < 0.05


def test_iban_search_is_fast_on_spaced_single_characters() -> None:
    text = ("AB12" + " A" * 2000)[:MAX_TEXT_BYTES]
    assert _best_of_three(text) < 0.05


def test_valid_ibans_are_still_found_printed_and_compact() -> None:
    printed = "pay to DE89 3704 0044 0532 0130 00 today"
    compact = "iban GB82WEST12345698765432."
    assert _detected(printed) == [(7, 34)]
    assert _detected(compact) == [(5, 27)]
    assert _detected("DE89 3704 0044 0532 0130 01") == []  # checksum fails


@settings(max_examples=400, deadline=None)
@given(st.text(alphabet="AB12DE89GW0 x", max_size=48))
def test_iban_scan_agrees_with_the_old_pattern(text: str) -> None:
    assert _iban_spans(text) == _old_iban_spans(text)


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.sampled_from(["DE89", "3704", "0044", "0532", "0130", "00", "GB82", "WEST", "X", " "]),
        max_size=14,
    )
)
def test_iban_scan_agrees_on_valid_iban_pieces(pieces: list[str]) -> None:
    text = "".join(pieces)
    assert _iban_spans(text) == _old_iban_spans(text)


def test_detector_inputs_with_a_lone_surrogate_never_raise() -> None:
    text = "mail a@example.com " + chr(0xD800) + " done"
    assert chr(0xD800) not in truncate_text(text)
    assert truncate_text("plain") == "plain"
    hits = detect_many(RegexDetector(), [text, "x"])
    assert [hit.entity_type for hit in hits[0]] == ["EMAIL_ADDRESS"]
