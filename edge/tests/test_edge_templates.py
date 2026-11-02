"""Drain3 template store: mining, parameters, ids, persistence, registry (spec 8.2 item 6,
ADR 0016)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_edge.pipeline.pii import MAX_TEXT_BYTES, PiiHit, truncate_text
from carto_edge.pipeline.templates import (
    MASK,
    MAX_DISTINCT_DIGESTS,
    MIN_CLUSTER_SIZE,
    STATE_VERSION,
    TemplateStore,
    compute_template_id,
    generalize_name,
    pii_constant_filter,
)
from carto_schema.event import MAX_TEMPLATE_TEXT_LEN, TEMPLATE_ID_PATTERN, EventKind

SIM_DIR = Path(__file__).resolve().parents[2] / "sim-out" / "shop"
needs_sim = pytest.mark.skipif(
    not SIM_DIR.is_dir(), reason="sim-out/shop missing: run make sim SCENARIO=shop"
)
EXPORT_LINES = [
    "PO export finished: 412 POs written to SHIP_20260923_2112.csv",
    "PO export finished: 7 POs written to SHIP_20260924_2115.csv",
    "SFTP upload complete: SHIP_20260923_2112.csv (23891 bytes)",
    "SFTP upload failed: Permission denied (/outbound/shipping/SHIP_20261001_2113.csv)",
    "export scheduler idle, next run 21:10",
]
T0 = datetime(2026, 9, 23, 21, 12, 41, tzinfo=UTC)


def test_templates_export_log_templates_and_params_from_the_first_sighting() -> None:
    store = TemplateStore(min_cluster_size=1)
    text, params = store.mine("sys_warehouse", EXPORT_LINES[0])
    assert text == "PO export finished: <*> POs written to <*>"
    assert params == ["412", "SHIP_20260923_2112.csv"]
    text, params = store.mine("sys_warehouse", EXPORT_LINES[1])
    assert text == "PO export finished: <*> POs written to <*>"
    assert params == ["7", "SHIP_20260924_2115.csv"]
    text, params = store.mine("sys_warehouse", EXPORT_LINES[3])
    assert text == "SFTP upload failed: Permission denied <*>"
    assert params == ["(/outbound/shipping/SHIP_20261001_2113.csv)"]
    text, params = store.mine("sys_warehouse", EXPORT_LINES[4])
    assert text == "export scheduler idle, next run <*>"
    assert params == ["21:10"]


def test_templates_constant_messages_have_no_params() -> None:
    store = TemplateStore(min_cluster_size=1)
    assert store.mine("sys_webstore", "cart created") == ("cart created", [])
    assert store.mine("sys_webstore", "checkout completed") == ("checkout completed", [])
    assert store.mine("sys_orders", "order created from cart") == ("order created from cart", [])


def test_templates_variable_words_become_params_after_a_second_sighting() -> None:
    store = TemplateStore(min_cluster_size=1)
    first, _ = store.mine("sys_x", "user alice logged in")
    assert first == "user alice logged in"
    second, params = store.mine("sys_x", "user bob logged in")
    assert second == f"user {MASK} logged in"
    assert params == ["bob"]
    again, params = store.mine("sys_x", "user alice logged in")
    assert again == second
    assert params == ["alice"]


def test_templates_emails_and_digit_tokens_are_masked_up_front() -> None:
    store = TemplateStore(min_cluster_size=1)
    text, params = store.mine("sys_x", "mail sent to jane.smith@example.com id 77")
    assert text == f"mail sent to {MASK} id {MASK}"
    assert params == ["jane.smith@example.com", "77"]


def test_templates_are_per_system() -> None:
    store = TemplateStore(min_cluster_size=1)
    store.mine("sys_a", "user alice logged in")
    store.mine("sys_a", "user bob logged in")
    text, _ = store.mine("sys_b", "user carol logged in")
    assert text == "user carol logged in"


def test_templates_whitespace_is_normalised_and_long_messages_are_bounded() -> None:
    store = TemplateStore(min_cluster_size=1)
    assert store.mine("sys_x", "  a   b\t c ")[0] == "a b c"
    text, params = store.mine("sys_x", " ".join(str(i) for i in range(5000)))
    assert len(text) <= MAX_TEMPLATE_TEXT_LEN
    assert len(params) <= 512
    assert store.mine("sys_x", "")[0] == ""


def test_templates_id_is_sha256_of_system_and_text() -> None:
    expected = "tpl_" + hashlib.sha256(b"sys_warehouse\0cart created").hexdigest()[:12]
    assert compute_template_id("sys_warehouse", "cart created") == expected
    assert (
        TemplateStore(min_cluster_size=1).template_id("sys_warehouse", "cart created") == expected
    )
    assert TEMPLATE_ID_PATTERN.match(expected)
    assert compute_template_id("sys_a", "x") != compute_template_id("sys_b", "x")


def test_templates_registry_counts_and_seen_range() -> None:
    store = TemplateStore(min_cluster_size=1)
    tid = store.template_id("sys_x", "cart created")
    store.register("sys_x", tid, "cart created", EventKind.LOG, T0)
    later = T0.replace(hour=22)
    store.register("sys_x", tid, "cart created", EventKind.LOG, later)
    earlier = T0.replace(hour=20)
    store.register("sys_x", tid, "cart created", EventKind.LOG, earlier)
    other = store.template_id("sys_y", "row_change purchase_orders")
    store.register("sys_y", other, "row_change purchase_orders", EventKind.ROW_CHANGE, T0)
    records = store.registry()
    assert [(r.system_id, r.template_id) for r in records] == sorted(
        (r.system_id, r.template_id) for r in records
    )
    by_id = {r.template_id: r for r in records}
    assert by_id[tid].count == 3
    assert by_id[tid].first_seen == earlier
    assert by_id[tid].last_seen == later
    assert by_id[tid].kind == EventKind.LOG
    assert by_id[tid].system_id == "sys_x"
    assert by_id[tid].template_text == "cart created"
    assert by_id[other].kind == EventKind.ROW_CHANGE
    assert by_id[other].count == 1


def test_templates_persist_and_reload_with_the_same_ids(tmp_path: Path) -> None:
    with TemplateStore(tmp_path, min_cluster_size=1) as store:
        ids_before = {}
        for line in EXPORT_LINES:
            text, _ = store.mine("sys_warehouse", line)
            ids_before[line] = store.template_id("sys_warehouse", text)
        store.mine("sys_x", "user alice logged in")
        store.mine("sys_x", "user bob logged in")
        tid = store.template_id("sys_x", f"user {MASK} logged in")
        store.register("sys_x", tid, f"user {MASK} logged in", EventKind.LOG, T0)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["sys_warehouse.json", "sys_x.json"]
    assert not list(tmp_path.glob("*.tmp"))
    reloaded = TemplateStore(tmp_path, min_cluster_size=1)
    for line in EXPORT_LINES:
        text, _ = reloaded.mine("sys_warehouse", line)
        assert reloaded.template_id("sys_warehouse", text) == ids_before[line]
    text, params = reloaded.mine("sys_x", "user dave logged in")
    assert text == f"user {MASK} logged in"
    assert params == ["dave"]
    assert reloaded.registry()[0].count == 1
    reloaded.close()


def test_templates_persistence_file_is_plain_json_without_pickle(tmp_path: Path) -> None:
    with TemplateStore(tmp_path, min_cluster_size=1) as store:
        store.mine("sys_x", "cart created 1")
    raw = (tmp_path / "sys_x.json").read_bytes()
    state = json.loads(raw)
    assert state["version"] == STATE_VERSION
    assert "py/object" not in raw.decode("utf-8")
    assert state["clusters"][0]["tokens"] == ["cart", "created", MASK]


def test_templates_corrupt_state_file_is_ignored_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "sys_x.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "sys_y.json").write_text(json.dumps({"version": 99}), encoding="utf-8")
    (tmp_path / "sys_z.json").write_text(
        json.dumps({"version": 1, "clusters": "nope", "tree": [], "counter": -1, "registry": 3}),
        encoding="utf-8",
    )
    store = TemplateStore(tmp_path, min_cluster_size=1)
    assert store.mine("sys_x", "a 1")[0] == f"a {MASK}"
    assert store.mine("sys_y", "a 1")[0] == f"a {MASK}"
    assert store.mine("sys_z", "a 1")[0] == f"a {MASK}"
    store.flush()
    assert (
        json.loads((tmp_path / "sys_x.json").read_text(encoding="utf-8"))["version"]
        == STATE_VERSION
    )


def test_templates_oversized_state_file_is_ignored(tmp_path: Path) -> None:
    big = tmp_path / "sys_x.json"
    with big.open("wb") as handle:
        handle.seek(70 * 1024 * 1024)
        handle.write(b"\0")
    store = TemplateStore(tmp_path, min_cluster_size=1)
    assert store.mine("sys_x", "a")[0] == "a"


def test_templates_flush_is_periodic_and_only_when_dirty(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path, flush_interval_seconds=0.0, min_cluster_size=1)
    store.mine("sys_x", "a")
    first = (tmp_path / "sys_x.json").stat().st_mtime_ns
    store.flush()
    assert (tmp_path / "sys_x.json").stat().st_mtime_ns == first
    store.close()


def test_templates_system_id_is_validated_before_it_names_a_file(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path, min_cluster_size=1)
    with pytest.raises(ValueError, match="system_id"):
        store.mine("../escape", "a")
    with pytest.raises(ValueError, match="system_id"):
        store.mine("Sys", "a")


def test_templates_memory_is_bounded_by_max_clusters() -> None:
    letters = "abcdefghijklmnopqrstuvwxyz"

    def word(n: int) -> str:
        return f"{letters[n % 26]}{letters[(n // 26) % 26]}x"

    store = TemplateStore(max_clusters=50, min_cluster_size=1)
    for i in range(500):
        store.mine("sys_x", f"{word(i)} {word(i + 7)}q {word(i + 3)}z {word(i + 11)}w")
    assert store.cluster_count("sys_x") <= 50


def test_generalize_name_digit_runs_become_stars() -> None:
    assert generalize_name("SHIP_20260923_2112.csv") == "SHIP_*_*.csv"
    assert generalize_name("claims-2026-10-06.x12") == "claims-*-*-*.x*"
    assert generalize_name("report.pdf") == "report.pdf"
    assert generalize_name("") == ""
    assert generalize_name("123") == "*"
    assert generalize_name("a1b22c333") == "a*b*c*"


def test_templates_file_arrived_template_text_pattern() -> None:
    assert (
        f"file_arrived {generalize_name('SHIP_20260923_2112.csv')}" == "file_arrived SHIP_*_*.csv"
    )


def test_templates_no_stray_output_on_construction() -> None:
    code = (
        "from carto_edge.pipeline.templates import TemplateStore\n"
        "s = TemplateStore(min_cluster_size=1)\n"
        "s.mine('sys_x', 'hello 1')\n"
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter, fixed code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout == ""
    assert "drain3.ini" not in result.stderr


@needs_sim
def test_templates_simulator_export_log_has_few_stable_templates() -> None:
    store = TemplateStore(min_cluster_size=1)
    lines = []
    for path in sorted((SIM_DIR / "warehouse").glob("export-job-*.log")):
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    texts = set()
    for line in lines:
        message = line.split(" ", 3)[3]
        texts.add(store.mine("sys_warehouse", message)[0])
    assert texts == {
        "PO export finished: <*> POs written to <*>",
        "SFTP upload complete: <*> <*> bytes)",
        "SFTP upload failed: Permission denied <*>",
        "export scheduler idle, next run <*>",
    }


@settings(max_examples=150, deadline=3000)
@given(st.text(max_size=500))
def test_templates_property_mine_never_raises_and_params_align(text: str) -> None:
    store = TemplateStore(min_cluster_size=1)
    template, params = store.mine("sys_x", text)
    assert len(template) <= MAX_TEMPLATE_TEXT_LEN
    assert all(isinstance(p, str) for p in params)
    assert template.count(MASK) >= len(params) or len(template) == MAX_TEMPLATE_TEXT_LEN


def test_words_seen_in_fewer_than_three_distinct_messages_are_never_constants() -> None:
    """Drain3 keeps a one-member cluster verbatim; a word seen once (a name, a code word, free
    text) must not become a template constant (spec 2.3 invariant 2). Identical repeats count
    once (ADR 0068): a retry loop logging the same line does not make its words constants."""
    store = TemplateStore()
    assert MIN_CLUSTER_SIZE == 3
    message = "delivery note leave behind the blue gate MKPINE"
    first, params = store.mine("sys_web", message)
    assert first == "<*> <*> <*> <*> <*> <*> <*> <*>"
    assert params == ["delivery", "note", "leave", "behind", "the", "blue", "gate", "MKPINE"]
    for _ in range(6):
        repeated, repeated_params = store.mine("sys_web", message)
        assert repeated == first  # one distinct message, however often: all parameters
        assert repeated_params == params
    # Whitespace variants are the same message.
    assert store.mine("sys_web", "  delivery note   leave behind the blue gate MKPINE ")[0] == first


def test_a_repeated_pii_line_stays_all_parameters() -> None:
    """The review's case: ``Refund approved for Alice Smith by manager`` three times verbatim."""
    store = TemplateStore()
    message = "Refund approved for Alice Smith by manager"
    for _ in range(3):
        template, params = store.mine("sys_web", message)
        assert template == "<*> <*> <*> <*> <*> <*> <*>"
        assert params == ["Refund", "approved", "for", "Alice", "Smith", "by", "manager"]


