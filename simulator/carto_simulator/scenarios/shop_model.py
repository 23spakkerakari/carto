"""Scenario A constants and in-memory records shared by the generator and its ground truth.

Systems, sources and node names are the vocabulary the eval harness sees in ``sources.json`` and
``event_txn.ndjson``. Records are slotted dataclasses because the generator creates one per
emitted line; pydantic models are built only when ground truth is written.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from carto_simulator.ground_truth import SourceDef, SourceFormat, SourceKind
from carto_simulator.names import Person

# ---------------------------------------------------------------------------------------------
# Systems, sources, nodes, entities
# ---------------------------------------------------------------------------------------------

SYS_WEBSTORE = "sys_webstore"
SYS_ORDERS = "sys_orders"
SYS_PAYMENTS = "sys_payments"
SYS_WAREHOUSE = "sys_warehouse"
SYS_SHIPPING = "sys_shipping"

SRC_WEBSTORE = "src_webstore_log"
SRC_ORDERS = "src_orders_log"
SRC_PAYMENTS = "src_payments_xml"
SRC_WMS_DB = "src_wms_db"
SRC_WMS_EXPORT = "src_wms_export_log"
SRC_SHIP_SFTP = "src_ship_sftp"
SRC_SHIP_LOG = "src_ship_log"

PO_TABLE = "purchase_orders"
WAREHOUSE_SKEW_SECONDS = 90
WAREHOUSE_SKEW = timedelta(seconds=WAREHOUSE_SKEW_SECONDS)

N_CART = "webstore:cart_created"
N_CHECKOUT = "webstore:checkout_completed"
N_WEBSTORE_NOISE = "webstore:noise"
N_ORDER_CREATED = "orders:order_created"
N_PAYMENT_REQUESTED = "orders:payment_requested"
N_PAYMENT_FAILED = "orders:payment_failed"
N_ORDER_RELEASED = "orders:order_released"
N_ORDERS_NOISE = "orders:noise"
N_AUTHORIZED = "payments:authorized"
N_ERROR_503 = "payments:error_503"
N_PAYMENTS_NOISE = "payments:noise"
N_PO_CREATED = "warehouse:po_created"
N_EXPORT_FINISHED = "warehouse:export_finished"
N_UPLOAD_COMPLETE = "warehouse:upload_complete"
N_UPLOAD_FAILED = "warehouse:upload_failed"
N_WAREHOUSE_NOISE = "warehouse:noise"
N_FILE_ARRIVED = "shipping:file_arrived"
N_SHIPMENT_CREATED = "shipping:shipment_created"
N_CARRIER_PICKUP = "shipping:carrier_pickup"
N_SHIPPING_NOISE = "shipping:noise"

ENT_ORDER = "ent_order"
ENT_SHIPPING_FILE = "ent_shipping_file"
ENT_MANIFEST = "ent_manifest"

SOURCES: tuple[SourceDef, ...] = (
    SourceDef(
        source_id=SRC_WEBSTORE,
        system_id=SYS_WEBSTORE,
        system_name="Webstore",
        kind=SourceKind.LOG_FILE,
        format=SourceFormat.NDJSON,
        path="webstore",
        timezone="UTC",
        timestamp_format="iso8601",
        notes="app-YYYY-MM-DD.ndjson, one file per UTC day; timestamp key 'ts'.",
    ),
    SourceDef(
        source_id=SRC_ORDERS,
        system_id=SYS_ORDERS,
        system_name="Order system",
        kind=SourceKind.LOG_FILE,
        format=SourceFormat.LOGFMT,
        path="orders",
        timezone="UTC",
        timestamp_format="iso8601",
        notes="order-svc-YYYY-MM-DD.log, one file per UTC day; timestamp key 'ts'.",
    ),
    SourceDef(
        source_id=SRC_PAYMENTS,
        system_id=SYS_PAYMENTS,
        system_name="Payments",
        kind=SourceKind.LOG_FILE,
        format=SourceFormat.XML_LINES,
        path="payments",
        timezone="America/New_York",
        timestamp_format="iso8601 with offset",
        notes="messages-YYYY-MM-DD.xml, one file per local day, one document per line.",
    ),
    SourceDef(
        source_id=SRC_WMS_DB,
        system_id=SYS_WAREHOUSE,
        system_name="Warehouse",
        kind=SourceKind.SQL_TABLE,
        format=SourceFormat.SQL_ROWS,
        path="warehouse",
        timezone="America/New_York",
        timestamp_format="%Y-%m-%d %H:%M:%S",
        clock_skew_seconds=WAREHOUSE_SKEW_SECONDS,
        notes="Table purchase_orders: purchase_orders.sql seed plus CSV export(s) of the rows.",
    ),
    SourceDef(
        source_id=SRC_WMS_EXPORT,
        system_id=SYS_WAREHOUSE,
        system_name="Warehouse",
        kind=SourceKind.LOG_FILE,
        format=SourceFormat.TEXT,
        path="warehouse",
        timezone="America/New_York",
        timestamp_format="%Y-%m-%d %H:%M:%S",
        clock_skew_seconds=WAREHOUSE_SKEW_SECONDS,
        notes="export-job-YYYY-MM-DD.log, unstructured lines for template mining.",
    ),
    SourceDef(
        source_id=SRC_SHIP_SFTP,
        system_id=SYS_SHIPPING,
        system_name="Shipping",
        kind=SourceKind.FILE_DROP,
        format=SourceFormat.FILES,
        path="shipping/outbound",
        timezone="UTC",
        timestamp_format="mtime",
        notes="SHIP_YYYYMMDD_HHMM.csv drops; the edge reads metadata only (spec 8.1.5).",
    ),
    SourceDef(
        source_id=SRC_SHIP_LOG,
        system_id=SYS_SHIPPING,
        system_name="Shipping",
        kind=SourceKind.LOG_FILE,
        format=SourceFormat.LOGFMT,
        path="shipping",
        timezone="UTC",
        timestamp_format="iso8601",
        notes="shipping-app-YYYY-MM-DD.log, one file per UTC day; timestamp key 'ts'.",
    ),
)

# ---------------------------------------------------------------------------------------------
# Scenario parameters
# ---------------------------------------------------------------------------------------------

HOUR_WEIGHTS: tuple[float, ...] = (
    1.0, 1.0, 0.5, 0.5, 0.5, 1.0, 2.0, 4.0, 6.0, 8.0, 9.0, 10.0,
    10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 6.0, 3.0,
)  # fmt: skip
CHANNELS = ("web", "ios", "android")
REGIONS = ("us-east", "us-west", "eu-west")
PAYMENT_METHODS = ("card", "paypal", "apple_pay")
PAYMENT_METHOD_WEIGHTS = (0.8, 0.12, 0.08)
WAREHOUSE_CODES = ("DC-01", "DC-02", "DC-03", "DC-04")
WAREHOUSE_WEIGHTS = (0.4, 0.3, 0.2, 0.1)
CARRIERS = ("UPS", "FEDEX", "USPS")
SERVICE_LEVELS = ("GROUND", "2DAY")
STALLED_WAREHOUSE = "DC-03"

FIRST_CART_NUMBER = 10_000_000
FIRST_ORDER_ID = 4471
FIRST_MERCHANT_NUMBER = 200_000
FIRST_PO_COUNTER = 88210
FIRST_SHIPMENT_NUMBER = 3_000_000
FIRST_ROW_ID = 600_000
COUNTER_OFFSET_RANGES: dict[str, int] = {
    "cart": 5_000_000,
    "order": 1_000,
    "merchant": 50_000,
    "po": 5_000,
    "shipment": 1_000_000,
    "row": 100_000,
}
"""Each dense counter starts at its ``FIRST_*`` minimum plus a per-seed offset below this bound,
so the counters live at different magnitudes and their ``digits``/``alnum`` forms never share a
value: cart ids are 8 digits (``c-13088213``), order ids 4 to 5 (``4471``, the ``digits.0`` of
``order_ref`` ``SO-0004471``: link L05 by construction), merchant refs 6 (``X9-213442``), PO
numbers ``88-210`` to ``99-999`` then ``100-000`` (``alnum`` 88,210 upwards), shipment numbers 7
(``SH-3405521``) and warehouse row ids 6 (``600001`` upwards). The ranges stay disjoint for runs
of fewer than about 80,000 orders (order ids below 88,210), which covers 28 days at 2,800 orders
per weekday. Real systems' counters sit at unrelated magnitudes like this; equal dense ranges
made the eval's engine propose exact links between unrelated ids (M2 eval, package H follow-up)."""


