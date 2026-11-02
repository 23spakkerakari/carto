"""Scenario C constants and in-memory records shared by the generator and its ground truth.

Systems, sources and node names are the vocabulary the eval harness sees in ``sources.json`` and
``event_txn.ndjson`` (spec 19 scenario C, plan M2 package H). Records are slotted dataclasses
because the generator creates one per emitted line; pydantic models are built only when ground
truth is written.

Volumes: ``daily_volume`` is the shop's orders per weekday (800 by default). Here it is a scale:
``ceil(daily_volume / 20)`` main hires and ``ceil(daily_volume / 80)`` legacy hires start per
weekday before the week-start weights (:data:`WEEKDAY_WEIGHTS`), and ``ceil(daily_volume / 160)``
SKUs are added to the product sheet per weekday. The default therefore gives 40 main and 10
legacy hires per weekday on average (56, 48, 40, 32, 24 from Monday to Friday; 14, 12, 10, 8, 6
legacy) and 25 SKUs per week, the figures of the founder-confirmed table.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from carto_simulator.ground_truth import SourceDef, SourceFormat, SourceKind
from carto_simulator.names import Person

# ---------------------------------------------------------------------------------------------
# Systems, sources, nodes, entities
# ---------------------------------------------------------------------------------------------

SYS_ONBOARDING = "sys_onboarding"
SYS_LEGACY = "sys_onboarding_legacy"
SYS_HRIS = "sys_hris"
SYS_PAYROLL = "sys_payroll"
SYS_LMS = "sys_lms"
SYS_SKU_SHEETS = "sys_sku_sheets"
SYS_SKU_MASTER = "sys_sku_master"

SRC_ONBOARDING = "src_onboarding_log"
SRC_LEGACY = "src_legacy_log"
SRC_HRIS = "src_hris_db"
SRC_PAYROLL = "src_payroll_csv"
SRC_LMS = "src_lms_log"
SRC_SKU_EXPORT = "src_sku_export_csv"
SRC_SKU_MASTER = "src_sku_master_db"

HRIS_TABLE = "employees"
PAYROLL_TABLE = "workers"
SHEET_TABLE = "sku_sheet"
MASTER_TABLE = "sku_master"

LEGACY_ZONE_KEY = "Europe/London"

N_APPLICATION = "onboarding:application_received"
N_OFFER = "onboarding:offer_accepted"
N_ONBOARDED = "onboarding:onboarding_started"
N_ONBOARDING_NOISE = "onboarding:noise"
N_LEGACY_APPLICATION = "onboarding_legacy:application_logged"
N_LEGACY_OFFER = "onboarding_legacy:offer_signed"
N_LEGACY_FIRST_DAY = "onboarding_legacy:first_day"
N_LEGACY_NOISE = "onboarding_legacy:noise"
N_EMPLOYEE_CREATED = "hris:employee_created"
N_WORKER_ENTERED = "payroll:worker_entered"
N_ENROLLED = "lms:enrolled"
N_LMS_NOISE = "lms:noise"
N_SKU_ADDED = "sku_sheets:sku_added"
N_SKU_CREATED = "sku_master:sku_created"

ENT_EMPLOYEE = "ent_employee"
ENT_SKU = "ent_sku"

LOCAL_FORMAT = "%Y-%m-%d %H:%M:%S"

SOURCES: tuple[SourceDef, ...] = (
    SourceDef(
        source_id=SRC_ONBOARDING,
        system_id=SYS_ONBOARDING,
        system_name="Onboarding",
        kind=SourceKind.LOG_FILE,
        format=SourceFormat.NDJSON,
        path="onboarding",
        timezone="UTC",
        timestamp_format="iso8601",
        notes="onboarding-YYYY-MM-DD.ndjson, one file per UTC day; timestamp key 'ts'.",
    ),
    SourceDef(
        source_id=SRC_LEGACY,
        system_id=SYS_LEGACY,
        system_name="Onboarding (legacy)",
        kind=SourceKind.LOG_FILE,
        format=SourceFormat.LOGFMT,
        path="onboarding_legacy",
        timezone=LEGACY_ZONE_KEY,
        timestamp_format=LOCAL_FORMAT,
        notes=(
            "legacy-hires-YYYY-MM-DD.log, one file per London day; timestamp key 'time' in "
            "naive Europe/London time; startDt is %d/%m/%Y."
        ),
    ),
    SourceDef(
        source_id=SRC_HRIS,
        system_id=SYS_HRIS,
        system_name="HRIS",
        kind=SourceKind.SQL_TABLE,
        format=SourceFormat.SQL_ROWS,
        path="hris",
        timezone="America/New_York",
        timestamp_format=LOCAL_FORMAT,
        notes="Table employees: employees.sql seed plus a CSV export of the rows.",
    ),
    SourceDef(
        source_id=SRC_PAYROLL,
        system_id=SYS_PAYROLL,
        system_name="Payroll",
        kind=SourceKind.SQL_TABLE,
        format=SourceFormat.CSV,
        path="payroll",
        timezone="America/New_York",
        timestamp_format=LOCAL_FORMAT,
        notes=(
            "payroll_YYYYMMDD.csv, one export per weekday with the worker rows entered that day; "
            "read as rows keyed by worker_number."
        ),
    ),
    SourceDef(
        source_id=SRC_LMS,
        system_id=SYS_LMS,
        system_name="Training LMS",
        kind=SourceKind.LOG_FILE,
        format=SourceFormat.NDJSON,
        path="lms",
        timezone="UTC",
        timestamp_format="iso8601",
        notes="lms-YYYY-MM-DD.ndjson, one file per UTC day; timestamp key 'ts'.",
    ),
    SourceDef(
        source_id=SRC_SKU_EXPORT,
        system_id=SYS_SKU_SHEETS,
        system_name="Product sheets",
        kind=SourceKind.SQL_TABLE,
        format=SourceFormat.CSV,
        path="sku_sheets",
        timezone="America/New_York",
        timestamp_format=LOCAL_FORMAT,
        notes=(
            "sku_sheet_YYYYMMDD.csv, the spreadsheet exported every Friday with the week's new "
            "SKUs; read as rows keyed by legacy_sku."
        ),
    ),
    SourceDef(
        source_id=SRC_SKU_MASTER,
        system_id=SYS_SKU_MASTER,
        system_name="SKU master",
        kind=SourceKind.SQL_TABLE,
        format=SourceFormat.SQL_ROWS,
        path="sku_master",
        timezone="America/New_York",
        timestamp_format=LOCAL_FORMAT,
        notes="Table sku_master: sku_master.sql seed plus a CSV export of the rows.",
    ),
)

# ---------------------------------------------------------------------------------------------
# Scenario parameters
# ---------------------------------------------------------------------------------------------

WEEKDAY_WEIGHTS: tuple[float, ...] = (1.4, 1.2, 1.0, 0.8, 0.6)
"""Start-date weights Monday to Friday; they sum to five so the weekly total is five times the
per-weekday base. New hires start at the beginning of the week."""

MAIN_DIVISOR = 20
LEGACY_DIVISOR = 80
SKU_DIVISOR = 160

DEPARTMENTS = ("Operations", "Warehouse", "Fleet", "Finance", "Customer Service")
CHANNELS = ("job_board", "referral", "agency")
LEGACY_SITES = ("LDN", "MAN", "BHX")
PAY_GROUPS = ("weekly", "biweekly", "monthly")
COURSES = ("safety-induction", "code-of-conduct", "forklift-basics")
SKU_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Packaging", "PKG"),
    ("Strapping", "STR"),
    ("Labels", "LBL"),
    ("Safety", "SAF"),
)
SKU_COLOURS = ("Blue", "Red", "Grey", "Black", "Green", "Yellow", "White", "Orange")
SKU_NOUNS = (
    "pallet wrap",
    "crate liner",
    "label roll",
    "tote",
    "divider",
    "strap",
    "corner guard",
    "stretch film",
    "edge protector",
    "bin tag",
    "hand tape",
    "dock mat",
)
SKU_UNITS = ("m", "cm", "mm", "pcs", "kg")

FIRST_CANDIDATE_NUMBER = 1000481
FIRST_LEGACY_NUMBER = 204117
FIRST_EMPLOYEE_NUMBER = 40210
FIRST_WORKER_NUMBER = 300118
FIRST_LEGACY_SKU = 500100
FIRST_SKU_COUNTER = 10240

HR_CLERKS: tuple[str, ...] = ("hr_asante", "hr_bergstrom", "hr_castillo", "hr_dlamini")
PAYROLL_CLERKS: tuple[str, ...] = ("pay_ekwueme", "pay_farrow", "pay_gallo")
PRODUCT_CLERKS: tuple[str, ...] = ("prd_haines", "prd_ibarra")
LMS_SERVICE_ACCOUNT = "svc_lms_provisioner"

SOURCE_REF_SHARE = 0.6
HRIS_NAME_TYPO_RATE = 0.03
PAYROLL_REF_TYPO_RATE = 0.02
PAYROLL_NAME_TYPO_RATE = 0.03
MASTER_DESCRIPTION_TYPO_RATE = 0.03
PAYROLL_DROP_RATE = 0.01

BACKLOG_FIRST_DAY, BACKLOG_LAST_DAY, BACKLOG_LANDING_DAY = 13, 15, 17
"""Run days (1-based) of the week-3 payroll backlog: entries due on days 13 to 15 are keyed on
day 17 (or the next business day), two days after the last of them."""

BUSINESS_MINUTES = 9 * 60 + 59
"""Clerk work lands at 08:00 + U(0, this) minutes local, inside Mon to Fri 08:00 to 18:00."""

NOISE_PER_HOUR: dict[str, float] = {SRC_ONBOARDING: 20.0, SRC_LEGACY: 4.0, SRC_LMS: 10.0}

HRIS_COLUMNS = (
    "id",
    "employee_id",
    "full_name",
    "start_date",
    "source_ref",
    "department",
    "created_by",
    "created_at",
)
PAYROLL_COLUMNS = (
    "worker_number",
    "employee_ref",
    "legal_name",
    "start_date",
    "pay_group",
    "entered_by",
    "entered_at",
)
SHEET_COLUMNS = ("legacy_sku", "description", "unit_price", "category", "added_at")
MASTER_COLUMNS = ("id", "sku", "description", "unit_price", "created_by", "created_at")

CREATE_HRIS_TABLE = f"""CREATE TABLE {HRIS_TABLE} (
  id INTEGER PRIMARY KEY,
  employee_id VARCHAR(16) NOT NULL,
  full_name VARCHAR(128) NOT NULL,
  start_date DATE NOT NULL,
  source_ref VARCHAR(16) NOT NULL,
  department VARCHAR(32) NOT NULL,
  created_by VARCHAR(64) NOT NULL,
  created_at TIMESTAMP NOT NULL
);"""
CREATE_MASTER_TABLE = f"""CREATE TABLE {MASTER_TABLE} (
  id INTEGER PRIMARY KEY,
  sku VARCHAR(16) NOT NULL,
  description VARCHAR(128) NOT NULL,
  unit_price DECIMAL(12, 2) NOT NULL,
  created_by VARCHAR(64) NOT NULL,
  created_at TIMESTAMP NOT NULL
);"""


# ---------------------------------------------------------------------------------------------
# In-memory records
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class HrisRow:
    txn_id: str
    full_name: str
    start_date: date
    source_ref: str
    department: str
    created_by: str
    created_at: datetime
    name_typo: bool
    row_id: int = 0
    employee_id: str = ""


@dataclass(slots=True)
class PayrollRow:
    txn_id: str
    employee_ref: str
    legal_name: str
    start_date: date
    pay_group: str
    entered_by: str
    entered_at: datetime
    ref_typo: bool
    name_typo: bool
    backlog: bool
    worker_number: str = ""


@dataclass(slots=True)
class Hire:
    """One transaction: a hire through onboarding (main or legacy), HRIS, payroll and the LMS."""

    txn_id: str
    legacy: bool
    candidate_id: str
    person: Person
    username: str
    department: str
    channel: str
    site: str
    applied_at: datetime
    application_pii: bool
    offered_at: datetime
    start_date: date
    started_at: datetime
    enrolled_at: datetime
    course: str
    hris: HrisRow | None
    payroll: PayrollRow | None
    dropped: bool


@dataclass(slots=True)
class SheetRow:
    txn_id: str
    legacy_sku: str
    description: str
    unit_price: str
    category: str
    category_code: str
    added_at: datetime
    export_at: datetime
    file_name: str
    master: MasterRow | None


@dataclass(slots=True)
class MasterRow:
    txn_id: str
    description: str
    unit_price: str
    category_code: str
    created_by: str
    created_at: datetime
    typo: bool
    row_id: int = 0
    sku: str = ""


@dataclass(slots=True)
class FaultState:
    """What the faults did in this run (empty when faults are off or nothing was affected)."""

    backlog_start: datetime | None = None
    backlog_end: datetime | None = None
    backlog_affected: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Draw helpers
# ---------------------------------------------------------------------------------------------


def per_weekday(daily_volume: int, divisor: int) -> int:
    """The per-weekday base for a flow: ``ceil(daily_volume / divisor)``, at least one."""
    return max(1, math.ceil(daily_volume / divisor))


def weekday_count(base: int, day: date) -> int:
    """Hires starting on ``day``: the base times the weekday weight; none at the weekend."""
    if day.weekday() >= 5:
        return 0
    return round(base * WEEKDAY_WEIGHTS[day.weekday()])


def draw_minutes(rng: random.Random, low: int, high: int) -> timedelta:
    return timedelta(minutes=rng.randint(low, high))


def draw_seconds(rng: random.Random, low: int, high: int) -> timedelta:
    return timedelta(seconds=rng.randint(low, high))


def poisson(rng: random.Random, mean: float) -> int:
    """Knuth's method; means stay below 200 (noise rate 10 on the busiest source)."""
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


