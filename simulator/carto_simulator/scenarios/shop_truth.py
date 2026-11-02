"""Ground truth assembly for scenario A: links, entities, batches, faults, hops, markers, README.

Everything here is a pure function of what :mod:`carto_simulator.scenarios.shop` generated; the
models are the contract in :mod:`carto_simulator.ground_truth` (ADR 0006). Link ids L01 to L10
and fault ids f1 to f6 are stable names the eval harness can rely on.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date, datetime, tzinfo
from pathlib import Path

from carto_simulator import clock
from carto_simulator.api import GENERATOR_VERSION, GenerationRequest
from carto_simulator.ground_truth import (
    EVENTS_FILE,
    GROUND_TRUTH_DIR,
    BatchTruth,
    CompositeComponent,
    EntityField,
    EntityTruth,
    FaultKind,
    FaultTruth,
    FieldRef,
    GroundTruth,
    LinkTruth,
    Manifest,
    ManualHopTruth,
    MarkerSet,
)
from carto_simulator.names import CLERKS
from carto_simulator.scenarios.shop_model import (
    CLERK_SHARE,
    CLERK_TYPO_RATE,
    ENT_MANIFEST,
    ENT_ORDER,
    ENT_SHIPPING_FILE,
    N_AUTHORIZED,
    N_CARRIER_PICKUP,
    N_CART,
    N_CHECKOUT,
    N_FILE_ARRIVED,
    N_ORDER_CREATED,
    N_ORDER_RELEASED,
    N_PAYMENT_REQUESTED,
    N_PO_CREATED,
    N_SHIPMENT_CREATED,
    SOURCES,
    SRC_ORDERS,
    SRC_PAYMENTS,
    SRC_SHIP_LOG,
    SRC_SHIP_SFTP,
    SRC_WEBSTORE,
    SRC_WMS_DB,
    STALLED_WAREHOUSE,
    SYS_ORDERS,
    SYS_PAYMENTS,
    SYS_SHIPPING,
    SYS_WAREHOUSE,
    SYS_WEBSTORE,
    WAREHOUSE_SKEW_SECONDS,
    ExportedFile,
    FaultState,
    PurchaseOrder,
    Transaction,
)
from carto_simulator.writers import Emitted


def _ref(system_id: str, source_id: str, field_name: str) -> FieldRef:
    return FieldRef(system_id=system_id, source_id=source_id, field=field_name)


def _link(
    link_id: str,
    a: FieldRef,
    form_a: str,
    b: FieldRef,
    form_b: str,
    link_type: str,
    role: str,
    direction: str,
    entity_id: str,
    notes: str = "",
) -> LinkTruth:
    return LinkTruth.model_validate(
        {
            "link_id": link_id,
            "a": a,
            "form_a": form_a,
            "b": b,
            "form_b": form_b,
            "link_type": link_type,
            "role": role,
            "direction": direction,
            "entity_id": entity_id,
            "notes": notes,
        }
    )


def composite_agreement(
    transactions: Iterable[Transaction], zone: tzinfo
) -> tuple[float, float, float]:
    """Agreement rates of the L06 components (amount, date, phonetic name) over clerk rows."""
    clerk_rows = [(txn, txn.po) for txn in transactions if txn.po is not None and txn.po.human]
    if not clerk_rows:
        return (1.0, 1.0, 0.0)
    amount = sum(txn.total == po.total for txn, po in clerk_rows)
    dates = sum(
        clock.to_local(txn.attempts[-1].responded_at, zone).date() == po.order_date
        for txn, po in clerk_rows
    )
    names = sum(txn.cardholder_pii for txn, _ in clerk_rows)
    count = len(clerk_rows)
    return (amount / count, dates / count, names / count)


def build_links(rename_active: bool, rates: tuple[float, float, float]) -> list[LinkTruth]:
    """L01 to L10 (spec 9.3 exact, 9.4 bridges, 9.5 composite, 9.7 batch keys)."""
    web_cart = _ref(SYS_WEBSTORE, SRC_WEBSTORE, "cart_id")
    ord_cart = _ref(SYS_ORDERS, SRC_ORDERS, "cart_id")
    ord_id = _ref(SYS_ORDERS, SRC_ORDERS, "order_id")
    ord_mref = _ref(SYS_ORDERS, SRC_ORDERS, "merchant_ref")
    pay_mref = _ref(SYS_PAYMENTS, SRC_PAYMENTS, "merchantRef")
    pay_amount = _ref(SYS_PAYMENTS, SRC_PAYMENTS, "amount")
    pay_ts = _ref(SYS_PAYMENTS, SRC_PAYMENTS, "timestamp")
    pay_name = _ref(SYS_PAYMENTS, SRC_PAYMENTS, "cardholderName")
    wh_order_ref = _ref(SYS_WAREHOUSE, SRC_WMS_DB, "order_ref")
    wh_total = _ref(SYS_WAREHOUSE, SRC_WMS_DB, "order_total")
    wh_date = _ref(SYS_WAREHOUSE, SRC_WMS_DB, "order_date")
    wh_name = _ref(SYS_WAREHOUSE, SRC_WMS_DB, "customer_name")
    wh_po = _ref(SYS_WAREHOUSE, SRC_WMS_DB, "po_num")
    wh_po_renamed = _ref(SYS_WAREHOUSE, SRC_WMS_DB, "po_number")
    ship_po = _ref(SYS_SHIPPING, SRC_SHIP_LOG, "po_num")
    ship_no = _ref(SYS_SHIPPING, SRC_SHIP_LOG, "shipment_no")
    ship_file = _ref(SYS_SHIPPING, SRC_SHIP_LOG, "file")
    ship_manifest = _ref(SYS_SHIPPING, SRC_SHIP_LOG, "manifest_id")
    sftp_name = _ref(SYS_SHIPPING, SRC_SHIP_SFTP, "file_name")
    composite = LinkTruth(
        link_id="L06",
        a=pay_amount,
        form_a="amount",
        b=wh_total,
        form_b="amount",
        link_type="composite",
        role="transaction",
        direction="a_to_b",
        entity_id=ENT_ORDER,
        manual=True,
        components=[
            CompositeComponent(
                a=pay_amount, form_a="amount", b=wh_total, form_b="amount", agreement_rate=rates[0]
            ),
            CompositeComponent(
                a=pay_ts, form_a="date", b=wh_date, form_b="date", agreement_rate=rates[1]
            ),
            CompositeComponent(
                a=pay_name,
                form_a="phonetic.0",
                b=wh_name,
                form_b="phonetic.0",
                agreement_rate=rates[2],
            ),
        ],
        notes=(
            f"{CLERK_SHARE:.0%} of purchase orders are keyed by clerks from the payment "
            f"confirmation during business hours, with {CLERK_TYPO_RATE:.0%} typos in order_ref "
            "that break L05 for those rows; amount + date + phonetic name still agree (spec 9.5)."
        ),
    )
    links = [
        _link("L01", web_cart, "raw", ord_cart, "raw", "exact", "transaction", "a_to_b", ENT_ORDER),
        _link(
            "L02",
            ord_cart,
            "raw",
            ord_id,
            "raw",
            "bridge",
            "transaction",
            "undirected",
            ENT_ORDER,
            "Bridge inside 'order created from cart' lines (spec 9.4).",
        ),
        _link(
            "L03",
            ord_id,
            "raw",
            ord_mref,
            "raw",
            "bridge",
            "transaction",
            "undirected",
            ENT_ORDER,
            "Bridge inside 'payment requested' lines (spec 9.4).",
        ),
        _link("L04", ord_mref, "raw", pay_mref, "raw", "exact", "transaction", "a_to_b", ENT_ORDER),
        _link(
            "L05",
            ord_id,
            "raw",
            wh_order_ref,
            "digits.0",
            "exact",
            "transaction",
            "a_to_b",
            ENT_ORDER,
            "SO-0004471: digits run 0004471, leading zeros stripped, 4471 (spec 8.4).",
        ),
        composite,
        _link("L07", wh_po, "raw", ship_po, "raw", "exact", "transaction", "a_to_b", ENT_ORDER),
    ]
    if rename_active:
        links.append(
            _link(
                "L07b",
                wh_po_renamed,
                "raw",
                ship_po,
                "raw",
                "exact",
                "transaction",
                "a_to_b",
                ENT_ORDER,
                "Rows created after the po_num -> po_number rename (fault F4).",
            )
        )
    links.extend(
        [
            _link(
                "L08",
                ship_no,
                "raw",
                ship_po,
                "raw",
                "bridge",
                "transaction",
                "undirected",
                ENT_ORDER,
                "Bridge inside 'shipment created' lines.",
            ),
            _link(
                "L09",
                sftp_name,
                "raw",
                ship_file,
                "raw",
                "batch",
                "batch",
                "a_to_b",
                ENT_SHIPPING_FILE,
                "The nightly SHIP file is a batch key (spec 9.7), never a transaction key.",
            ),
            _link(
                "L10",
                ship_no,
                "raw",
                ship_manifest,
                "raw",
                "bridge",
                "batch",
                "undirected",
                ENT_MANIFEST,
                "Bridge inside 'carrier pickup' lines; manifest_id is a batch key.",
            ),
        ]
    )
    # Bridges the data holds because several identifiers sit in one record (spec 9.4): the
    # warehouse row id with its own columns, two columns of one row, and the SHIP file name a
    # shipment line travelled in (M2 eval follow-up: the engine found them; the truth lists them).
    wh_id = _ref(SYS_WAREHOUSE, SRC_WMS_DB, "id")
    row_note = "Bridge inside purchase_orders rows: the primary key and one of its columns."
    links.extend(
        [
            _link(
                "L11",
                wh_id,
                "raw",
                wh_po,
                "raw",
                "bridge",
                "transaction",
                "undirected",
                ENT_ORDER,
                row_note,
            ),
            _link(
                "L12",
                wh_id,
                "raw",
                wh_order_ref,
                "digits.0",
                "bridge",
                "transaction",
                "undirected",
                ENT_ORDER,
                row_note,
            ),
            _link(
                "L13",
                wh_order_ref,
                "digits.0",
                wh_po,
                "raw",
                "bridge",
                "transaction",
                "undirected",
                ENT_ORDER,
                "Bridge inside purchase_orders rows: two columns of one row.",
            ),
        ]
    )
    if rename_active:
        links.extend(
            [
                _link(
                    "L11b",
                    wh_id,
                    "raw",
                    wh_po_renamed,
                    "raw",
                    "bridge",
                    "transaction",
                    "undirected",
                    ENT_ORDER,
                    f"{row_note} Rows after the rename (fault F4).",
                ),
                _link(
                    "L13b",
                    wh_order_ref,
                    "digits.0",
                    wh_po_renamed,
                    "raw",
                    "bridge",
                    "transaction",
                    "undirected",
                    ENT_ORDER,
                    "Two columns of one row, after the rename (fault F4).",
                ),
            ]
        )
    file_note = "Bridge inside 'shipment created' lines: the SHIP file is a batch key (spec 9.7)."
    links.extend(
        [
            _link(
                "L14",
                ship_file,
                "raw",
                ship_no,
                "raw",
                "bridge",
                "batch",
                "undirected",
                ENT_SHIPPING_FILE,
                file_note,
            ),
            _link(
                "L15",
                ship_file,
                "raw",
                ship_po,
                "raw",
                "bridge",
                "batch",
                "undirected",
                ENT_SHIPPING_FILE,
                file_note,
            ),
        ]
    )
    return links


def build_entities(rename_active: bool) -> list[EntityTruth]:
    def ent(system_id: str, source_id: str, field_name: str, form: str = "raw") -> EntityField:
        return EntityField(ref=_ref(system_id, source_id, field_name), form=form)

    order_fields = [
        ent(SYS_WEBSTORE, SRC_WEBSTORE, "cart_id"),
        ent(SYS_ORDERS, SRC_ORDERS, "cart_id"),
        ent(SYS_ORDERS, SRC_ORDERS, "order_id"),
        ent(SYS_ORDERS, SRC_ORDERS, "merchant_ref"),
        ent(SYS_PAYMENTS, SRC_PAYMENTS, "merchantRef"),
        ent(SYS_WAREHOUSE, SRC_WMS_DB, "id"),
        ent(SYS_WAREHOUSE, SRC_WMS_DB, "order_ref", "digits.0"),
        ent(SYS_WAREHOUSE, SRC_WMS_DB, "po_num"),
    ]
    if rename_active:
        order_fields.append(ent(SYS_WAREHOUSE, SRC_WMS_DB, "po_number"))
    order_fields.extend(
        [ent(SYS_SHIPPING, SRC_SHIP_LOG, "po_num"), ent(SYS_SHIPPING, SRC_SHIP_LOG, "shipment_no")]
    )
    return [
        EntityTruth(entity_id=ENT_ORDER, name="Order", fields=order_fields),
        EntityTruth(
            entity_id=ENT_SHIPPING_FILE,
            name="Shipping file",
            fields=[
                ent(SYS_SHIPPING, SRC_SHIP_SFTP, "file_name"),
                ent(SYS_SHIPPING, SRC_SHIP_LOG, "file"),
            ],
        ),
        EntityTruth(
            entity_id=ENT_MANIFEST,
            name="Carrier manifest",
            fields=[ent(SYS_SHIPPING, SRC_SHIP_LOG, "manifest_id")],
        ),
    ]


def build_batches(
    exported_files: Iterable[ExportedFile], manifests: dict[str, list[str]]
) -> list[BatchTruth]:
    batches = [
        BatchTruth(
            batch_id=exported.batch_id,
            key_field=_ref(SYS_SHIPPING, SRC_SHIP_SFTP, "file_name"),
            key_value=exported.name,
            txn_ids=exported.txn_ids,
        )
        for exported in exported_files
    ]
    batches.extend(
        BatchTruth(
            batch_id=f"batch_{manifest_id}",
            key_field=_ref(SYS_SHIPPING, SRC_SHIP_LOG, "manifest_id"),
            key_value=manifest_id,
            txn_ids=txn_ids,
        )
        for manifest_id, txn_ids in sorted(manifests.items())
    )
    return batches


def build_faults(
    enabled: bool,
    faults: FaultState,
    transactions: Iterable[Transaction],
    purchase_orders: Iterable[PurchaseOrder],
    window_start: datetime,
) -> list[FaultTruth]:
    """One FaultTruth per active fault (spec 19 scenario A faults, 18.4 detection metrics)."""
    if not enabled:
        return []
    result: list[FaultTruth] = []
    if faults.f1 is not None:
        result.append(
            FaultTruth(
                fault_id="f1_payments_503_spike",
                kind=FaultKind.ERROR_SPIKE,
                start=faults.f1[0],
                end=faults.f1[1],
                system_id=SYS_PAYMENTS,
                source_id=SRC_PAYMENTS,
                affected_txn_ids=sorted(
                    txn.txn_id for txn in transactions if len(txn.attempts) > 1
                ),
                cause_evidence="Service Unavailable",
                expected_alert_kind="error_rate",
                expected_cause_kind="error_spike",
                notes=(
                    "Every payment attempt in the window answers 503; "
                    "orders retry every 5 to 10 min."
                ),
            )
        )
    if faults.f2_expected_by is not None:
        result.append(
            FaultTruth(
                fault_id="f2_missing_nightly_file",
                kind=FaultKind.MISSING_FILE,
                start=faults.f2_expected_by,
                end=faults.f2_arrival,
                system_id=SYS_SHIPPING,
                source_id=SRC_SHIP_SFTP,
                affected_txn_ids=faults.f2_affected,
                cause_evidence="SFTP upload failed: Permission denied",
                expected_alert_kind="schedule",
                expected_cause_kind="error_spike",
                notes=(
                    "The export ran, 12 'Permission denied' errors followed and no file landed; "
                    "the next night's file lists both days' POs."
                ),
            )
        )
    if faults.f3 is not None:
        result.append(
            FaultTruth(
                fault_id="f3_webstore_outage",
                kind=FaultKind.VISIBILITY_GAP,
                start=faults.f3[0],
                end=faults.f3[1],
                system_id=SYS_WEBSTORE,
                source_id=SRC_WEBSTORE,
                affected_txn_ids=sorted(faults.f3_affected),
                expected_alert=True,
                expected_alert_kind="freshness",
                expected_cause_kind="visibility_gap",
                notes="must be reported as a visibility gap, never as a stall (spec 10.2)",
            )
        )
    if faults.f4_start is not None:
        result.append(
            FaultTruth(
                fault_id="f4_schema_rename",
                kind=FaultKind.SCHEMA_DRIFT,
                start=faults.f4_start,
                end=None,
                system_id=SYS_WAREHOUSE,
                source_id=SRC_WMS_DB,
                attributes={"field": "po_num", "renamed_to": "po_number"},
                expected_alert_kind="schema_drift",
                expected_cause_kind="schema_drift",
            )
        )
    if faults.f5_start is not None:
        result.append(
            FaultTruth(
                fault_id="f5_partial_stall_dc03",
                kind=FaultKind.PARTIAL_STALL,
                start=faults.f5_start,
                end=None,
                system_id=SYS_WAREHOUSE,
                source_id=SRC_WMS_DB,
                affected_txn_ids=sorted(po.txn_id for po in purchase_orders if po.stalled),
                attributes={"warehouse_code": STALLED_WAREHOUSE},
                expected_alert_kind="hop_deadline",
                expected_cause_kind="partial_attribute_lift",
                notes=f"what's different: warehouse_code={STALLED_WAREHOUSE}",
            )
        )
    result.append(
        FaultTruth(
            fault_id="f6_warehouse_clock_skew",
            kind=FaultKind.CLOCK_SKEW,
            start=window_start,
            end=None,
            system_id=SYS_WAREHOUSE,
            source_id=None,
            attributes={"offset_seconds": str(WAREHOUSE_SKEW_SECONDS)},
            expected_alert=False,
            expected_alert_kind=None,
            notes=(
                "Both warehouse sources render true time + 90 s; "
                "a source property, not an incident."
            ),
        )
    )
    return result


def build_manual_hops() -> list[ManualHopTruth]:
    """The manual hop (payments to warehouse, spec 11.1) and the automated ones."""
    automated = [
        ("hop_webstore_cart_to_checkout", N_CART, N_CHECKOUT),
        ("hop_webstore_to_orders", N_CHECKOUT, N_ORDER_CREATED),
        ("hop_orders_created_to_payment_requested", N_ORDER_CREATED, N_PAYMENT_REQUESTED),
        ("hop_orders_to_payments", N_PAYMENT_REQUESTED, N_AUTHORIZED),
        ("hop_payments_to_orders", N_AUTHORIZED, N_ORDER_RELEASED),
        ("hop_warehouse_to_shipping", N_PO_CREATED, N_FILE_ARRIVED),
        ("hop_shipping_file_to_shipment", N_FILE_ARRIVED, N_SHIPMENT_CREATED),
        ("hop_shipping_shipment_to_pickup", N_SHIPMENT_CREATED, N_CARRIER_PICKUP),
    ]
    hops = [
        ManualHopTruth(
            hop_id="hop_payments_to_warehouse",
            from_node=N_AUTHORIZED,
            to_node=N_PO_CREATED,
            entity_id=ENT_ORDER,
            manual=True,
            share_manual=CLERK_SHARE,
            actors=list(CLERKS),
            typo_rate=CLERK_TYPO_RATE,
        )
    ]
    hops.extend(
        ManualHopTruth(
            hop_id=hop_id,
            from_node=a,
            to_node=b,
            entity_id=ENT_ORDER,
            manual=False,
            share_manual=0.0,
        )
        for hop_id, a, b in automated
    )
    return hops


def build_marker_set(
    marker_tokens: set[str],
    pii_values: set[str],
    identifier_values: dict[str, set[str]],
    actor_values: set[str] | None = None,
    amount_values: set[str] | None = None,
) -> MarkerSet:
    return MarkerSet(
        marker_tokens=sorted(marker_tokens),
        pii_values=sorted(pii_values),
        identifier_values={
            key: sorted(values) for key, values in sorted(identifier_values.items())
        },
        actor_values=sorted(actor_values or ()),
        amount_values=sorted(amount_values or ()),
    )


def build_manifest(
    request: GenerationRequest,
    scenario: str,
    emitted: Iterable[Emitted],
    transaction_count: int,
    hashes: dict[str, str],
) -> Manifest:
    counts: dict[str, int] = {}
    events = 0
    noise = 0
    for item in emitted:
        counts[item.source_id] = counts.get(item.source_id, 0) + 1
        if item.truth.txn_id is not None:
            events += 1
        if item.truth.node.endswith(":noise"):
            noise += 1
    for source in SOURCES:
        counts.setdefault(source.source_id, 0)
    counts["transactions"] = transaction_count
    counts["events"] = events
    counts["noise"] = noise
    counts["files"] = len(hashes)
    return Manifest(
        generator_version=GENERATOR_VERSION,
        scenario=scenario,
        seed=request.seed,
        days=request.days,
        start_date=request.start_date,
        daily_volume=request.daily_volume,
        faults_enabled=request.faults,
        noise_rate=request.noise_rate,
        pii_density=request.pii_density,
        counts=counts,
        sha256=hashes,
    )


def write_readme(out_dir: Path, truth: GroundTruth, start_day: date, zone: tzinfo) -> None:
    """``README.md`` next to the data: sources, counts, faults and the determinism note."""
    events_digest = hashlib.sha256(
        (out_dir / GROUND_TRUTH_DIR / EVENTS_FILE).read_bytes()
    ).hexdigest()
    manifest = truth.manifest
    faults_state = "on" if manifest.faults_enabled else "off"
    lines = [
        "# Scenario A (shop): generated data",
        "",
        (
            f"Generated by carto-sim {GENERATOR_VERSION}: {manifest.days} days from "
            f"{start_day.isoformat()} (local time {clock.LOCAL_ZONE_KEY}), "
            f"{manifest.daily_volume} orders per weekday, seed {manifest.seed}, faults "
            f"{faults_state}, noise rate {manifest.noise_rate}, PII density "
            f"{manifest.pii_density}. Everything here is synthetic."
        ),
        "",
        "## Sources",
        "",
        "| Source | System | Format | Path | Timezone | Clock skew (s) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {s.source_id} | {s.system_name} ({s.system_id}) | {s.format.value} | {s.path} | "
        f"{s.timezone} | {s.clock_skew_seconds} |"
        for s in truth.sources
    )
    lines.extend(["", "## Counts", "", "| Key | Count |", "| --- | --- |"])
    lines.extend(f"| {key} | {value} |" for key, value in sorted(manifest.counts.items()))
    lines.extend(["", "## Faults", ""])
    if truth.faults:
        lines.extend(
            [
                "| Fault | Kind | Window (local) | Affected transactions |",
                "| --- | --- | --- | --- |",
            ]
        )
        for fault in truth.faults:
            start_local = clock.to_local(fault.start, zone)
            start = start_local.strftime("%Y-%m-%d %H:%M")
            if fault.end is None:
                end = "open"
            else:
                end_local = clock.to_local(fault.end, zone)
                same_day = end_local.date() == start_local.date()
                end = end_local.strftime("%H:%M" if same_day else "%Y-%m-%d %H:%M")
            affected = len(fault.affected_txn_ids)
            lines.append(
                f"| {fault.fault_id} | {fault.kind.value} | {start} to {end} | {affected} |"
            )
    else:
        lines.append("None (generated with faults off).")
    lines.extend(
        [
            "",
            "## Determinism",
            "",
            (
                f"Seed {manifest.seed}. sha256 of ground_truth/event_txn.ndjson: "
                f"`{events_digest}`. ground_truth/manifest.json carries the digest of every "
                "native file. The same request produces byte-identical output on every platform."
            ),
            "",
        ]
    )
    (out_dir / "README.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
