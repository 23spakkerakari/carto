"""What a Splunk source may run and hold: allowlists, not denylists (ADR 0037 items 4 and 5).

Spec 2.3 invariant 1 says no connector may write to a source system. A Splunk search can: the
``outputlookup``, ``collect``, ``delete``, ``sendemail`` and ``outputcsv`` commands write, and
``map``, ``rest``, ``script`` or a partner's custom command can do anything. Splunk adds
commands with every release, so the check names what is allowed:

- :func:`check_search` splits the search into pipeline stages on unquoted ``|``. Double quotes
  quote (a backslash escapes the next character, inside quotes and out; single quotes do not
  quote in a search, so they are ordinary characters here). Every unquoted ``[...]`` is a
  subsearch or a subpipeline and is checked the same way, recursively. Each stage's command
  must be in :data:`READ_ONLY_COMMANDS`: generating, filtering, eval, statistics, formatting
  and extraction commands that only read. The first stage of the top-level search is the
  implicit ``search`` unless the text starts with ``|`` (a generating command). Inside brackets
  the first word is taken as a command (``appendpipe`` and ``foreach`` run it as one), unless
  it is a ``key=value`` term or a quoted string. Macros (backquoted names) are refused: they
  expand to text the check cannot see. Anything the scanner cannot read with certainty (an
  unterminated quote, an unbalanced bracket, an empty stage) is refused.
- :func:`is_read_capability` accepts ``search``, ``rtsearch``, the ``list_*`` and ``get_*``
  families and a few named read capabilities (:data:`READ_CAPABILITIES`); every other
  capability makes the token write-capable.

A partner that needs another read-only command or capability gets it added here, in code, when
they ask (ADR 0037); there is deliberately no setting that widens either list. Error messages
name the refused command or capability, never the rest of the search.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from carto_edge.connectors.base import InvalidConfigError

__all__ = [
    "READ_CAPABILITIES",
    "READ_CAPABILITY_PREFIXES",
    "READ_ONLY_COMMANDS",
    "SearchCheckError",
    "check_search",
    "is_read_capability",
    "search_commands",
    "write_capabilities",
]

READ_ONLY_COMMANDS: Final = frozenset(
    {
        # generating (first stage after "|")
        "datamodel",
        "eventcount",
        "from",
        "inputlookup",
        "makeresults",
        "metadata",
        "mstats",
        "tstats",
        # searching and filtering
        "dedup",
        "head",
        "regex",
        "search",
        "tail",
        "uniq",
        "where",
        # eval and field shaping
        "convert",
        "eval",
        "fieldformat",
        "fields",
        "filldown",
        "fillnull",
        "foreach",
        "makemv",
        "mvcombine",
        "mvexpand",
        "nomv",
        "rangemap",
        "rename",
        "replace",
        "strcat",
        "table",
        # extraction
        "extract",
        "iplocation",
        "kv",
        "lookup",
        "multikv",
        "rex",
        "spath",
        "xmlkv",
        "xpath",
        # statistics and reporting
        "accum",
        "addcoltotals",
        "addtotals",
        "autoregress",
        "bin",
        "bucket",
        "chart",
        "cluster",
        "contingency",
        "delta",
        "eventstats",
        "fieldsummary",
        "geostats",
        "makecontinuous",
        "rare",
        "reverse",
        "sort",
        "stats",
        "streamstats",
        "timechart",
        "timewrap",
        "top",
        "transaction",
        "transpose",
        "trendline",
        "untable",
        "xyseries",
        # combining searches (their subsearches are checked too)
        "append",
        "appendcols",
        "appendpipe",
        "format",
        "join",
        "multisearch",
        "return",
        "union",
    }
)
"""Built-in commands that only read. Never on it: anything that writes (``outputlookup``,
``outputcsv``, ``outputtext``, ``collect``, ``mcollect``, ``meventcollect``, ``tscollect``,
``delete``), sends (``sendemail``, ``sendalert``), calls out (``rest``, ``dbxquery``), runs code
(``script``, ``run``) or spawns searches carto cannot see (``map``, ``savedsearch``)."""

READ_CAPABILITIES: Final = frozenset(
    {
        "export_results_is_visible",
        "pattern_detect",
        "rest_apps_view",
        "rest_properties_get",
        "rtsearch",
        "search",
    }
)
READ_CAPABILITY_PREFIXES: Final = ("list_", "get_")

MAX_DEPTH: Final = 8
_COMMAND: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class SearchCheckError(InvalidConfigError):
    """The Splunk search runs a command that is not on the read-only list, or cannot be read."""


def _skip_quoted(text: str, start: int) -> int:
    """Index just past the double-quoted string that opens at ``start``."""
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1
        index += 1
    msg = "the search has an unterminated quote"
    raise SearchCheckError(msg)


def _matching_bracket(text: str, start: int) -> int:
    """Index of the ``]`` that closes the ``[`` at ``start``."""
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            index = _skip_quoted(text, index)
            continue
        if char == "\\":
            index += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    msg = "the search has an unbalanced bracket"
    raise SearchCheckError(msg)


def _stages(text: str) -> list[tuple[str, list[str]]]:
    """Split on unquoted ``|``: each stage's text (subsearches blanked) and its subsearches."""
    stages: list[tuple[str, list[str]]] = []
    current: list[str] = []
    subsearches: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            end = _skip_quoted(text, index)
            current.append(text[index:end])
            index = end
        elif char == "\\":
            current.append(text[index : index + 2])
            index += 2
        elif char == "`":
            msg = (
                "macros (backquoted names) are not allowed: they expand to text carto cannot check"
            )
            raise SearchCheckError(msg)
        elif char == "[":
            end = _matching_bracket(text, index)
            subsearches.append(text[index + 1 : end])
            current.append(" [] ")
            index = end + 1
        elif char == "]":
            msg = "the search has an unbalanced bracket"
            raise SearchCheckError(msg)
        elif char == "|":
            stages.append(("".join(current), subsearches))
            current, subsearches = [], []
            index += 1
        else:
            current.append(char)
            index += 1
    stages.append(("".join(current), subsearches))
    return stages


def _command(stage: str) -> str:
    match = _COMMAND.match(stage.lstrip())
    if match is None:
        msg = "a pipeline stage does not start with a command name"
        raise SearchCheckError(msg)
    return match.group(0).lower()


def _implicit_search(stage: str) -> bool:
    """Inside brackets: a first stage that starts with a term, not a command name."""
    first = stage.split(maxsplit=1)[0] if stage.split() else ""
    return "=" in first or first.startswith(('"', "(", "[", "-", "*"))


def _walk(text: str, *, nested: bool, depth: int, found: list[str]) -> None:
    if depth > MAX_DEPTH:
        msg = f"the search nests subsearches deeper than {MAX_DEPTH}"
        raise SearchCheckError(msg)
    stages = _stages(text)
    generating = text.lstrip().startswith("|")
    if generating:
        stages = stages[1:]  # the empty text before the leading "|"
    for position, (stage, subsearches) in enumerate(stages):
        if not stage.strip():
            msg = "the search has an empty pipeline stage"
            raise SearchCheckError(msg)
        if position == 0 and not generating and (not nested or _implicit_search(stage)):
            name = "search"
        else:
            name = _command(stage)
        if name not in READ_ONLY_COMMANDS:
            msg = (
                f"search command {name!r} is not on carto's read-only command list (ADR 0037); "
                "ask for a read-only command to be added"
            )
            raise SearchCheckError(msg)
        found.append(name)
        for subsearch in subsearches:
            _walk(subsearch, nested=True, depth=depth + 1, found=found)


def search_commands(text: str) -> list[str]:
    """The commands ``text`` runs, in order, subsearches included; raises
    :class:`SearchCheckError` when any is not read-only or the text cannot be read."""
    found: list[str] = []
    _walk(text, nested=False, depth=0, found=found)
    return found


def check_search(text: str) -> None:
    """Refuse a search that could run anything but a read-only built-in command."""
    if "\x00" in text:
        msg = "the search must not contain NUL"
        raise SearchCheckError(msg)
    search_commands(text)


def is_read_capability(capability: str) -> bool:
    name = capability.strip().lower()
    return name in READ_CAPABILITIES or name.startswith(READ_CAPABILITY_PREFIXES)


def write_capabilities(capabilities: Iterable[str]) -> list[str]:
    """Every capability that is not on the read list, sorted (ADR 0037 item 5)."""
    return sorted({item for item in capabilities if not is_read_capability(item)})