def apply_digit_typo(rng: random.Random, value: str, prefix_len: int) -> str:
    """One digit replaced, or two adjacent differing digits swapped, after ``prefix_len``."""
    prefix, digits = value[:prefix_len], list(value[prefix_len:])
    swappable = [i for i in range(len(digits) - 1) if digits[i] != digits[i + 1]]
    if swappable and rng.random() < 0.5:
        i = rng.choice(swappable)
        digits[i], digits[i + 1] = digits[i + 1], digits[i]
    else:
        i = rng.randrange(len(digits))
        digits[i] = rng.choice([d for d in "0123456789" if d != digits[i]])
    return prefix + "".join(digits)


def apply_text_typo(rng: random.Random, text: str) -> str:
    """A keying slip inside one word: two adjacent letters swapped, or one letter doubled.

    Spaces and punctuation are kept, so the token count and the word order survive; the
    ``alnum`` form changes and the phonetic form may or may not (spec 9.5, 11.2).
    """
    letters = [i for i, ch in enumerate(text) if ch.isalpha()]
    swappable = [i for i in letters if i + 1 < len(text) and text[i + 1].isalpha()]
    swappable = [i for i in swappable if text[i].lower() != text[i + 1].lower()]
    if swappable and rng.random() < 0.6:
        i = rng.choice(swappable)
        return text[:i] + text[i + 1] + text[i] + text[i + 2 :]
    lowercase = [i for i in letters if text[i].islower()] or letters
    i = rng.choice(lowercase)
    return text[: i + 1] + text[i] + text[i + 1 :]


def sku_code(prefix: str, counter: int) -> str:
    """``PKG-10240``: the category code plus a five-digit counter (shape ``AAA-99999``)."""
    return f"{prefix}-{counter:05d}"