@dataclass(frozen=True, slots=True)
class CounterBases:
    """The first value of each counter for a seed; see :data:`COUNTER_OFFSET_RANGES`."""

    cart: int
    order: int
    merchant: int
    po: int
    shipment: int
    row: int

    @classmethod
    def for_seed(cls, seed: int) -> CounterBases:
        """Offsets drawn from a generator of their own, so the scenario's draw sequence (times,
        people, clerks) is the same as before the offsets existed and tests can recompute them."""
        rng = random.Random(f"shop-counters-{seed}")
        offsets = {name: rng.randrange(bound) for name, bound in COUNTER_OFFSET_RANGES.items()}
        return cls(
            cart=FIRST_CART_NUMBER + offsets["cart"],
            order=FIRST_ORDER_ID + offsets["order"],
            merchant=FIRST_MERCHANT_NUMBER + offsets["merchant"],
            po=FIRST_PO_COUNTER + offsets["po"],
            shipment=FIRST_SHIPMENT_NUMBER + offsets["shipment"],
            row=FIRST_ROW_ID + offsets["row"],
        )


CLERK_SHARE = 0.3
CLERK_TYPO_RATE = 0.02
CLERK_START_DELAY = timedelta(minutes=20)

NOISE_PER_HOUR: dict[str, float] = {
    SRC_WEBSTORE: 40.0,
    SRC_ORDERS: 30.0,
    SRC_PAYMENTS: 6.0,
    SRC_WMS_EXPORT: 1.0,
    SRC_SHIP_LOG: 10.0,
}