def test_three_distinct_messages_sharing_constants_mine_the_template() -> None:
    store = TemplateStore()
    lines = [
        "Refund approved for MKALPHA by manager",
        "Refund approved for MKBRAVO by manager",
        "Refund approved for MKBRAVO by manager",  # a repeat: still two distinct
    ]
    for line in lines:
        assert store.mine("sys_web", line)[0] == "<*> <*> <*> <*> <*> <*>"
    template, params = store.mine("sys_web", "Refund approved for MKCHARLIE by manager")
    assert template == "Refund approved for <*> by manager"
    assert params == ["MKCHARLIE"]
    # Earlier members of the cluster get the template too, once it has it.
    assert store.mine("sys_web", lines[0]) == ("Refund approved for <*> by manager", ["MKALPHA"])


ALICE_LINES = [f"Refund approved for Alice Smith attempt {n}" for n in range(1, 4)]
ALL_PARAMS_7 = " ".join([MASK] * 7)


def _flags_alice(constants: Sequence[str]) -> bool:
    """A constant filter standing in for the detector: a PERSON hit on the name."""
    return "Alice" not in constants


def test_the_constant_filter_keeps_a_name_out_of_the_constants(tmp_path: Path) -> None:
    """The review's second case: three retries that differ only in a counter are three distinct
    messages whose shared words, the name among them, would become constants. The constant
    filter (the detector, in the runtime) refuses them and the cluster stays all parameters for
    good; the verdict is persisted with the cluster (ADR 0068)."""
    with TemplateStore(tmp_path, constant_filter=_flags_alice) as store:
        for line in ALICE_LINES:
            template, params = store.mine("sys_web", line)
            assert template == ALL_PARAMS_7
            assert params == line.split()
        # Later variants never promote it either, and the name is only ever a parameter.
        template, params = store.mine("sys_web", "Refund approved for Alice Smith attempt 44")
        assert template == ALL_PARAMS_7
        assert "Alice" in params
        # Ordinary lines, constants plus masked parameters, mine their template as before.
        for n, size in enumerate((23891, 118, 7730), start=1):
            template, params = store.mine(
                "sys_web", f"SFTP upload complete: SHIP_2026092{n}_2112.csv ({size} bytes)"
            )
        assert template == "SFTP upload complete: <*> <*> bytes)"
        assert params == ["SHIP_20260923_2112.csv", "(7730"]
        for user in ("alice", "bob", "carol"):
            template, params = store.mine("sys_web", f"session opened user={user} host:edge")
        assert template == f"session opened user={MASK} host:{MASK}"
        assert params == ["carol", "edge"]
    # A hit is persisted with the cluster (the Drain3 state is edge-local, as before); an
    # allowed verdict is not: the cluster is judged again once per process (ADR 0068).
    state = json.loads((tmp_path / "sys_web.json").read_text(encoding="utf-8"))
    verdicts = {" ".join(c["tokens"]): c.get("p") for c in state["clusters"]}
    assert verdicts["Refund approved for Alice Smith attempt <*>"] is True
    assert verdicts["SFTP upload complete: <*> <*> bytes)"] is None
    assert "false" not in (tmp_path / "sys_web.json").read_text(encoding="utf-8")
    # The verdict survives a restart, even one whose filter would now allow the words.
    reloaded = TemplateStore(tmp_path, constant_filter=lambda _constants: True)
    assert reloaded.mine("sys_web", ALICE_LINES[0])[0] == ALL_PARAMS_7
    reloaded.freeze()
    assert reloaded.mine("sys_web", ALICE_LINES[1])[0] == ALL_PARAMS_7
    assert reloaded.mine("sys_web", "SFTP upload complete: SHIP_x.csv (9 bytes)")[0] == (
        "SFTP upload complete: <*> <*> bytes)"
    )


