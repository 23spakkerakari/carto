"""carto_edge.connectors.splunk_spl: the Splunk search and capability allowlists (ADR 0037 items
4 and 5, spec 2.3 invariant 1). Every pipeline stage, subsearches included, must run a built-in
read-only command; every capability of the token's roles must be on the read list."""

from __future__ import annotations

import pytest

from carto_edge.connectors.base import InvalidConfigError
from carto_edge.connectors.splunk_spl import (
    READ_ONLY_COMMANDS,
    SearchCheckError,
    check_search,
    is_read_capability,
    search_commands,
    write_capabilities,
)


@pytest.mark.parametrize(
    "search",
    [
        "search index=orders sourcetype=order_svc",
        "index=orders ERROR",
        'index=orders msg="a | outputlookup x" | stats count by host',
        "index=orders | eval x=if(a>1, 1, 0) | where x=1 | table _time x",
        'index=orders | rex field=_raw "id=(?<id>\\d+)" | fields id',
        "| tstats count where index=orders by _time span=1h",
        "| inputlookup carriers.csv | fields code",
        "index=orders [search index=refunds | fields order_id] | head 10",
        "index=orders | join type=left order_id [| inputlookup carriers.csv]",
        "index=orders | append [search index=refunds] | dedup order_id | sort - _time",
        "index=orders | foreach * [eval <<FIELD>>=trim('<<FIELD>>')]",
        "index=orders | lookup carriers code OUTPUT name | timechart span=5m count",
        'index=orders "quoted \\" still quoted | collect index=x"',
        "index=orders\n| STATS count",
        "index=orders | bin _time span=1m | streamstats count | eventstats max(count)",
    ],
)
def test_read_only_searches_are_accepted(search: str) -> None:
    check_search(search)


@pytest.mark.parametrize(
    ("search", "refused"),
    [
        ("index=orders | outputlookup carriers.csv", "outputlookup"),
        ("index=orders | collect index=main", "collect"),
        ("index=orders | delete", "delete"),
        ("index=orders | sendemail to=a@example.com", "sendemail"),
        ("index=orders | outputcsv /tmp/x", "outputcsv"),
        ("| rest /services/server/info", "rest"),
        ('index=orders | map search="search index=x | delete"', "map"),
        ("index=orders | script python evil", "script"),
        ("index=orders | sendalert foo", "sendalert"),
        ('| dbxquery query="DELETE FROM t"', "dbxquery"),
        ("index=orders | mycustomcommand", "mycustomcommand"),
        ("index=orders [search index=x | outputlookup y]", "outputlookup"),
        ("index=orders | appendpipe [outputlookup y]", "outputlookup"),
        ("index=orders [| rest /services/apps/local]", "rest"),
        ("index=orders | `evil_macro`", "macro"),
        ("index=orders | savedsearch nightly", "savedsearch"),
        ('index=orders "unterminated | delete', "quote"),
        ("index=orders [search index=x", "bracket"),
        ("index=orders ] | delete", "bracket"),
        ("index=orders | | stats count", "empty"),
        ("index=orders |", "empty"),
        ('index=orders \\" | delete | \\"', "delete"),
        ("index=orders' | delete | '", "delete"),
    ],
)
def test_write_and_unknown_commands_are_refused(search: str, refused: str) -> None:
    with pytest.raises(SearchCheckError, match=refused):
        check_search(search)


def test_search_check_error_is_an_invalid_config_error() -> None:
    assert issubclass(SearchCheckError, InvalidConfigError)


def test_commands_are_listed_in_order_with_the_implicit_search() -> None:
    assert search_commands("index=orders | stats count | where count > 1") == [
        "search",
        "stats",
        "where",
    ]
    assert search_commands("| inputlookup x.csv [search index=y | fields a]") == [
        "inputlookup",
        "search",
        "fields",
    ]


def test_the_allowlist_has_no_write_command() -> None:
    writers = {
        "outputlookup",
        "collect",
        "mcollect",
        "tscollect",
        "meventcollect",
        "delete",
        "sendemail",
        "sendalert",
        "outputcsv",
        "outputtext",
        "rest",
        "map",
        "script",
        "run",
        "dbxquery",
        "savedsearch",
    }
    assert not writers & READ_ONLY_COMMANDS


@pytest.mark.parametrize(
    "capability",
    [
        "search",
        "rtsearch",
        "list_inputs",
        "list_storage_passwords",
        "get_metadata",
        "get_typeahead",
        "rest_properties_get",
    ],
)
def test_read_capabilities(capability: str) -> None:
    assert is_read_capability(capability)


@pytest.mark.parametrize(
    "capability",
    [
        "edit_user",
        "delete_by_keyword",
        "admin_all_objects",
        "change_own_password",
        "restart_splunkd",
        "output_file",
        "schedule_search",
        "rest_properties_set",
        "input_file",
        "embed_report",
        "accelerate_search",
        "some_future_capability",
    ],
)
def test_everything_else_is_a_write_capability(capability: str) -> None:
    assert not is_read_capability(capability)
    assert write_capabilities(["search", capability]) == [capability]