F1_DAY, F1_START, F1_END = 6, time(10, 20), time(11, 0)
F2_DAY = 9
F3_DAY, F3_START, F3_END = 10, time(14, 0), time(16, 0)
F4_DAY = 12
F5_DAY, F5_START = 13, time(10, 0)
UPLOAD_FAILURES = 12
UPLOAD_FAILURE_FIRST, UPLOAD_FAILURE_LAST = time(21, 13), time(21, 19)

CSV_COLUMNS = (
    "id",
    "po_num",
    "order_ref",
    "status",
    "warehouse_code",
    "order_total",
    "order_date",
    "customer_name",
    "ship_to_address",
    "created_by",
    "created_at",
    "updated_at",
)
RENAMED_CSV_COLUMNS = tuple("po_number" if c == "po_num" else c for c in CSV_COLUMNS)
SHIP_FILE_COLUMNS = (
    "po_num",
    "order_ref",
    "warehouse_code",
    "carrier",
    "service_level",
    "weight_kg",
)

CREATE_TABLE = f"""CREATE TABLE {PO_TABLE} (
  id INTEGER PRIMARY KEY,
  po_num VARCHAR(16) NOT NULL,
  order_ref VARCHAR(16) NOT NULL,
  status VARCHAR(16) NOT NULL,
  warehouse_code VARCHAR(8) NOT NULL,
  order_total DECIMAL(12, 2) NOT NULL,
  order_date DATE NOT NULL,
  customer_name VARCHAR(128) NOT NULL,
  ship_to_address VARCHAR(256) NOT NULL,
  created_by VARCHAR(64) NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);"""
ALTER_RENAME = f"ALTER TABLE {PO_TABLE} RENAME COLUMN po_num TO po_number;"


# ---------------------------------------------------------------------------------------------
# In-memory records
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class PaymentAttempt:
    requested_at: datetime
    responded_at: datetime
    failure_logged_at: datetime | None  # None when the attempt was authorized