def test_the_constant_filter_runs_once_per_cluster_and_fails_closed() -> None:
    seen: list[list[str]] = []

    def record(constants: Sequence[str]) -> bool:
        seen.append(list(constants))
        return True

    store = TemplateStore(constant_filter=record)
    for n in range(1, 6):
        store.mine("sys_web", f"Refund approved for Alice Smith attempt {n}")
    assert seen == [["Refund", "approved", "for", "Alice", "Smith", "attempt"]]
    for user in ("alice", "bob", "carol"):
        store.mine("sys_web", f"session opened user={user} host:edge")
    assert seen[1] == ["session", "opened", "user=", "host:"]
    assert len(seen) == 2

    def broken(_constants: Sequence[str]) -> bool:
        msg = "detector unavailable"
        raise RuntimeError(msg)

    failing = TemplateStore(constant_filter=broken)
    for line in ALICE_LINES:
        assert failing.mine("sys_web", line)[0] == ALL_PARAMS_7
    assert failing.mine("sys_web", "Refund approved for Alice Smith attempt 9")[0] == ALL_PARAMS_7


class _FakeDetector:
    """Reports a PERSON on ``Alice`` at the score it was built with."""

    def __init__(self, score: float, threshold: float | None = None) -> None:
        self.score = score
        self.texts: list[str] = []
        if threshold is not None:
            self.settings = type("Settings", (), {"score_threshold": threshold})()

    def detect(self, text: str) -> list[PiiHit]:
        self.texts.append(text)
        start = text.find("Alice")
        return [] if start < 0 else [PiiHit("PERSON", start, start + 5, self.score)]


