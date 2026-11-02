"""``carto-core gate-report`` (spec 21 M2 gate; plan M2 package I) on a hand-written
prediction directory: found and missed rows, both directions, case-insensitive suffix names,
the summary line, and the input checks (header, cells, size, malformed JSON)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from carto_core import cli
from carto_core.fieldpaths import matches_name, name_candidates
from carto_core.gate import (
    CONFIRMED_HEADER,
    MAX_CONFIRMED_BYTES,
    GateError,
    evaluate,
    gate_report,
    read_confirmed,
    read_engine_links,
)


def end(system: str, path: str, form: str = "raw", template: str = "tpl_a") -> dict[str, str]:
    return {
        "field_ref": f"{system}/{template}/{path}",
        "system_id": system,
        "template_id": template,
        "path": path,
        "form": form,
    }


def link(
    link_id: str,
    a: dict[str, str],
    b: dict[str, str],
    *,
    score: float,
    volume: int,
    link_type: str = "exact",
    role: str = "transaction",
    status: str = "proposed",
) -> dict[str, Any]:
    return {
        "id": link_id,
        "status": status,
        "link_type": link_type,
        "role": role,
        "direction": "a_to_b",
        "score": score,
        "a": a,
        "b": b,
        "affected_volume": volume,
        "evidence": {"n_match": volume},
        "extra": {"sentence": "x"},
    }


ORDERS_ID = end("sys_orders", "order_id")
WMS_REF = end("sys_warehouse", "order_ref", "digits.0")
PAY_REF = end("sys_payments", "merchantRef")
ORD_MREF = end("sys_orders", "merchant_ref")
WEB_CART = end("sys_webstore", "cart.cart_id")
ORD_CART = end("sys_orders", "cart_id")
SHIP_PO = end("sys_shipping", "po_num")
WMS_PO = end("sys_warehouse", "po_num")
NOISE = [end("sys_noise", f"n{i}") for i in range(5)]


@pytest.fixture
def predictions(tmp_path: Path) -> Path:
    links = [
        # order_id's partners by score x volume: three noise links beat the confirmed one.
        link("l1", ORDERS_ID, NOISE[0], score=0.9, volume=1000),
        link("l2", ORDERS_ID, NOISE[1], score=0.9, volume=900),
        link("l3", ORDERS_ID, NOISE[2], score=0.9, volume=800),
        link("l4", ORDERS_ID, WMS_REF, score=0.97, volume=500),
        # but from order_ref's side the confirmed link is the top proposal (either direction).
        link("l5", WMS_REF, NOISE[3], score=0.5, volume=10),
        # merchant_ref ~ merchantRef is the top proposal straight away.
        link("l6", ORD_MREF, PAY_REF, score=0.99, volume=600),
        link("l7", ORD_MREF, NOISE[4], score=0.99, volume=10),
        # cart_id lives under a dotted path on the webstore side.
        link("l8", WEB_CART, ORD_CART, score=0.98, volume=600),
        # a rejected link never counts.
        link("l9", SHIP_PO, WMS_PO, score=0.99, volume=700, status="rejected"),
    ]
    directory = tmp_path / "pred"
    directory.mkdir()
    (directory / "engine_links.json").write_text(json.dumps(links), encoding="utf-8")
    return directory


def write_csv(path: Path, rows: list[str], header: str = ",".join(CONFIRMED_HEADER)) -> Path:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def test_found_and_missed_rows_and_the_summary_line(predictions: Path, tmp_path: Path) -> None:
    confirmed = write_csv(
        tmp_path / "links.csv",
        [
            "sys_orders,order_id,sys_warehouse,order_ref",  # found from order_ref's side
            "sys_orders,merchant_ref,sys_payments,MERCHANTREF",  # found, case-insensitive
            "sys_webstore,cart_id,sys_orders,cart_id",  # found through the dotted path suffix
            "sys_shipping,po_num,sys_warehouse,po_num",  # only a rejected link: missed
            "sys_orders,order_id,sys_payments,merchantRef",  # no such link: missed
            "sys_unknown,thing,sys_orders,order_id",  # unknown field: missed, noted
        ],
    )
    result = evaluate(read_engine_links(predictions), read_confirmed(confirmed))
    assert [item.found for item in result.results] == [True, True, True, False, False, False]
    first = result.results[0]
    assert [p.link.link_id for p in first.top_a] == ["l1", "l2", "l3"]
    assert [p.link.link_id for p in first.top_b] == ["l4", "l5"]
    assert first.top_b[0].confirmed
    assert result.found == 3 and result.total == 6 and result.passes
    text = gate_report(predictions, confirmed)
    assert (
        "3 of 6 confirmed links appear in the top 3 proposals (50%); the gate asks for at "
        "least half." in text
    )
    assert "Outcome: pass" in text
    assert "the engine knows no field named thing in sys_unknown" in text
    assert "| sys_orders order_id | 1. sys_noise n0 (raw) | exact (transaction) | 0.900 |" in text
    assert "rejected" not in text.split("## 4.")[1].split("## 5.")[0]


def test_below_half_reads_review(predictions: Path, tmp_path: Path) -> None:
    confirmed = write_csv(
        tmp_path / "links.csv",
        ["sys_shipping,po_num,sys_warehouse,po_num", "sys_orders,order_id,sys_payments,x"],
    )
    text = gate_report(predictions, confirmed)
    assert "0 of 2 confirmed links appear in the top 3 proposals (0%)" in text
    assert "Outcome: review" in text


def test_cli_writes_the_report(
    predictions: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    confirmed = write_csv(
        tmp_path / "links.csv", ["sys_orders,merchant_ref,sys_payments,merchantRef"]
    )
    out = tmp_path / "gate.md"
    code = cli.main(
        [
            "gate-report",
            "--predictions",
            str(predictions),
            "--confirmed",
            str(confirmed),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert "1 of 1 confirmed links" in out.read_text(encoding="utf-8")
    code = cli.main(
        ["gate-report", "--predictions", str(predictions), "--confirmed", str(confirmed)]
    )
    assert code == 0
    assert "1 of 1 confirmed links" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("header", "rows", "message"),
    [
        ("system_a,field_a,system_b", ["a,b,c"], "expected the header"),
        (",".join(CONFIRMED_HEADER), ["a,b,c"], "expected 4 cells"),
        (",".join(CONFIRMED_HEADER), ["a,,c,d"], "empty cell"),
        (",".join(CONFIRMED_HEADER), [], "no confirmed links"),
        (",".join(CONFIRMED_HEADER), ["a,b,c," + "x" * 300], "longer than 256"),
    ],
)
def test_malformed_csv_is_a_gate_error(
    predictions: Path, tmp_path: Path, header: str, rows: list[str], message: str
) -> None:
    confirmed = write_csv(tmp_path / "links.csv", rows, header=header)
    with pytest.raises(GateError, match=message):
        gate_report(predictions, confirmed)
    assert (
        cli.main(["gate-report", "--predictions", str(predictions), "--confirmed", str(confirmed)])
        == 1
    )


def test_oversized_csv_and_blank_lines(predictions: Path, tmp_path: Path) -> None:
    big = tmp_path / "big.csv"
    big.write_bytes(
        b"system_a,field_a,system_b,field_b\n" + b"a,b,c,d\n" * (MAX_CONFIRMED_BYTES // 8)
    )
    with pytest.raises(GateError, match="larger than"):
        read_confirmed(big)
    blanks = write_csv(
        tmp_path / "blank.csv", ["", "sys_orders,order_id,sys_warehouse,order_ref", "  "]
    )
    assert len(read_confirmed(blanks)) == 1


def test_malformed_engine_links(tmp_path: Path) -> None:
    directory = tmp_path / "pred"
    directory.mkdir()
    confirmed = write_csv(tmp_path / "links.csv", ["a,b,c,d"])
    with pytest.raises(GateError, match="not found"):
        gate_report(directory, confirmed)
    target = directory / "engine_links.json"
    target.write_text("{", encoding="utf-8")
    with pytest.raises(GateError, match="line 1"):
        gate_report(directory, confirmed)
    target.write_text(json.dumps({"links": []}), encoding="utf-8")
    with pytest.raises(GateError, match="expected a JSON array"):
        gate_report(directory, confirmed)
    bad = link("l1", ORDERS_ID, WMS_REF, score=1.5, volume=1)
    target.write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(GateError, match="item 0: score"):
        gate_report(directory, confirmed)
    bad = link("l1", ORDERS_ID, {"system_id": "x"}, score=0.5, volume=1)
    target.write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(GateError, match=r"b\.field_ref is missing"):
        gate_report(directory, confirmed)


def test_path_rules_shared_with_the_mapping() -> None:
    assert name_candidates("order.cart_id") == ("order.cart_id", "cart_id")
    assert name_candidates("stock.0.key") == ("stock.0.key", "key", "stock")
    assert name_candidates("stock.*") == ("stock",)
    assert name_candidates("*~1") == ()
    assert name_candidates("msg.param_0") == ("msg.param_0", "param_0")
    assert matches_name("order.cart_id", "CART_ID")
    assert matches_name("order.cart_id", "order.cart_id")
    assert not matches_name("order.cart_id", "art_id")
    assert not matches_name("*", "*")
    assert matches_name("merchantRef", "merchantref")
