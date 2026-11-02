"""``carto-core gate-report`` (spec 21 M2 gate; plan M2 package I; founder decision 6).

The partner fills a CSV on the review call, independently of the engine's proposals, one row per
link they confirm by hand: ``system_a,field_a,system_b,field_b`` (system ids as the analyzer
configuration names them, field names as the partner knows them). :func:`gate_report` reads the
``engine_links.json`` that ``carto-core discover`` wrote and, for each confirmed row:

1. finds the engine ends of ``system_a`` whose path :func:`~carto_core.fieldpaths.matches_name`
   names ``field_a`` (case-insensitive; a leaf name matches a dotted path on a segment
   boundary), and the same for ``field_b``;
2. ranks every stored link touching an end of ``field_a`` (rejected links excluded) by
   ``score x affected_volume`` and keeps the top 3; does the same from ``field_b``'s side;
3. counts the link found when ``field_b`` is the partner end of one of ``field_a``'s top 3, or
   ``field_a`` of one of ``field_b``'s (either direction, spec 21: "the top 3 proposals for
   their fields").

The Markdown report lists the top 3 per confirmed link with type, score and volume, says which
confirmed fields the engine knows no end for, and ends with the summary line
``<n> of <m> confirmed links appear in the top 3 proposals (<pct>%); the gate asks for at least
half``. Both inputs are bounded and validated; a malformed file is a :class:`GateError` naming
the file and the place, never a value. Nothing in the report is a token: field names, system ids,
scores and counts only.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from carto_core.fieldpaths import matches_name

__all__ = [
    "CONFIRMED_HEADER",
    "ENGINE_LINKS_FILE",
    "MAX_CONFIRMED_BYTES",
    "MAX_CONFIRMED_ROWS",
    "MAX_LINKS_BYTES",
    "TOP_N",
    "ConfirmedLink",
    "EngineEnd",
    "EngineLink",
    "GateError",
    "GateResult",
    "LinkResult",
    "Proposal",
    "evaluate",
    "gate_report",
    "read_confirmed",
    "read_engine_links",
    "render",
]

ENGINE_LINKS_FILE: Final = "engine_links.json"
CONFIRMED_HEADER: Final[tuple[str, ...]] = ("system_a", "field_a", "system_b", "field_b")
MAX_CONFIRMED_BYTES: Final = 1024 * 1024
MAX_CONFIRMED_ROWS: Final = 10_000
MAX_LINKS_BYTES: Final = 256 * 1024 * 1024
MAX_FIELD_CHARS: Final = 256
TOP_N: Final = 3
GATE_SHARE: Final = 0.5
_EXCLUDED_STATUSES: Final = frozenset({"rejected"})


class GateError(Exception):
    """The report could not be built; the message names the file and the place."""


@dataclass(frozen=True, slots=True)
class EngineEnd:
    field_ref: str
    system_id: str
    template_id: str
    path: str
    form: str

    @property
    def label(self) -> str:
        return f"{self.system_id} {self.path} ({self.form})"


@dataclass(frozen=True, slots=True)
class EngineLink:
    link_id: str
    a: EngineEnd
    b: EngineEnd
    link_type: str
    role: str
    status: str
    score: float
    affected_volume: int

    @property
    def rank_key(self) -> float:
        return self.score * self.affected_volume

    def other(self, end: EngineEnd) -> EngineEnd:
        return self.b if end == self.a else self.a


@dataclass(frozen=True, slots=True)
class ConfirmedLink:
    row: int
    system_a: str
    field_a: str
    system_b: str
    field_b: str


@dataclass(frozen=True, slots=True)
class Proposal:
    """One of the top proposals for a confirmed field: the partner end and the link."""

    link: EngineLink
    partner: EngineEnd
    confirmed: bool


@dataclass(frozen=True, slots=True)
class LinkResult:
    confirmed: ConfirmedLink
    ends_a: tuple[EngineEnd, ...]
    ends_b: tuple[EngineEnd, ...]
    top_a: tuple[Proposal, ...]
    top_b: tuple[Proposal, ...]

    @property
    def found(self) -> bool:
        return any(p.confirmed for p in self.top_a) or any(p.confirmed for p in self.top_b)


@dataclass(frozen=True, slots=True)
class GateResult:
    results: tuple[LinkResult, ...]
    engine_links: int

    @property
    def found(self) -> int:
        return sum(1 for result in self.results if result.found)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def share(self) -> float | None:
        return self.found / self.total if self.total else None

    @property
    def passes(self) -> bool:
        return self.share is not None and self.share >= GATE_SHARE


# -- inputs -------------------------------------------------------------------------------------


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        msg = f"{path}: cannot read ({type(exc).__name__})"
        raise GateError(msg) from exc
    if len(data) > limit:
        msg = f"{path}: larger than {limit} bytes"
        raise GateError(msg)
    return data


def _end(item: Mapping[str, Any], path: Path, index: int, side: str) -> EngineEnd:
    raw = item.get(side)
    if not isinstance(raw, Mapping):
        msg = f"{path}: item {index}: {side} is not an object"
        raise GateError(msg)
    values: dict[str, str] = {}
    for key in ("field_ref", "system_id", "template_id", "path", "form"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            msg = f"{path}: item {index}: {side}.{key} is missing"
            raise GateError(msg)
        values[key] = value
    return EngineEnd(**values)


def _number(item: Mapping[str, Any], key: str, path: Path, index: int) -> float:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{path}: item {index}: {key} is not a number"
        raise GateError(msg)
    return float(value)


def read_engine_links(predictions: Path) -> list[EngineLink]:
    """``engine_links.json`` as :class:`EngineLink` rows (every status; the ranking excludes
    rejected ones). Unknown keys are ignored so a richer file still reads."""
    path = predictions / ENGINE_LINKS_FILE
    if not path.is_file():
        msg = f"{path}: not found (run carto-core discover --out {predictions} first)"
        raise GateError(msg)
    try:
        payload = json.loads(_read_bounded(path, MAX_LINKS_BYTES).decode("utf-8"))
    except UnicodeDecodeError as exc:
        msg = f"{path}: not UTF-8"
        raise GateError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"{path}: line {exc.lineno} column {exc.colno}: {exc.msg}"
        raise GateError(msg) from exc
    except RecursionError as exc:
        msg = f"{path}: JSON nested too deeply"
        raise GateError(msg) from exc
    if not isinstance(payload, list):
        msg = f"{path}: expected a JSON array of links"
        raise GateError(msg)
    links: list[EngineLink] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            msg = f"{path}: item {index}: not an object"
            raise GateError(msg)
        strings: dict[str, str] = {}
        for key in ("id", "link_type", "role", "status"):
            value = item.get(key)
            if not isinstance(value, str) or not value:
                msg = f"{path}: item {index}: {key} is missing"
                raise GateError(msg)
            strings[key] = value
        score = _number(item, "score", path, index)
        if not 0.0 <= score <= 1.0:
            msg = f"{path}: item {index}: score outside [0, 1]"
            raise GateError(msg)
        volume = _number(item, "affected_volume", path, index)
        if volume < 0:
            msg = f"{path}: item {index}: affected_volume is negative"
            raise GateError(msg)
        links.append(
            EngineLink(
                link_id=strings["id"],
                a=_end(item, path, index, "a"),
                b=_end(item, path, index, "b"),
                link_type=strings["link_type"],
                role=strings["role"],
                status=strings["status"],
                score=score,
                affected_volume=int(volume),
            )
        )
    return links


def read_confirmed(path: Path) -> list[ConfirmedLink]:
    """The partner's CSV: the exact header, four non-empty cells per row, bounded."""
    data = _read_bounded(path, MAX_CONFIRMED_BYTES)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        msg = f"{path}: not UTF-8"
        raise GateError(msg) from exc
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        msg = f"{path}: empty file; expected the header {','.join(CONFIRMED_HEADER)}"
        raise GateError(msg) from None
    except csv.Error as exc:
        msg = f"{path}: line 1: {exc}"
        raise GateError(msg) from exc
    if tuple(cell.strip().lower() for cell in header) != CONFIRMED_HEADER:
        msg = f"{path}: line 1: expected the header {','.join(CONFIRMED_HEADER)}"
        raise GateError(msg)
    rows: list[ConfirmedLink] = []
    try:
        for number, cells in enumerate(reader, start=2):
            if not cells or all(not cell.strip() for cell in cells):
                continue
            if len(cells) != len(CONFIRMED_HEADER):
                msg = f"{path}: line {number}: expected {len(CONFIRMED_HEADER)} cells"
                raise GateError(msg)
            values = [cell.strip() for cell in cells]
            if any(not value for value in values):
                msg = f"{path}: line {number}: empty cell"
                raise GateError(msg)
            if any(len(value) > MAX_FIELD_CHARS for value in values):
                msg = f"{path}: line {number}: cell longer than {MAX_FIELD_CHARS} characters"
                raise GateError(msg)
            rows.append(ConfirmedLink(number, *values))
            if len(rows) > MAX_CONFIRMED_ROWS:
                msg = f"{path}: more than {MAX_CONFIRMED_ROWS} rows"
                raise GateError(msg)
    except csv.Error as exc:
        msg = f"{path}: line {reader.line_num}: {exc}"
        raise GateError(msg) from exc
    if not rows:
        msg = f"{path}: no confirmed links below the header"
        raise GateError(msg)
    return rows