def test_pii_constant_filter_asks_the_detector_at_its_threshold() -> None:
    strong = _FakeDetector(0.85)
    assert pii_constant_filter(strong)(["Refund", "approved", "for", "Alice"]) is False
    assert strong.texts == ["Refund approved for Alice"]
    assert pii_constant_filter(strong)(["Refund", "approved"]) is True
    weak = _FakeDetector(0.4)  # below the 0.5 default: no hit
    assert pii_constant_filter(weak)(["Alice"]) is True
    assert pii_constant_filter(_FakeDetector(0.4, threshold=0.3))(["Alice"]) is False
    store = TemplateStore(constant_filter=pii_constant_filter(strong))
    for line in ALICE_LINES:
        assert store.mine("sys_web", line)[0] == ALL_PARAMS_7


def test_distinct_message_digests_are_persisted_and_bounded(tmp_path: Path) -> None:
    with TemplateStore(tmp_path) as store:
        store.mine("sys_web", "voucher MKALPHA redeemed by staff")
        store.mine("sys_web", "voucher MKBRAVO redeemed by staff")
        store.mine("sys_web", "voucher MKBRAVO redeemed by staff")
    raw = (tmp_path / "sys_web.json").read_text(encoding="utf-8")
    state = json.loads(raw)
    assert state["version"] == STATE_VERSION == 3
    (cluster,) = state["clusters"]
    assert len(cluster["d"]) == 2  # two distinct messages, one repeat
    assert all(len(digest) == 12 for digest in cluster["d"])
    assert "MKALPHA" not in raw and "MKBRAVO" not in raw  # digests, never the messages
    reloaded = TemplateStore(tmp_path)
    # The third distinct message completes the count across the restart.
    assert reloaded.mine("sys_web", "voucher MKCHARLIE redeemed by staff") == (
        "voucher <*> redeemed by staff",
        ["MKCHARLIE"],
    )
    for n in range(20):
        reloaded.mine("sys_web", f"voucher MK{'X' * (n + 1)} redeemed by staff")
    reloaded.flush()
    state = json.loads((tmp_path / "sys_web.json").read_text(encoding="utf-8"))
    assert len(state["clusters"][0]["d"]) == MAX_DISTINCT_DIGESTS