@dataclass(slots=True)
class PurchaseOrder:
    txn_id: str
    order_id: int
    order_ref: str
    warehouse_code: str
    total: str
    order_date: date
    person: Person
    created_by: str
    human: bool
    created_at: datetime
    typo: bool
    stalled: bool
    picked_at: datetime | None
    row_id: int = 0
    po_num: str = ""
    renamed: bool = False
    status: str = "CREATED"
    updated_at: datetime | None = None
    file_name: str | None = None
    carrier: str = ""
    service: str = ""
    weight: str = ""
    shipped_at: datetime | None = None
    shipment_no: str = ""


@dataclass(slots=True)
class Transaction:
    txn_id: str
    cart_id: str
    order_id: int
    merchant_ref: str
    cart_at: datetime
    checkout_at: datetime
    channel: str
    region: str
    items: int
    total: str
    payment_method: str
    person: Person
    checkout_pii: bool
    cardholder_pii: bool
    order_created_at: datetime
    attempts: list[PaymentAttempt]
    released_at: datetime
    po: PurchaseOrder | None


@dataclass(slots=True)
class DayParams:
    """Per-day draws for the nightly export, file arrival and carrier pickup."""

    export_offset: timedelta
    upload_delay: timedelta
    arrival_delay: timedelta
    pickup_offset: timedelta


@dataclass(slots=True)
class ExportedFile:
    name: str
    batch_id: str
    arrived_at: datetime
    txn_ids: list[str]


@dataclass(slots=True)
class FaultState:
    """Active fault windows in UTC (None when the fault is off or its day is outside the run).

    ``f2_expected_by`` is set only once the failed export has actually run: a day 9 without a
    due PO leaves no evidence, so it produces no FaultTruth.
    """

    f1: tuple[datetime, datetime] | None = None
    f2_day: date | None = None
    f2_expected_by: datetime | None = None
    f2_arrival: datetime | None = None
    f2_affected: list[str] = field(default_factory=list)
    f3: tuple[datetime, datetime] | None = None
    f3_affected: set[str] = field(default_factory=set)
    f4_start: datetime | None = None
    f5_start: datetime | None = None

    def f2_day_after(self) -> date | None:
        """The day whose file carries day 9's POs (None when F2 is off)."""
        return None if self.f2_day is None else self.f2_day + timedelta(days=1)


# ---------------------------------------------------------------------------------------------
# Draw helpers
# ---------------------------------------------------------------------------------------------


def draw_ms(rng: random.Random, low_seconds: float, high_seconds: float) -> timedelta:
    """Uniform delay between two bounds in seconds, millisecond resolution."""
    return timedelta(milliseconds=rng.randint(int(low_seconds * 1000), int(high_seconds * 1000)))


def draw_seconds(rng: random.Random, low: int, high: int) -> timedelta:
    return timedelta(seconds=rng.randint(low, high))


def poisson(rng: random.Random, mean: float) -> int:
    """Knuth's method; means stay below 400 (noise rate 10 on the busiest source)."""
    if mean <= 0.0:
        return 0
    limit = math.exp(-mean)
    count = 0
    product = 1.0
    while True:
        product *= rng.random()
        if product <= limit:
            return count
        count += 1


def apply_typo(rng: random.Random, order_ref: str) -> str:
    """One digit replaced, or two adjacent differing digits swapped, inside the digit run."""
    prefix, digits = order_ref[:3], list(order_ref[3:])
    swappable = [i for i in range(len(digits) - 1) if digits[i] != digits[i + 1]]
    if swappable and rng.random() < 0.5:
        i = rng.choice(swappable)
        digits[i], digits[i + 1] = digits[i + 1], digits[i]
    else:
        i = rng.randrange(len(digits))
        digits[i] = rng.choice([d for d in "0123456789" if d != digits[i]])
    return prefix + "".join(digits)


def po_number(counter: int) -> str:
    """``88210`` becomes ``88-210``; the prefix grows to three digits past 99999."""
    return f"{counter // 1000:02d}-{counter % 1000:03d}"