# -- evaluation ---------------------------------------------------------------------------------


def _ends_of(links: Sequence[EngineLink], system_id: str, name: str) -> tuple[EngineEnd, ...]:
    wanted = system_id.strip().lower()
    found: dict[str, EngineEnd] = {}
    for link in links:
        for end in (link.a, link.b):
            if end.system_id.lower() == wanted and matches_name(end.path, name):
                found.setdefault(f"{end.field_ref}|{end.form}", end)
    return tuple(found[key] for key in sorted(found))


def _top(
    links: Sequence[EngineLink],
    ends: Sequence[EngineEnd],
    partner_system: str,
    partner_name: str,
) -> tuple[Proposal, ...]:
    own = set(ends)
    seen: set[str] = set()
    ranked: list[tuple[float, str, EngineLink, EngineEnd]] = []
    for link in links:
        if link.status in _EXCLUDED_STATUSES or link.link_id in seen:
            continue
        touching = [end for end in (link.a, link.b) if end in own]
        if not touching:
            continue
        seen.add(link.link_id)
        partner = link.other(touching[0])
        ranked.append((-link.rank_key, link.link_id, link, partner))
    ranked.sort(key=lambda item: (item[0], item[1]))
    wanted_system = partner_system.strip().lower()
    return tuple(
        Proposal(
            link=link,
            partner=partner,
            confirmed=partner.system_id.lower() == wanted_system
            and matches_name(partner.path, partner_name),
        )
        for _key, _id, link, partner in ranked[:TOP_N]
    )