def test_version_one_state_loads_with_constants_to_be_earned_again(tmp_path: Path) -> None:
    """A version 1 file carries cluster sizes but no digests: its clusters start with no
    distinct message, so a cluster of identical repeats cannot carry its words over."""
    text = "voucher <*> redeemed by staff"
    with TemplateStore(tmp_path) as store:
        for name in ("MKALPHA", "MKBRAVO", "MKCHARLIE"):
            store.mine("sys_web", f"voucher {name} redeemed by staff")
        store.register("sys_web", store.template_id("sys_web", text), text, EventKind.LOG, T0)
    path = tmp_path / "sys_web.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["version"] = 1
    for cluster in state["clusters"]:
        del cluster["d"]
    path.write_text(json.dumps(state), encoding="utf-8")
    reloaded = TemplateStore(tmp_path)
    assert reloaded.mine("sys_web", "voucher MKALPHA redeemed by staff")[0] == "<*> <*> <*> <*> <*>"
    assert reloaded.registry()[0].count == 1  # the file was readable: the registry survived
    for name in ("MKBRAVO", "MKDELTA"):
        reloaded.mine("sys_web", f"voucher {name} redeemed by staff")
    assert reloaded.mine("sys_web", "voucher MKECHO redeemed by staff")[0] == (
        "voucher <*> redeemed by staff"
    )
    reloaded.flush()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == STATE_VERSION


