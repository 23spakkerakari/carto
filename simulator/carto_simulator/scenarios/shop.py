"""Scenario A, "shop" (spec 19): webstore, order system, payments, warehouse, shipping.

One transaction is one order. It starts as a webstore cart, becomes an order, is authorized by
the payment processor, turns into a warehouse purchase order (70% by an integration service, 30%
keyed by clerks during business hours from the payment confirmation, with 2% typos in the order
reference: the composite path of spec 9.5 and 11.1), is picked, exported in the nightly
``SHIP_YYYYMMDD_HHMM.csv`` file (a batch, spec 9.7), shipped and picked up by the carrier on a
manifest (another batch). Faults are the ones spec 19 lists for scenario A; see
``simulator/README.md`` for every timing and the choices made where the spec is silent.

Generation is deterministic: every random draw comes from one ``random.Random(seed)`` consumed in
a fixed order (cart times, then each transaction's lifecycle in cart order, then per-day export
parameters, then the nightly exports day by day, then noise source by source). Nothing reads the
wall clock.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from carto_simulator import clock
from carto_simulator.api import GenerationRequest, GenerationResult
from carto_simulator.formats import (
    csv_text,
    iso_local_offset_ms,
    iso_utc_ms,
    logfmt_line,
    naive_local_seconds,
    ndjson_line,
    sql_insert,
    sql_string,
    text_line,
    xml_line,
)
from carto_simulator.ground_truth import (
    GROUND_TRUTH_DIR,
    MANIFEST_FILE,
    ActorKind,
    EventTruth,
    GroundTruth,
    row_key,
    write_ground_truth,
)
from carto_simulator.names import CLERKS, SERVICE_ACCOUNT, MarkerFactory, Person, draw_person
from carto_simulator.scenarios import shop_truth
from carto_simulator.scenarios.shop_model import (
    ALTER_RENAME,
    CARRIERS,
    CHANNELS,
    CLERK_SHARE,
    CLERK_START_DELAY,
    CLERK_TYPO_RATE,
    CREATE_TABLE,
    CSV_COLUMNS,
    F1_DAY,
    F1_END,
    F1_START,
    F2_DAY,
    F3_DAY,
    F3_END,
    F3_START,
    F4_DAY,
    F5_DAY,
    F5_START,
    HOUR_WEIGHTS,
    N_AUTHORIZED,
    N_CARRIER_PICKUP,
    N_CART,
    N_CHECKOUT,
    N_ERROR_503,
    N_EXPORT_FINISHED,
    N_FILE_ARRIVED,
    N_ORDER_CREATED,
    N_ORDER_RELEASED,
    N_ORDERS_NOISE,
    N_PAYMENT_FAILED,
    N_PAYMENT_REQUESTED,
    N_PAYMENTS_NOISE,
    N_PO_CREATED,
    N_SHIPMENT_CREATED,
    N_SHIPPING_NOISE,
    N_UPLOAD_COMPLETE,
    N_UPLOAD_FAILED,
    N_WAREHOUSE_NOISE,
    N_WEBSTORE_NOISE,
    NOISE_PER_HOUR,
    PAYMENT_METHOD_WEIGHTS,
    PAYMENT_METHODS,
    PO_TABLE,
    REGIONS,
    RENAMED_CSV_COLUMNS,
    SERVICE_LEVELS,
    SHIP_FILE_COLUMNS,
    SOURCES,
    SRC_ORDERS,
    SRC_PAYMENTS,
    SRC_SHIP_LOG,
    SRC_SHIP_SFTP,
    SRC_WEBSTORE,
    SRC_WMS_DB,
    SRC_WMS_EXPORT,
    STALLED_WAREHOUSE,
    SYS_ORDERS,
    SYS_PAYMENTS,
    SYS_SHIPPING,
    SYS_WAREHOUSE,
    SYS_WEBSTORE,
    UPLOAD_FAILURE_FIRST,
    UPLOAD_FAILURE_LAST,
    UPLOAD_FAILURES,
    WAREHOUSE_CODES,
    WAREHOUSE_SKEW,
    WAREHOUSE_WEIGHTS,
    CounterBases,
    DayParams,
    ExportedFile,
    FaultState,
    PaymentAttempt,
    PurchaseOrder,
    Transaction,
    apply_typo,
    draw_ms,
    draw_seconds,
    po_number,
    poisson,
)
from carto_simulator.writers import DroppedFile, Emitted, FileDropSink, LogSink, Truth

SERVICE = ActorKind.SERVICE


class _ShopRun:
    """State of one generation; a fresh instance per :meth:`ShopScenario.generate`."""

    def __init__(self, request: GenerationRequest, out_dir: Path) -> None:
        self.request = request
        self.out_dir = out_dir
        self.rng = random.Random(request.seed)
        self.counters = CounterBases.for_seed(request.seed)
        self.zone = clock.local_zone()
        self.start_day = request.start_date
        self.end_day = request.start_date + timedelta(days=request.days)
        self.window_start = clock.local_midnight_utc(self.start_day, self.zone)
        self.window_end = clock.local_midnight_utc(self.end_day, self.zone)
        self.markers = MarkerFactory(self.rng)
        self.faults = FaultState()
        self.transactions: list[Transaction] = []
        self.purchase_orders: list[PurchaseOrder] = []
        self.exported_files: list[ExportedFile] = []
        self.manifests: dict[str, list[str]] = {}
        self.identifier_values: dict[str, set[str]] = {}
        self.pii_values: set[str] = set()
        self.marker_tokens: set[str] = set()
        self.day_params: dict[date, DayParams] = {}
        self.emitted: list[Emitted] = []
        local = self.zone
        self.webstore = LogSink(
            SRC_WEBSTORE,
            SYS_WEBSTORE,
            "webstore",
            lambda t: f"app-{t:%Y-%m-%d}.ndjson",
            UTC,
            iso_utc_ms,
        )
        self.orders = LogSink(
            SRC_ORDERS,
            SYS_ORDERS,
            "orders",
            lambda t: f"order-svc-{t:%Y-%m-%d}.log",
            UTC,
            iso_utc_ms,
        )
        self.payments = LogSink(
            SRC_PAYMENTS,
            SYS_PAYMENTS,
            "payments",
            lambda t: f"messages-{t:%Y-%m-%d}.xml",
            local,
            lambda t: iso_local_offset_ms(t.astimezone(local)),
        )
        self.export_log = LogSink(
            SRC_WMS_EXPORT,
            SYS_WAREHOUSE,
            "warehouse",
            lambda t: f"export-job-{t:%Y-%m-%d}.log",
            local,
            lambda t: naive_local_seconds(t.astimezone(local)),
            skew=WAREHOUSE_SKEW,
        )
        self.sftp = FileDropSink(SRC_SHIP_SFTP, SYS_SHIPPING, "shipping/outbound")
        self.shipping = LogSink(
            SRC_SHIP_LOG,
            SYS_SHIPPING,
            "shipping",
            lambda t: f"shipping-app-{t:%Y-%m-%d}.log",
            UTC,
            iso_utc_ms,
        )

    # -- helpers ------------------------------------------------------------------------------

    def day(self, number: int) -> date:
        """Calendar date of 1-based day ``number`` of the run."""
        return self.start_day + timedelta(days=number - 1)

    def day_in_window(self, number: int) -> bool:
        return 1 <= number <= self.request.days

    def local(self, day: date, at: time) -> datetime:
        """UTC instant of a local wall-clock time on ``day``."""
        return clock.to_utc(clock.local_wall(day, at, self.zone))

    def in_window(self, instant: datetime) -> bool:
        return instant < self.window_end

    def record_identifier(self, system_id: str, source_id: str, field: str, value: str) -> None:
        """Remember an emitted identifier value under ``str(FieldRef)`` (spec 18.3 markers)."""
        key = f"{system_id}/{source_id}/{field}"  # the same shape as str(FieldRef(...))
        values = self.identifier_values.get(key)
        if values is None:
            values = self.identifier_values[key] = set()
        values.add(value)

    # -- faults -------------------------------------------------------------------------------

    def configure_faults(self) -> None:
        if not self.request.faults:
            return
        faults = self.faults
        if self.day_in_window(F1_DAY):
            day = self.day(F1_DAY)
            faults.f1 = (self.local(day, F1_START), self.local(day, F1_END))
        if self.day_in_window(F2_DAY):
            faults.f2_day = self.day(F2_DAY)
        if self.day_in_window(F3_DAY):
            day = self.day(F3_DAY)
            faults.f3 = (self.local(day, F3_START), self.local(day, F3_END))
        if self.day_in_window(F4_DAY):
            faults.f4_start = self.local(self.day(F4_DAY), time(0))
        if self.day_in_window(F5_DAY):
            faults.f5_start = self.local(self.day(F5_DAY), F5_START)

    def payment_fails(self, requested_at: datetime) -> bool:
        window = self.faults.f1
        return window is not None and window[0] <= requested_at < window[1]

    # -- phase A: cart creation times ---------------------------------------------------------

    def draw_cart_times(self) -> list[datetime]:
        """Per day: weekday volume (half on weekends), hour-of-day weights, uniform within hour."""
        rng = self.rng
        hours = list(range(24))
        times: list[datetime] = []
        for offset in range(self.request.days):
            day = self.start_day + timedelta(days=offset)
            volume = self.request.daily_volume
            if not clock.is_business_day(day):
                volume = (volume + 1) // 2
            midnight = clock.local_wall(day, zone=self.zone)
            day_times = [
                clock.to_utc(midnight + timedelta(hours=h, milliseconds=rng.randint(0, 3_599_999)))
                for h in rng.choices(hours, weights=HOUR_WEIGHTS, k=volume)
            ]
            day_times.sort()
            times.extend(day_times)
        return times

    # -- phase B: one transaction's lifecycle -------------------------------------------------

    def build_transaction(self, index: int, cart_at: datetime) -> Transaction:
        rng = self.rng
        checkout_at = cart_at + draw_ms(rng, 120, 1200)
        channel = rng.choice(CHANNELS)
        region = rng.choice(REGIONS)
        items = rng.randint(1, 6)
        total = f"{round(rng.uniform(15.0, 900.0), 2):.2f}"
        payment_method = rng.choices(PAYMENT_METHODS, weights=PAYMENT_METHOD_WEIGHTS, k=1)[0]
        person = draw_person(rng, self.markers)
        checkout_pii = rng.random() < self.request.pii_density
        order_created_at = checkout_at + draw_ms(rng, 5, 60)
        attempts: list[PaymentAttempt] = []
        requested_at = order_created_at + draw_ms(rng, 5, 30)
        while True:
            responded_at = requested_at + draw_ms(rng, 1, 15)
            if not self.payment_fails(requested_at):
                attempts.append(PaymentAttempt(requested_at, responded_at, None))
                break
            failure_logged_at = responded_at + draw_ms(rng, 0.05, 0.5)
            attempts.append(PaymentAttempt(requested_at, responded_at, failure_logged_at))
            requested_at = failure_logged_at + draw_ms(rng, 300, 600)
        authorized_at = attempts[-1].responded_at
        cardholder_pii = rng.random() < self.request.pii_density
        released_at = authorized_at + draw_ms(rng, 1, 10)
        txn_id = f"txn_{index:06d}"
        order_id = self.counters.order + index - 1
        po = self.build_purchase_order(
            txn_id, order_id, total, person, order_created_at, authorized_at
        )
        return Transaction(
            txn_id=txn_id,
            cart_id=f"c-{self.counters.cart + index - 1}",
            order_id=order_id,
            merchant_ref=f"X9-{self.counters.merchant + index - 1}",
            cart_at=cart_at,
            checkout_at=checkout_at,
            channel=channel,
            region=region,
            items=items,
            total=total,
            payment_method=payment_method,
            person=person,
            checkout_pii=checkout_pii,
            cardholder_pii=cardholder_pii,
            order_created_at=order_created_at,
            attempts=attempts,
            released_at=released_at,
            po=po,
        )

    def build_purchase_order(
        self,
        txn_id: str,
        order_id: int,
        total: str,
        person: Person,
        order_created_at: datetime,
        authorized_at: datetime,
    ) -> PurchaseOrder | None:
        """The warehouse row: automated minutes later, or clerk-entered in business hours."""
        rng = self.rng
        human = rng.random() < CLERK_SHARE
        if human:
            created_by = rng.choice(CLERKS)
            base = clock.first_business_instant(authorized_at + CLERK_START_DELAY)
            created_at = clock.floor_seconds(base + draw_seconds(rng, 0, 240 * 60))
            base_day = clock.to_local(base, self.zone).date()
            if created_at >= clock.business_close(base_day):
                next_day = clock.next_business_day(base_day)
                created_at = clock.business_open(next_day) + draw_seconds(rng, 0, 3600)
        else:
            created_by = SERVICE_ACCOUNT
            created_at = clock.floor_seconds(authorized_at + draw_seconds(rng, 60, 300))
        warehouse_code = rng.choices(WAREHOUSE_CODES, weights=WAREHOUSE_WEIGHTS, k=1)[0]
        order_ref = f"SO-{order_id:07d}"
        typo = human and rng.random() < CLERK_TYPO_RATE
        if typo:
            order_ref = apply_typo(rng, order_ref)
        picked_at = created_at + draw_seconds(rng, 30 * 60, 240 * 60)
        if not clock.inside_warehouse_hours(picked_at):
            picked_at = clock.next_warehouse_opening(picked_at) + draw_seconds(rng, 0, 3600)
        if not self.in_window(created_at):
            return None
        stalled = (
            self.faults.f5_start is not None
            and warehouse_code == STALLED_WAREHOUSE
            and created_at >= self.faults.f5_start
        )
        return PurchaseOrder(
            txn_id=txn_id,
            order_id=order_id,
            order_ref=order_ref,
            warehouse_code=warehouse_code,
            total=total,
            order_date=clock.to_local(order_created_at, self.zone).date(),
            person=person,
            created_by=created_by,
            human=human,
            created_at=created_at,
            typo=typo,
            stalled=stalled,
            picked_at=None if stalled or not self.in_window(picked_at) else picked_at,
        )

    def number_purchase_orders(self) -> None:
        """Row ids and PO numbers follow creation order, as the database would assign them."""
        self.purchase_orders.sort(key=lambda po: (po.created_at, po.txn_id))
        rename_from = self.faults.f4_start
        for index, po in enumerate(self.purchase_orders):
            po.row_id = self.counters.row + index
            po.po_num = po_number(self.counters.po + index)
            po.renamed = rename_from is not None and po.created_at >= rename_from
            if po.picked_at is not None:
                po.status = "PICKED"
                po.updated_at = po.picked_at
            else:
                po.updated_at = po.created_at

    # -- phase C: nightly exports, file drops, shipments, manifests ---------------------------

    def draw_day_params(self) -> None:
        rng = self.rng
        for offset in range(self.request.days):
            day = self.start_day + timedelta(days=offset)
            self.day_params[day] = DayParams(
                export_offset=draw_seconds(rng, 0, 300),
                upload_delay=draw_seconds(rng, 30, 60),
                arrival_delay=draw_ms(rng, 180, 480),
                pickup_offset=draw_seconds(rng, 0, 1800),
            )

    def run_exports(self) -> list[PurchaseOrder]:
        """Export each day's due POs (plus any carried over); returns the shipped POs."""
        rng = self.rng
        due: dict[date, list[PurchaseOrder]] = {}
        for po in self.purchase_orders:
            if po.picked_at is not None:
                due.setdefault(clock.export_day_for_pick(po.picked_at), []).append(po)
        carry: list[PurchaseOrder] = []
        shipped: list[PurchaseOrder] = []
        for offset in range(self.request.days):
            day = self.start_day + timedelta(days=offset)
            fresh = sorted(
                due.get(day, []), key=lambda po: (po.picked_at or po.created_at, po.row_id)
            )
            listed = carry + fresh
            carry = []
            if not listed:
                continue
            params = self.day_params[day]
            export_at = self.local(day, clock.EXPORT_START) + params.export_offset
            if not self.in_window(export_at):
                continue
            stamp = clock.to_local(export_at + WAREHOUSE_SKEW, self.zone)
            file_name = f"SHIP_{stamp:%Y%m%d_%H%M}.csv"
            batch_id = f"batch_{file_name.removesuffix('.csv')}"
            failing = self.faults.f2_day == day
            self.export_log.add(
                Truth(N_EXPORT_FINISHED, export_at, None, None if failing else batch_id, SERVICE),
                self.export_text(
                    export_at,
                    "INFO",
                    f"PO export finished: {len(listed)} POs written to {file_name}",
                ),
            )
            if failing:
                self.emit_upload_failures(day, export_at, file_name)
                self.faults.f2_expected_by = self.local(day, clock.EXPECTED_FILE_BY)
                self.faults.f2_affected = sorted(po.txn_id for po in listed)
                carry = listed
                continue
            for po in listed:
                po.carrier = rng.choice(CARRIERS)
                po.service = rng.choice(SERVICE_LEVELS)
                po.weight = f"{round(rng.uniform(0.2, 30.0), 2):.2f}"
            content = csv_text(
                SHIP_FILE_COLUMNS,
                [
                    (po.po_num, po.order_ref, po.warehouse_code, po.carrier, po.service, po.weight)
                    for po in listed
                ],
            )
            upload_at = export_at + params.upload_delay
            size = len(content.encode("utf-8"))
            self.export_log.add(
                Truth(N_UPLOAD_COMPLETE, upload_at, None, batch_id, SERVICE),
                self.export_text(
                    upload_at, "INFO", f"SFTP upload complete: {file_name} ({size} bytes)"
                ),
            )
            arrived_at = export_at + params.arrival_delay
            if not self.in_window(arrived_at):
                continue
            truth = Truth(N_FILE_ARRIVED, arrived_at, None, batch_id)
            self.sftp.add(DroppedFile(file_name, content, arrived_at, truth))
            self.record_identifier(SYS_SHIPPING, SRC_SHIP_SFTP, "file_name", file_name)
            txn_ids = sorted(po.txn_id for po in listed)
            self.exported_files.append(ExportedFile(file_name, batch_id, arrived_at, txn_ids))
            if self.faults.f2_expected_by is not None and day == self.faults.f2_day_after():
                self.faults.f2_arrival = arrived_at
            for po in listed:
                shipped_at = arrived_at + draw_ms(rng, 300, 4200)
                if self.in_window(shipped_at):
                    po.file_name = file_name
                    po.shipped_at = shipped_at
                    po.status = "SHIPPED"
                    po.updated_at = shipped_at
                    shipped.append(po)
        shipped.sort(key=lambda po: (po.shipped_at or po.created_at, po.row_id))
        return shipped

    def export_text(self, instant: datetime, level: str, message: str) -> str:
        return text_line(self.export_log.timestamp(instant), level, message)

    def emit_upload_failures(self, day: date, export_at: datetime, file_name: str) -> None:
        """Fault F2: 12 'Permission denied' errors between 21:13 and 21:19, after the export."""
        first = max(export_at + timedelta(seconds=30), self.local(day, UPLOAD_FAILURE_FIRST))
        last = self.local(day, UPLOAD_FAILURE_LAST)
        step = (last - first) / (UPLOAD_FAILURES - 1)
        message = f"SFTP upload failed: Permission denied (/outbound/shipping/{file_name})"
        for attempt in range(UPLOAD_FAILURES):
            failed_at = clock.floor_seconds(first + step * attempt)
            self.export_log.add(
                Truth(N_UPLOAD_FAILED, failed_at, None, None, SERVICE, is_error=True),
                self.export_text(failed_at, "ERROR", message),
            )

    def emit_shipments(self, shipped: list[PurchaseOrder]) -> None:
        """Shipment lines in creation order, then one carrier pickup line per manifest member."""
        pickup_days: dict[date, list[PurchaseOrder]] = {}
        for index, po in enumerate(shipped):
            shipped_at = po.shipped_at
            if shipped_at is None or po.file_name is None:
                continue
            po.shipment_no = f"SH-{self.counters.shipment + index}"
            batch_id = f"batch_{po.file_name.removesuffix('.csv')}"
            self.shipping.add(
                Truth(N_SHIPMENT_CREATED, shipped_at, po.txn_id, batch_id, SERVICE),
                logfmt_line(
                    [
                        ("ts", self.shipping.timestamp(shipped_at)),
                        ("level", "info"),
                        ("msg", "shipment created"),
                        ("shipment_no", po.shipment_no),
                        ("po_num", po.po_num),
                        ("file", po.file_name),
                        ("carrier", po.carrier),
                        ("service", po.service),
                    ]
                ),
            )
            self.record_identifier(SYS_SHIPPING, SRC_SHIP_LOG, "shipment_no", po.shipment_no)
            self.record_identifier(SYS_SHIPPING, SRC_SHIP_LOG, "po_num", po.po_num)
            self.record_identifier(SYS_SHIPPING, SRC_SHIP_LOG, "file", po.file_name)
            local_day = clock.to_local(shipped_at, self.zone).date()
            if shipped_at >= self.pickup_time(local_day):
                local_day += timedelta(days=1)
            pickup_days.setdefault(local_day, []).append(po)
        for pickup_day in sorted(pickup_days):
            if pickup_day not in self.day_params:
                continue
            pickup_at = self.pickup_time(pickup_day)
            if not self.in_window(pickup_at):
                continue
            manifest_id = f"MAN-{pickup_day:%Y%m%d}-01"
            batch_id = f"batch_{manifest_id}"
            members = pickup_days[pickup_day]
            self.manifests[manifest_id] = sorted(po.txn_id for po in members)
            for po in members:
                self.shipping.add(
                    Truth(N_CARRIER_PICKUP, pickup_at, po.txn_id, batch_id, SERVICE),
                    logfmt_line(
                        [
                            ("ts", self.shipping.timestamp(pickup_at)),
                            ("level", "info"),
                            ("msg", "carrier pickup"),
                            ("shipment_no", po.shipment_no),
                            ("manifest_id", manifest_id),
                            ("carrier", po.carrier),
                        ]
                    ),
                )
            self.record_identifier(SYS_SHIPPING, SRC_SHIP_LOG, "manifest_id", manifest_id)

    def pickup_time(self, day: date) -> datetime:
        params = self.day_params.get(day)
        offset = params.pickup_offset if params is not None else timedelta(0)
        return self.local(day, clock.PICKUP_START) + offset

    # -- emitting the per-transaction lines ----------------------------------------------------

    def emit_webstore(self, truth: Truth, text: str) -> bool:
        """Write a webstore line unless the day-10 outage swallows it (fault F3)."""
        window = self.faults.f3
        if window is not None and window[0] <= truth.observed_at < window[1]:
            if truth.txn_id is not None:
                self.faults.f3_affected.add(truth.txn_id)
            return False
        self.webstore.add(truth, text)
        return True

    def emit_transaction(self, txn: Transaction) -> None:
        if self.in_window(txn.cart_at):
            cart_line = ndjson_line(
                {
                    "ts": self.webstore.timestamp(txn.cart_at),
                    "level": "info",
                    "msg": "cart created",
                    "cart_id": txn.cart_id,
                    "items": txn.items,
                    "region": txn.region,
                    "channel": txn.channel,
                }
            )
            if self.emit_webstore(Truth(N_CART, txn.cart_at, txn.txn_id, None, SERVICE), cart_line):
                self.record_identifier(SYS_WEBSTORE, SRC_WEBSTORE, "cart_id", txn.cart_id)
        if not self.in_window(txn.checkout_at):
            return
        checkout: dict[str, object] = {
            "ts": self.webstore.timestamp(txn.checkout_at),
            "level": "info",
            "msg": "checkout completed",
            "cart_id": txn.cart_id,
            "total": float(txn.total),
            "payment_method": txn.payment_method,
        }
        if txn.checkout_pii:
            checkout["customer_name"] = txn.person.name
            checkout["customer_email"] = txn.person.email
        checkout_truth = Truth(N_CHECKOUT, txn.checkout_at, txn.txn_id, None, SERVICE)
        if self.emit_webstore(checkout_truth, ndjson_line(checkout)):
            self.record_identifier(SYS_WEBSTORE, SRC_WEBSTORE, "cart_id", txn.cart_id)
            if txn.checkout_pii:
                self.pii_values.update((txn.person.name, txn.person.email))
                self.marker_tokens.add(txn.person.email_marker)
        if not self.in_window(txn.order_created_at):
            return
        self.orders.add(
            Truth(N_ORDER_CREATED, txn.order_created_at, txn.txn_id, None, SERVICE),
            logfmt_line(
                [
                    ("ts", self.orders.timestamp(txn.order_created_at)),
                    ("level", "info"),
                    ("msg", "order created from cart"),
                    ("order_id", txn.order_id),
                    ("cart_id", txn.cart_id),
                    ("total", txn.total),
                    ("channel", txn.channel),
                ]
            ),
        )
        self.record_identifier(SYS_ORDERS, SRC_ORDERS, "order_id", str(txn.order_id))
        self.record_identifier(SYS_ORDERS, SRC_ORDERS, "cart_id", txn.cart_id)
        for attempt in txn.attempts:
            if not self.emit_payment_attempt(txn, attempt):
                return
        if not self.in_window(txn.released_at):
            return
        self.orders.add(
            Truth(N_ORDER_RELEASED, txn.released_at, txn.txn_id, None, SERVICE),
            logfmt_line(
                [
                    ("ts", self.orders.timestamp(txn.released_at)),
                    ("level", "info"),
                    ("msg", "order released"),
                    ("order_id", txn.order_id),
                    ("status", "RELEASED"),
                ]
            ),
        )

    def emit_payment_attempt(self, txn: Transaction, attempt: PaymentAttempt) -> bool:
        """Emit one request/response pair; False when the window ended before the response."""
        if not self.in_window(attempt.requested_at):
            return False
        self.orders.add(
            Truth(N_PAYMENT_REQUESTED, attempt.requested_at, txn.txn_id, None, SERVICE),
            logfmt_line(
                [
                    ("ts", self.orders.timestamp(attempt.requested_at)),
                    ("level", "info"),
                    ("msg", "payment requested"),
                    ("order_id", txn.order_id),
                    ("merchant_ref", txn.merchant_ref),
                    ("amount", txn.total),
                    ("currency", "USD"),
                ]
            ),
        )
        self.record_identifier(SYS_ORDERS, SRC_ORDERS, "merchant_ref", txn.merchant_ref)
        if not self.in_window(attempt.responded_at):
            return False
        failed = attempt.failure_logged_at is not None
        children: list[tuple[str, str]] = [
            ("timestamp", self.payments.timestamp(attempt.responded_at)),
            ("merchantRef", txn.merchant_ref),
            ("amount", txn.total),
            ("currency", "USD"),
        ]
        if txn.cardholder_pii:
            children.append(("cardholderName", txn.person.name))
            self.pii_values.add(txn.person.name)
        if failed:
            children.extend(
                [("status", "ERROR"), ("httpStatus", "503"), ("error", "Service Unavailable")]
            )
        else:
            children.extend([("status", "AUTHORIZED"), ("httpStatus", "200")])
        children.append(("processor", "cardnet"))
        node = N_ERROR_503 if failed else N_AUTHORIZED
        self.payments.add(
            Truth(node, attempt.responded_at, txn.txn_id, None, SERVICE, is_error=failed),
            xml_line("paymentMessage", children),
        )
        self.record_identifier(SYS_PAYMENTS, SRC_PAYMENTS, "merchantRef", txn.merchant_ref)
        if attempt.failure_logged_at is None:
            return True
        if not self.in_window(attempt.failure_logged_at):
            return False
        self.orders.add(
            Truth(N_PAYMENT_FAILED, attempt.failure_logged_at, txn.txn_id, None, SERVICE, True),
            logfmt_line(
                [
                    ("ts", self.orders.timestamp(attempt.failure_logged_at)),
                    ("level", "error"),
                    ("msg", "payment request failed"),
                    ("order_id", txn.order_id),
                    ("merchant_ref", txn.merchant_ref),
                    ("http_status", 503),
                ]
            ),
        )
        return True

    # -- phase D: noise -----------------------------------------------------------------------

    def emit_noise(self) -> None:
        """Poisson count per source and hour (expectation scaled by noise_rate), uniform within."""
        rng = self.rng
        rate = self.request.noise_rate
        hour = timedelta(hours=1)
        for source_id in (SRC_WEBSTORE, SRC_ORDERS, SRC_PAYMENTS, SRC_WMS_EXPORT, SRC_SHIP_LOG):
            mean = NOISE_PER_HOUR[source_id] * rate
            hour_start = self.window_start
            while hour_start < self.window_end:
                for _ in range(poisson(rng, mean)):
                    at = hour_start + timedelta(milliseconds=rng.randint(0, 3_599_999))
                    if self.in_window(at):
                        self.emit_noise_line(source_id, at)
                hour_start += hour

    def emit_noise_line(self, source_id: str, at: datetime) -> None:
        rng = self.rng
        if source_id == SRC_WEBSTORE:
            fields: dict[str, object] = {"ts": self.webstore.timestamp(at), "level": "info"}
            if rng.random() < 0.5:
                fields.update({"msg": "health check ok", "path": "/healthz", "status": 200})
            else:
                fields.update({"msg": "cache refresh", "keys": 1200, "region": rng.choice(REGIONS)})
            self.emit_webstore(Truth(N_WEBSTORE_NOISE, at), ndjson_line(fields))
        elif source_id == SRC_ORDERS:
            pairs: list[tuple[str, str | int]] = [
                ("ts", self.orders.timestamp(at)),
                ("level", "info"),
            ]
            if rng.random() < 0.5:
                pairs.extend([("msg", "scheduler tick"), ("queue", "orders")])
            else:
                pairs.extend([("msg", "db pool stats"), ("active", 3), ("idle", 7)])
            self.orders.add(Truth(N_ORDERS_NOISE, at), logfmt_line(pairs))
        elif source_id == SRC_PAYMENTS:
            self.payments.add(
                Truth(N_PAYMENTS_NOISE, at),
                xml_line(
                    "heartbeat", [("timestamp", self.payments.timestamp(at)), ("status", "OK")]
                ),
            )
        elif source_id == SRC_WMS_EXPORT:
            at = clock.floor_seconds(at)
            self.export_log.add(
                Truth(N_WAREHOUSE_NOISE, at),
                self.export_text(at, "INFO", "export scheduler idle, next run 21:10"),
            )
        else:
            self.shipping.add(
                Truth(N_SHIPPING_NOISE, at),
                logfmt_line(
                    [
                        ("ts", self.shipping.timestamp(at)),
                        ("level", "info"),
                        ("msg", "carrier rate table refreshed"),
                        ("carrier", rng.choice(CARRIERS)),
                        ("rows", 412),
                    ]
                ),
            )

    # -- native files -------------------------------------------------------------------------

    def row_values(self, po: PurchaseOrder) -> dict[str, str]:
        """Column values as the warehouse renders them (clock skew applied, naive local time)."""
        created = clock.to_local(po.created_at + WAREHOUSE_SKEW, self.zone)
        updated = clock.to_local((po.updated_at or po.created_at) + WAREHOUSE_SKEW, self.zone)
        return {
            "id": str(po.row_id),
            "po_num": po.po_num,
            "order_ref": po.order_ref,
            "status": po.status,
            "warehouse_code": po.warehouse_code,
            "order_total": po.total,
            "order_date": po.order_date.isoformat(),
            "customer_name": po.person.name,
            "ship_to_address": po.person.address,
            "created_by": po.created_by,
            "created_at": naive_local_seconds(created),
            "updated_at": naive_local_seconds(updated),
        }

    def write_warehouse_table(self) -> None:
        """purchase_orders.sql plus the CSV export(s); registers the row records (ADR 0006)."""
        directory = self.out_dir / "warehouse"
        directory.mkdir(parents=True, exist_ok=True)
        rename_active = self.faults.f4_start is not None
        plain_rows: list[list[str]] = []
        renamed_rows: list[list[str]] = []
        statements: list[str] = [CREATE_TABLE]
        renamed_statements: list[str] = []
        unquoted = {"id", "order_total"}  # integer and decimal literals; everything else quoted
        for po in self.purchase_orders:
            values = self.row_values(po)
            row = [values[column] for column in CSV_COLUMNS]
            literals = [
                v if c in unquoted else sql_string(v) for c, v in zip(CSV_COLUMNS, row, strict=True)
            ]
            if po.renamed:
                renamed_rows.append(row)
                renamed_statements.append(sql_insert(PO_TABLE, RENAMED_CSV_COLUMNS, literals))
                self.record_identifier(SYS_WAREHOUSE, SRC_WMS_DB, "po_number", po.po_num)
            else:
                plain_rows.append(row)
                statements.append(sql_insert(PO_TABLE, CSV_COLUMNS, literals))
                self.record_identifier(SYS_WAREHOUSE, SRC_WMS_DB, "po_num", po.po_num)
            self.record_identifier(SYS_WAREHOUSE, SRC_WMS_DB, "order_ref", po.order_ref)
            self.pii_values.update((po.person.name, po.person.address))
            self.marker_tokens.add(po.person.address_marker)
            actor = ActorKind.HUMAN if po.human else SERVICE
            truth = Truth(N_PO_CREATED, po.created_at, po.txn_id, None, actor)
            key = row_key(SRC_WMS_DB, PO_TABLE, po.row_id)
            self.emitted.append(Emitted(key, SRC_WMS_DB, SYS_WAREHOUSE, truth))
        if rename_active:
            statements.append(ALTER_RENAME)
            statements.extend(renamed_statements)
        self.write_text(directory / f"{PO_TABLE}.sql", "\n".join(statements) + "\n")
        self.write_text(directory / f"{PO_TABLE}.csv", csv_text(CSV_COLUMNS, plain_rows))
        if rename_active:
            renamed = csv_text(RENAMED_CSV_COLUMNS, renamed_rows)
            self.write_text(directory / f"{PO_TABLE}.renamed.csv", renamed)

    @staticmethod
    def write_text(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8", newline="\n")

    def write_native_files(self) -> None:
        for sink in (self.webstore, self.orders, self.payments, self.export_log, self.shipping):
            self.emitted.extend(sink.write(self.out_dir))
        self.emitted.extend(self.sftp.write(self.out_dir))
        self.write_warehouse_table()

    def native_file_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in sorted(self.out_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.out_dir).as_posix()
            if relative.startswith(f"{GROUND_TRUTH_DIR}/") or relative == "README.md":
                continue
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes

    # -- ground truth and orchestration -------------------------------------------------------

    def events(self) -> Iterator[EventTruth]:
        """Every emitted record in (observed_at, key) order."""
        self.emitted.sort(key=lambda item: (item.truth.observed_at, item.key))
        for item in self.emitted:
            truth = item.truth
            # model_construct skips validation on the hot path; the fields are built here from
            # typed records and every value is already UTC-aware.
            yield EventTruth.model_construct(
                key=item.key,
                source_id=item.source_id,
                system_id=item.system_id,
                node=truth.node,
                observed_at=truth.observed_at,
                txn_id=truth.txn_id,
                batch_id=truth.batch_id,
                actor_kind=truth.actor_kind,
                is_error=truth.is_error,
            )

    def prepare_out_dir(self) -> None:
        """Create ``out_dir`` or empty it, so stale files from an earlier run never remain.

        Only an empty directory or a previous run (one holding ``ground_truth/manifest.json``)
        is emptied; any other populated directory, and a path that is not a directory, is
        refused with ``ValueError`` so a mistyped ``--out`` never wipes a checkout.
        """
        out_dir = self.out_dir
        if not out_dir.exists():
            out_dir.mkdir(parents=True)
            return
        if not out_dir.is_dir():
            msg = f"{out_dir} exists and is not a directory"
            raise ValueError(msg)
        children = sorted(out_dir.iterdir())
        if children and not (out_dir / GROUND_TRUTH_DIR / MANIFEST_FILE).is_file():
            msg = (
                f"refusing to empty {out_dir}: it is not empty and holds no "
                f"{GROUND_TRUTH_DIR}/{MANIFEST_FILE} from a previous run"
            )
            raise ValueError(msg)
        for child in children:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    def run(self) -> GenerationResult:
        self.prepare_out_dir()
        self.configure_faults()
        for index, cart_at in enumerate(self.draw_cart_times(), start=1):
            txn = self.build_transaction(index, cart_at)
            self.transactions.append(txn)
            if txn.po is not None:
                self.purchase_orders.append(txn.po)
        self.number_purchase_orders()
        self.draw_day_params()
        self.emit_shipments(self.run_exports())
        for txn in self.transactions:
            self.emit_transaction(txn)
        self.emit_noise()
        self.write_native_files()
        hashes = self.native_file_hashes()
        rename_active = self.faults.f4_start is not None
        truth = GroundTruth(
            manifest=shop_truth.build_manifest(
                self.request, ShopScenario.name, self.emitted, len(self.transactions), hashes
            ),
            sources=list(SOURCES),
            links=shop_truth.build_links(
                rename_active, shop_truth.composite_agreement(self.transactions, self.zone)
            ),
            entities=shop_truth.build_entities(rename_active),
            batches=shop_truth.build_batches(self.exported_files, self.manifests),
            faults=shop_truth.build_faults(
                self.request.faults,
                self.faults,
                self.transactions,
                self.purchase_orders,
                self.window_start,
            ),
            manual_hops=shop_truth.build_manual_hops(),
            markers=shop_truth.build_marker_set(
                self.marker_tokens,
                self.pii_values,
                self.identifier_values,
                actor_values={po.created_by for po in self.purchase_orders if po.human},
                amount_values=_amount_renderings(
                    [txn.total for txn in self.transactions]
                    + [po.total for po in self.purchase_orders]
                ),
            ),
        )
        ground_truth_dir = write_ground_truth(self.out_dir, truth, self.events())
        shop_truth.write_readme(self.out_dir, truth, self.start_day, self.zone)
        return GenerationResult(
            out_dir=self.out_dir, ground_truth_dir=ground_truth_dir, manifest=truth.manifest
        )


class ShopScenario:
    """Scenario A registered as ``shop`` (spec 19, 21 M0)."""

    name = "shop"
    description = "Scenario A: an order's journey across five systems, with manual PO entry."

    def generate(self, request: GenerationRequest, out_dir: Path) -> GenerationResult:
        if request.scenario != self.name:
            msg = f"request is for scenario {request.scenario!r}, not {self.name!r}"
            raise ValueError(msg)
        return _ShopRun(request, out_dir).run()


def _amount_renderings(totals: list[str]) -> set[str]:
    """Every way an amount appears in the native files: ``123.40`` in logfmt, XML and rows, and
    ``123.4`` where the webstore writes it as a JSON number."""
    values: set[str] = set()
    for total in totals:
        values.add(total)
        values.add(json.dumps(float(total)))
    return values