def evaluate(links: Sequence[EngineLink], confirmed: Sequence[ConfirmedLink]) -> GateResult:
    """Top 3 per confirmed field in both directions (module docstring)."""
    results: list[LinkResult] = []
    for row in confirmed:
        ends_a = _ends_of(links, row.system_a, row.field_a)
        ends_b = _ends_of(links, row.system_b, row.field_b)
        results.append(
            LinkResult(
                confirmed=row,
                ends_a=ends_a,
                ends_b=ends_b,
                top_a=_top(links, ends_a, row.system_b, row.field_b),
                top_b=_top(links, ends_b, row.system_a, row.field_a),
            )
        )
    return GateResult(results=tuple(results), engine_links=len(links))


# -- rendering ----------------------------------------------------------------------------------


def _proposal_rows(side: str, proposals: Sequence[Proposal]) -> list[str]:
    if not proposals:
        return [f"| {side} | (no proposal) | | | | |"]
    return [
        (
            f"| {side} | {index}. {proposal.partner.label} | {proposal.link.link_type} "
            f"({proposal.link.role}) | {proposal.link.score:.3f} | "
            f"{proposal.link.affected_volume:,} | {'yes' if proposal.confirmed else 'no'} |"
        )
        for index, proposal in enumerate(proposals, start=1)
    ]


def summary_line(result: GateResult) -> str:
    pct = 0.0 if result.share is None else result.share * 100
    return (
        f"{result.found} of {result.total} confirmed links appear in the top {TOP_N} proposals "
        f"({pct:.0f}%); the gate asks for at least half."
    )


def render(result: GateResult) -> str:
    """The Markdown report."""
    lines = [
        "# carto M2 gate: confirmed links against the engine's top proposals",
        "",
        (
            f"{result.engine_links:,} engine links read; {result.total} links confirmed by hand. "
            f"Proposals are ranked by score x affected volume; a confirmed link counts when its "
            f"other field is among the top {TOP_N} proposals of either field."
        ),
        "",
        f"**{summary_line(result)}** Outcome: {'pass' if result.passes else 'review'}.",
        "",
        "| # | Confirmed link | Found |",
        "| --- | --- | --- |",
    ]
    for index, item in enumerate(result.results, start=1):
        row = item.confirmed
        lines.append(
            f"| {index} | {row.system_a} {row.field_a} ~ {row.system_b} {row.field_b} | "
            f"{'yes' if item.found else 'no'} |"
        )
    lines.append("")
    for index, item in enumerate(result.results, start=1):
        row = item.confirmed
        lines.extend(
            [
                f"## {index}. {row.system_a} {row.field_a} ~ {row.system_b} {row.field_b}",
                "",
            ]
        )
        notes = []
        if not item.ends_a:
            notes.append(f"the engine knows no field named {row.field_a} in {row.system_a}")
        if not item.ends_b:
            notes.append(f"the engine knows no field named {row.field_b} in {row.system_b}")
        if notes:
            lines.extend([f"Note: {'; '.join(notes)}.", ""])
        lines.extend(
            [
                "| Side | Proposal (partner field) | Type | Score | Volume | Confirmed |",
                "| --- | --- | --- | --- | --- | --- |",
                *_proposal_rows(f"{row.system_a} {row.field_a}", item.top_a),
                *_proposal_rows(f"{row.system_b} {row.field_b}", item.top_b),
                "",
            ]
        )
    lines.append(summary_line(result))
    return "\n".join(lines) + "\n"


def gate_report(predictions: Path, confirmed: Path) -> str:
    """Read both inputs, evaluate, render (module docstring)."""
    links = read_engine_links(predictions)
    rows = read_confirmed(confirmed)
    return render(evaluate(links, rows))