def test_version_two_state_loads_with_its_digests_and_no_verdicts(tmp_path: Path) -> None:
    """A version 2 file holds the same digests as version 3 and no constant-filter verdicts:
    the digests are kept and a promoted cluster is judged at its next use (ADR 0068)."""
    with TemplateStore(tmp_path) as store:
        for line in ALICE_LINES:
            store.mine("sys_web", line)
        for name in ("MKALPHA", "MKBRAVO"):
            store.mine("sys_web", f"voucher {name} redeemed by staff")
    path = tmp_path / "sys_web.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    # Allowed without a filter and not yet promoted: neither cluster carries a verdict.
    assert [c.get("p") for c in state["clusters"]] == [None, None]
    state["version"] = 2
    for cluster in state["clusters"]:
        cluster.pop("p", None)
    path.write_text(json.dumps(state), encoding="utf-8")
    reloaded = TemplateStore(tmp_path, constant_filter=_flags_alice)
    assert reloaded.mine("sys_web", ALICE_LINES[0])[0] == ALL_PARAMS_7  # judged again: a hit
    assert reloaded.mine("sys_web", "voucher MKCHARLIE redeemed by staff")[0] == (
        "voucher <*> redeemed by staff"  # the two digests were kept: the third promotes
    )
    reloaded.flush()
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["version"] == STATE_VERSION
    assert [c.get("p") for c in state["clusters"]] == [True, None]


def test_an_allowed_verdict_is_judged_again_after_a_restart(tmp_path: Path) -> None:
    """The degraded-then-repaired case (review of 2026-11-02): the detector that judged the
    cluster knew no names (the regex fallback of a failed model load, or ``pii.enabled=false``)
    and allowed them; the model is repaired and the process restarted. The allowed verdict was
    never persisted, so the repaired detector judges the cluster again at its next use and the
    name goes back to being a parameter. A persisted ``"p": false`` from an older state file
    reads as not judged either."""
    with TemplateStore(tmp_path, constant_filter=lambda _constants: True) as store:
        for line in ALICE_LINES:
            store.mine("sys_web", line)
        assert store.mine("sys_web", "Refund approved for Alice Smith attempt 44") == (
            "Refund approved for Alice Smith attempt <*>",
            ["44"],
        )
    path = tmp_path / "sys_web.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    assert [c.get("p") for c in state["clusters"]] == [None]
    reloaded = TemplateStore(tmp_path, constant_filter=_flags_alice)
    assert reloaded.mine("sys_web", "Refund approved for Alice Smith attempt 45") == (
        ALL_PARAMS_7,
        ["Refund", "approved", "for", "Alice", "Smith", "attempt", "45"],
    )
    reloaded.flush()
    assert [c.get("p") for c in json.loads(path.read_text(encoding="utf-8"))["clusters"]] == [True]
    state["clusters"][0]["p"] = False
    path.write_text(json.dumps(state), encoding="utf-8")
    judged: list[list[str]] = []

    def record(constants: Sequence[str]) -> bool:
        judged.append(list(constants))
        return "Alice" not in constants

    again = TemplateStore(tmp_path, constant_filter=record)
    assert again.mine("sys_web", "Refund approved for Alice Smith attempt 46")[0] == ALL_PARAMS_7
    assert judged == [["Refund", "approved", "for", "Alice", "Smith", "attempt"]]
    assert hashlib.blake2b(b"x", digest_size=6).hexdigest() not in str(state)  # sanity


def test_an_invalid_verdict_makes_the_state_file_unreadable(tmp_path: Path) -> None:
    with TemplateStore(tmp_path) as store:
        store.mine("sys_web", "voucher MKALPHA redeemed by staff")
    path = tmp_path / "sys_web.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["clusters"][0]["p"] = "yes"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert TemplateStore(tmp_path).cluster_count("sys_web") == 0


def test_invalid_digests_make_the_state_file_unreadable(tmp_path: Path) -> None:
    with TemplateStore(tmp_path) as store:
        store.mine("sys_web", "voucher MKALPHA redeemed by staff")
    path = tmp_path / "sys_web.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["clusters"][0]["d"] = ["x" * 12] * (MAX_DISTINCT_DIGESTS + 1)
    path.write_text(json.dumps(state), encoding="utf-8")
    reloaded = TemplateStore(tmp_path)
    assert reloaded.cluster_count("sys_web") == 0  # mining started fresh


@pytest.mark.parametrize(
    ("message", "secret_words"),
    [
        (
            (
                "key loaded -----BEGIN RSA PRIVATE KEY----- MIIEvQIBADANBgkqhkiG9w0BAQEFAASC "
                "-----END RSA PRIVATE KEY----- ok"
            ),
            ["BEGIN", "PRIVATE", "KEY", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"],
        ),
        (
            "key loaded -----BEGIN PRIVATE KEY----- MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
            ["BEGIN", "PRIVATE", "KEY", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"],
        ),
        (
            "db reconnect postgres://svc_orders:Hunter2@db.internal/orders done",
            ["Hunter2", "svc_orders"],
        ),
        (
            "db reconnect dsn=postgres://svc_orders:Hunter2@db.internal/orders done",
            ["Hunter2", "svc_orders"],
        ),
        ("fetch https://svc:Hunter2@splunk.internal/api failed", ["Hunter2"]),
    ],
)
def test_pem_blocks_and_url_userinfo_are_secrets_never_constants_nor_params(
    message: str, secret_words: list[str]
) -> None:
    """ADR 0068: PEM armour and a URL with userinfo are masked before mining and are never
    parameters, so neither the key nor the password reaches a field, however often the line
    repeats and however many variants arrive."""
    store = TemplateStore()
    for n in range(4):
        template, params = store.mine("sys_web", f"{message} attempt{'x' * n}")
        text = template + " " + " ".join(params)
        for word in secret_words:
            assert word not in text, (word, template, params)


def test_a_varying_word_stays_a_parameter_once_constants_appear() -> None:
    store = TemplateStore()
    for name in ("MKALPHA", "MKBRAVO"):
        template, _ = store.mine("sys_web", f"voucher {name} redeemed by staff")
        assert template == "<*> <*> <*> <*> <*>"
    template, params = store.mine("sys_web", "voucher MKCHARLIE redeemed by staff")
    assert template == "voucher <*> redeemed by staff"
    assert params == ["MKCHARLIE"]


def test_a_frozen_store_never_grows_a_cluster() -> None:
    """The analyzer mines its input twice (ADR 0017); pass 2 must not count a message again, or
    a message seen twice reaches MIN_CLUSTER_SIZE and travels with its words as constants."""
    store = TemplateStore()
    message = "parcel left with neighbour MKTWICE"
    for _ in range(2):
        assert store.mine("sys_web", message)[0] == "<*> <*> <*> <*> <*>"
    store.freeze()
    assert store.frozen
    for _ in range(3):
        template, params = store.mine("sys_web", message)
        assert template == "<*> <*> <*> <*> <*>"
        assert params == ["parcel", "left", "with", "neighbour", "MKTWICE"]


def test_a_frozen_store_uses_the_templates_pass_one_learned() -> None:
    store = TemplateStore()
    for name in ("MKALPHA", "MKBRAVO", "MKCHARLIE"):
        store.mine("sys_web", f"voucher {name} redeemed by staff")
    store.freeze()
    # The first member of the cluster now gets the final template, not the all-parameter one.
    assert store.mine("sys_web", "voucher MKALPHA redeemed by staff") == (
        "voucher <*> redeemed by staff",
        ["MKALPHA"],
    )
    assert store.mine("sys_web", "an unseen message shape") == (
        "<*> <*> <*> <*>",
        [
            "an",
            "unseen",
            "message",
            "shape",
        ],
    )
    assert store.cluster_count("sys_web") == 1


class _TruncatingDetector(_FakeDetector):
    """The fake with the real detectors' input cap: ``MAX_TEXT_BYTES`` of UTF-8 per call."""

    backend = "regex"

    def detect(self, text: str) -> list[PiiHit]:
        return super().detect(truncate_text(text))


def test_pii_constant_filter_judges_the_constants_in_chunks_the_detector_can_see() -> None:
    """A template's constants may run to 4096 characters while a detector sees 4096 UTF-8
    bytes: a name past the byte cap must still be judged (review of 2026-11-02)."""
    detector = _TruncatingDetector(0.85)
    constants = ["ü" * 16] * 128 + ["Alice"]  # 2181 characters, 4229 bytes joined
    assert len(" ".join(constants)) <= MAX_TEMPLATE_TEXT_LEN
    assert len(" ".join(constants).encode("utf-8")) > MAX_TEXT_BYTES
    assert pii_constant_filter(detector)(constants) is False
    assert len(detector.texts) >= 2
    assert all(len(text.encode("utf-8")) <= MAX_TEXT_BYTES for text in detector.texts)
    assert "Alice" in detector.texts[-1]
    # One constant longer than the cap is cut into pieces the detector can see whole.
    detector = _TruncatingDetector(0.85)
    assert pii_constant_filter(detector)(["x" * (MAX_TEXT_BYTES + 100) + "Alice"]) is False
    assert all(len(text.encode("utf-8")) <= MAX_TEXT_BYTES for text in detector.texts)
    # Constants that fit go in one call, as before.
    detector = _TruncatingDetector(0.85)
    assert pii_constant_filter(detector)(["Refund", "approved"]) is True
    assert detector.texts == ["Refund approved"]


def test_pii_constant_filter_names_the_detector_backend_in_the_judging_log_line() -> None:
    detector = _TruncatingDetector(0.85)
    constant_filter = pii_constant_filter(detector)
    assert constant_filter.backend == "regex"
    store = TemplateStore(constant_filter=constant_filter)
    with structlog.testing.capture_logs() as logs:
        for line in ALICE_LINES:
            store.mine("sys_web", line)
        for name in ("MKALPHA", "MKBRAVO", "MKCHARLIE"):
            store.mine("sys_web", f"voucher {name} redeemed by staff")
    judged = [entry for entry in logs if entry["event"] == "template_constants_judged"]
    assert [(entry["hit"], entry["backend"]) for entry in judged] == [
        (True, "regex"),
        (False, "regex"),
    ]
    assert all("Alice" not in json.dumps(entry) for entry in judged)
    assert all(isinstance(entry["cluster_id"], int) for entry in judged)


def _counting_miner(store: TemplateStore, system_id: str) -> list[str]:
    """Wrap the system's miner so every tree walk is recorded; returns the record."""
    calls: list[str] = []
    state = store._states[system_id]
    original = state.miner.add_log_message

    def counted(message: str) -> dict[str, object]:
        calls.append(message)
        result: dict[str, object] = original(message)
        return result

    state.miner.add_log_message = counted
    return calls


def test_a_blocked_cluster_is_cached_like_a_promoted_one() -> None:
    store = TemplateStore(constant_filter=_flags_alice)
    for line in ALICE_LINES:
        store.mine("sys_web", line)
    calls = _counting_miner(store, "sys_web")
    for n in (44, 45, 44):
        assert store.mine("sys_web", f"Refund approved for Alice Smith attempt {n}")[0] == (
            ALL_PARAMS_7
        )
    assert len(calls) <= 1  # the first variant may walk the tree; the cache serves the rest
    state = store._states["sys_web"]
    assert state.cache["Refund approved for Alice Smith attempt <*>"] == ALL_PARAMS_7


def test_an_identical_repeat_of_a_sub_threshold_message_does_not_walk_the_tree() -> None:
    store = TemplateStore()
    message = "health check ok for MKPINE"
    assert store.mine("sys_web", message)[0] == "<*> <*> <*> <*> <*>"
    calls = _counting_miner(store, "sys_web")
    for _ in range(3):
        assert store.mine("sys_web", message) == (
            "<*> <*> <*> <*> <*>",
            ["health", "check", "ok", "for", "MKPINE"],
        )
    assert calls == []
    # A distinct variant is not a repeat: it walks the tree and counts toward promotion.
    assert store.mine("sys_web", "health check ok for MKOAK")[0] == "<*> <*> <*> <*> <*>"
    assert len(calls) == 1
    template, params = store.mine("sys_web", "health check ok for MKELM")
    assert template == "health check ok for <*>"
    assert params == ["MKELM"]
    # Once promoted, the earlier repeat gets the template too (the repeat cache is gone).
    assert store.mine("sys_web", message) == ("health check ok for <*>", ["MKPINE"])
