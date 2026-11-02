"""Scenario C, "people_ops" (spec 19): onboarding (main and legacy), HRIS, payroll, LMS, SKUs.

One transaction is one hire. It is applied for, offered and started in one of two onboarding
systems (the main one logs JSON; the acquired company's legacy one logs logfmt with other field
names, ``%d/%m/%Y`` dates and naive Europe/London time), typed into the HRIS by HR clerks one to
two business days after the start from an onboarding export (``source_ref`` carries the candidate
id for 60% of rows; the rest are the composite path of spec 9.5: phonetic name plus start date),
keyed again by payroll clerks one to two business days later (``employee_ref`` with 2% typos),
and enrolled in the training LMS by a provisioning service minutes after the start, under a
username derived from the work email. A second, smaller flow adds SKUs to a product spreadsheet
whose weekly export a product clerk re-keys into the SKU master (composite on description plus
unit price, no shared identifier). Hops are mostly manual (spec 11.1); see ``simulator/README.md``
for every timing and the choices made where the spec is silent.

Faults (spec 19 scenario C): a payroll backlog in week 3 (entries due on run days 13 to 15 are
keyed on day 17, so three daily exports are missing and one file carries them all) and 1% of
hires that never reach payroll.

Generation is deterministic: every random draw comes from one ``random.Random(seed)`` consumed
in a fixed order (each hire's lifecycle in start-date order, main before legacy within a day;
then payroll references in the same order; then the SKU sheet rows and their master rows week
by week; then noise source by source). Nothing reads the wall clock.
"""

from __future__ import annotations

import hashlib
import random
import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from carto_simulator import clock
from carto_simulator.api import GenerationRequest, GenerationResult
from carto_simulator.formats import (
    csv_text,
    iso_utc_ms,
    logfmt_line,
    naive_local_seconds,
    ndjson_line,
    sql_insert,
    sql_string,
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
from carto_simulator.names import MarkerFactory, draw_person
from carto_simulator.scenarios import people_ops_truth
from carto_simulator.scenarios.people_ops_model import (
    BACKLOG_FIRST_DAY,
    BACKLOG_LANDING_DAY,
    BACKLOG_LAST_DAY,
    BUSINESS_MINUTES,
    CHANNELS,
    COURSES,
    CREATE_HRIS_TABLE,
    CREATE_MASTER_TABLE,
    DEPARTMENTS,
    FIRST_CANDIDATE_NUMBER,
    FIRST_EMPLOYEE_NUMBER,
    FIRST_LEGACY_NUMBER,
    FIRST_LEGACY_SKU,
    FIRST_SKU_COUNTER,
    FIRST_WORKER_NUMBER,
    HR_CLERKS,
    HRIS_COLUMNS,
    HRIS_NAME_TYPO_RATE,
    HRIS_TABLE,
    LEGACY_DIVISOR,
    LEGACY_SITES,
    LEGACY_ZONE_KEY,
    LMS_SERVICE_ACCOUNT,
    MAIN_DIVISOR,
    MASTER_COLUMNS,
    MASTER_DESCRIPTION_TYPO_RATE,
    MASTER_TABLE,
    N_APPLICATION,
    N_EMPLOYEE_CREATED,
    N_ENROLLED,
    N_LEGACY_APPLICATION,
    N_LEGACY_FIRST_DAY,
    N_LEGACY_NOISE,
    N_LEGACY_OFFER,
    N_LMS_NOISE,
    N_OFFER,
    N_ONBOARDED,
    N_ONBOARDING_NOISE,
    N_SKU_ADDED,
    N_SKU_CREATED,
    N_WORKER_ENTERED,
    NOISE_PER_HOUR,
    PAY_GROUPS,
    PAYROLL_CLERKS,
    PAYROLL_COLUMNS,
    PAYROLL_DROP_RATE,
    PAYROLL_NAME_TYPO_RATE,
    PAYROLL_REF_TYPO_RATE,
    PAYROLL_TABLE,
    PRODUCT_CLERKS,
    SHEET_COLUMNS,
    SHEET_TABLE,
    SKU_CATEGORIES,
    SKU_COLOURS,
    SKU_DIVISOR,
    SKU_NOUNS,
    SKU_UNITS,
    SOURCE_REF_SHARE,
    SOURCES,
    SRC_HRIS,
    SRC_LEGACY,
    SRC_LMS,
    SRC_ONBOARDING,
    SRC_PAYROLL,
    SRC_SKU_EXPORT,
    SRC_SKU_MASTER,
    SYS_HRIS,
    SYS_LEGACY,
    SYS_LMS,
    SYS_ONBOARDING,
    SYS_PAYROLL,
    SYS_SKU_MASTER,
    SYS_SKU_SHEETS,
    FaultState,
    Hire,
    HrisRow,
    MasterRow,
    PayrollRow,
    SheetRow,
    apply_digit_typo,
    apply_text_typo,
    draw_minutes,
    draw_seconds,
    per_weekday,
    poisson,
    sku_code,
    weekday_count,
)
from carto_simulator.writers import Emitted, LogSink, Truth

SERVICE = ActorKind.SERVICE
HUMAN = ActorKind.HUMAN
SHEET_EXPORT_TIME = time(17, 0)


class _PeopleOpsRun:
    """State of one generation; a fresh instance per :meth:`PeopleOpsScenario.generate`."""

    def __init__(self, request: GenerationRequest, out_dir: Path) -> None:
        self.request = request
        self.out_dir = out_dir
        self.rng = random.Random(request.seed)
        self.zone = clock.local_zone()
        self.legacy_zone = ZoneInfo(LEGACY_ZONE_KEY)
        self.start_day = request.start_date
        self.end_day = request.start_date + timedelta(days=request.days)
        self.window_start = clock.local_midnight_utc(self.start_day, self.zone)
        self.window_end = clock.local_midnight_utc(self.end_day, self.zone)
        self.markers = MarkerFactory(self.rng)
        self.faults = FaultState()
        self.hires: list[Hire] = []
        self.hris_rows: list[HrisRow] = []
        self.payroll_rows: list[PayrollRow] = []
        self.sheet_rows: list[SheetRow] = []
        self.master_rows: list[MasterRow] = []
        self.identifier_values: dict[str, set[str]] = {}
        self.pii_values: set[str] = set()
        self.marker_tokens: set[str] = set()
        self.actor_values: set[str] = set()
        self.emitted: list[Emitted] = []
        legacy = self.legacy_zone
        self.onboarding = LogSink(
            SRC_ONBOARDING,
            SYS_ONBOARDING,
            "onboarding",
            lambda t: f"onboarding-{t:%Y-%m-%d}.ndjson",
            UTC,
            iso_utc_ms,
        )
        self.legacy = LogSink(
            SRC_LEGACY,
            SYS_LEGACY,
            "onboarding_legacy",
            lambda t: f"legacy-hires-{t:%Y-%m-%d}.log",
            legacy,
            lambda t: naive_local_seconds(t.astimezone(legacy)),
        )
        self.lms = LogSink(
            SRC_LMS, SYS_LMS, "lms", lambda t: f"lms-{t:%Y-%m-%d}.ndjson", UTC, iso_utc_ms
        )

    # -- helpers ------------------------------------------------------------------------------

    def day(self, number: int) -> date:
        """Calendar date of 1-based day ``number`` of the run."""
        return self.start_day + timedelta(days=number - 1)

    def day_number(self, day: date) -> int:
        return (day - self.start_day).days + 1

    def local(self, day: date, at: time) -> datetime:
        """UTC instant of a local wall-clock time on ``day``."""
        return clock.to_utc(clock.local_wall(day, at, self.zone))

    def in_window(self, instant: datetime) -> bool:
        return self.window_start <= instant < self.window_end

    def business_instant(self, day: date) -> datetime:
        """A clerk's moment on business day ``day``: 08:00 + U(0, 9 h 59 min) + U(0, 59) s local."""
        return (
            clock.business_open(day)
            + draw_minutes(self.rng, 0, BUSINESS_MINUTES)
            + draw_seconds(self.rng, 0, 59)
        )

    @staticmethod
    def business_days_after(day: date, count: int) -> date:
        for _ in range(count):
            day = clock.next_business_day(day)
        return day

    @staticmethod
    def previous_business_day(day: date) -> date:
        while not clock.is_business_day(day):
            day -= timedelta(days=1)
        return day

    def record_identifier(self, system_id: str, source_id: str, field: str, value: str) -> None:
        """Remember an emitted identifier value under ``str(FieldRef)`` (spec 18.3 markers)."""
        key = f"{system_id}/{source_id}/{field}"  # the same shape as str(FieldRef(...))
        values = self.identifier_values.get(key)
        if values is None:
            values = self.identifier_values[key] = set()
        values.add(value)

    def record_person(self, name: str, email: str, marker: str) -> None:
        self.pii_values.update((name, email))
        self.marker_tokens.add(marker)

    # -- phase A: hires -----------------------------------------------------------------------

    def build_hires(self) -> None:
        """Hires in start-date order: main ones, then legacy ones, per weekday of the run."""
        main_base = per_weekday(self.request.daily_volume, MAIN_DIVISOR)
        legacy_base = per_weekday(self.request.daily_volume, LEGACY_DIVISOR)
        index = 0
        for offset in range(self.request.days):
            day = self.start_day + timedelta(days=offset)
            for legacy, base in ((False, main_base), (True, legacy_base)):
                for _ in range(weekday_count(base, day)):
                    index += 1
                    self.hires.append(self.build_hire(index, day, legacy))

    def build_hire(self, index: int, start_day: date, legacy: bool) -> Hire:
        rng = self.rng
        zone = self.legacy_zone if legacy else self.zone
        person = draw_person(rng, self.markers)
        username = person.email.partition("@")[0].replace("+", ".")
        department = rng.choice(DEPARTMENTS)
        channel = rng.choice(CHANNELS)
        site = rng.choice(LEGACY_SITES)
        offer_day = self.previous_business_day(start_day - timedelta(days=rng.randint(3, 10)))
        offered_at = clock.to_utc(clock.local_wall(offer_day, time(8), zone)) + draw_minutes(
            rng, 0, BUSINESS_MINUTES
        )
        applied_day = offer_day - timedelta(days=rng.randint(2, 10))
        applied_at = clock.to_utc(
            clock.local_wall(
                applied_day, time(rng.randint(7, 22), rng.randint(0, 59), rng.randint(0, 59)), zone
            )
        )
        application_pii = rng.random() < self.request.pii_density
        if legacy:
            started_at = clock.to_utc(clock.local_wall(start_day, time(9), zone)) + draw_minutes(
                rng, 0, 60
            )
        else:
            started_at = self.local(start_day, time(8)) + draw_minutes(rng, 0, 90)
        started_at += draw_seconds(rng, 0, 59)
        enrolled_at = started_at + draw_seconds(rng, 120, 1800)
        course = rng.choice(COURSES)
        txn_id = f"txn_{index:06d}"
        if legacy:
            candidate_id = f"L{FIRST_LEGACY_NUMBER + index - 1}"
        else:
            candidate_id = f"C-{FIRST_CANDIDATE_NUMBER + index - 1}"
        hris = self.build_hris_row(txn_id, person.name, start_day, candidate_id, department)
        payroll, dropped = self.build_payroll_row(txn_id, hris)
        return Hire(
            txn_id=txn_id,
            legacy=legacy,
            candidate_id=candidate_id,
            person=person,
            username=username,
            department=department,
            channel=channel,
            site=site,
            applied_at=applied_at,
            application_pii=application_pii,
            offered_at=offered_at,
            start_date=start_day,
            started_at=started_at,
            enrolled_at=enrolled_at,
            course=course,
            hris=hris,
            payroll=payroll,
            dropped=dropped,
        )

    def build_hris_row(
        self, txn_id: str, name: str, start_day: date, candidate_id: str, department: str
    ) -> HrisRow | None:
        """The HR clerk's row, 1 to 2 business days after the start, in business hours."""
        rng = self.rng
        created_day = self.business_days_after(start_day, rng.randint(1, 2))
        created_at = self.business_instant(created_day)
        created_by = rng.choice(HR_CLERKS)
        has_ref = rng.random() < SOURCE_REF_SHARE
        name_typo = rng.random() < HRIS_NAME_TYPO_RATE
        full_name = apply_text_typo(rng, name) if name_typo else name
        if not self.in_window(created_at):
            return None
        return HrisRow(
            txn_id=txn_id,
            full_name=full_name,
            start_date=start_day,
            source_ref=candidate_id if has_ref else "",
            department=department,
            created_by=created_by,
            created_at=created_at,
            name_typo=name_typo,
        )

    def build_payroll_row(
        self, txn_id: str, hris: HrisRow | None
    ) -> tuple[PayrollRow | None, bool]:
        """The payroll clerk's row, 1 to 2 business days after the HRIS row; the draws are made
        whether or not the HRIS row is inside the window, so the sequence never depends on it."""
        rng = self.rng
        dropped = self.request.faults and rng.random() < PAYROLL_DROP_RATE
        delay = rng.randint(1, 2)
        offset = timedelta(minutes=rng.randint(0, BUSINESS_MINUTES), seconds=rng.randint(0, 59))
        entered_by = rng.choice(PAYROLL_CLERKS)
        ref_typo = rng.random() < PAYROLL_REF_TYPO_RATE
        name_typo = rng.random() < PAYROLL_NAME_TYPO_RATE
        pay_group = rng.choice(PAY_GROUPS)
        if hris is None or dropped:
            return None, dropped
        created_day = clock.to_local(hris.created_at, self.zone).date()
        entered_day = self.business_days_after(created_day, delay)
        backlog = False
        if self.request.faults and (
            BACKLOG_FIRST_DAY <= self.day_number(entered_day) <= BACKLOG_LAST_DAY
        ):
            backlog = True
            landing = self.day(BACKLOG_LANDING_DAY)
            if not clock.is_business_day(landing):
                landing = clock.next_business_day(landing)
            entered_day = landing
        entered_at = clock.business_open(entered_day) + offset
        legal_name = apply_text_typo(rng, hris.full_name) if name_typo else hris.full_name
        if not self.in_window(entered_at):
            return None, dropped
        return PayrollRow(
            txn_id=txn_id,
            employee_ref="",
            legal_name=legal_name,
            start_date=hris.start_date,
            pay_group=pay_group,
            entered_by=entered_by,
            entered_at=entered_at,
            ref_typo=ref_typo,
            name_typo=name_typo,
            backlog=backlog,
        ), dropped

    def number_rows(self) -> None:
        """Row ids, employee ids and worker numbers follow creation order, as the systems would
        assign them; payroll references are keyed (with their typos) once employee ids exist."""
        rng = self.rng
        self.hris_rows = sorted(
            (h.hris for h in self.hires if h.hris is not None),
            key=lambda row: (row.created_at, row.txn_id),
        )
        for index, row in enumerate(self.hris_rows):
            row.row_id = index + 1
            row.employee_id = f"E-{FIRST_EMPLOYEE_NUMBER + index}"
        for hire in self.hires:
            if hire.payroll is None or hire.hris is None:
                continue
            employee_id = hire.hris.employee_id
            hire.payroll.employee_ref = (
                apply_digit_typo(rng, employee_id, 2) if hire.payroll.ref_typo else employee_id
            )
        self.payroll_rows = sorted(
            (h.payroll for h in self.hires if h.payroll is not None),
            key=lambda row: (row.entered_at, row.txn_id),
        )
        for index, payroll in enumerate(self.payroll_rows):
            payroll.worker_number = f"W-{FIRST_WORKER_NUMBER + index}"

    def record_faults(self) -> None:
        if not self.request.faults:
            return
        faults = self.faults
        faults.dropped = sorted(
            hire.txn_id for hire in self.hires if hire.dropped and hire.hris is not None
        )
        backlog = [row for row in self.payroll_rows if row.backlog]
        if backlog:
            faults.backlog_start = clock.business_open(self.day(BACKLOG_FIRST_DAY))
            faults.backlog_end = min(row.entered_at for row in backlog)
            faults.backlog_affected = sorted(row.txn_id for row in backlog)

    # -- phase B: SKUs ------------------------------------------------------------------------

    def build_skus(self) -> None:
        """Sheet rows per weekday, exported on Friday 17:00 local; the master rows follow."""
        base = per_weekday(self.request.daily_volume, SKU_DIVISOR)
        counter = 0
        pending: list[SheetRow] = []
        for offset in range(self.request.days):
            day = self.start_day + timedelta(days=offset)
            if clock.is_business_day(day):
                for _ in range(base):
                    counter += 1
                    pending.append(self.build_sheet_row(counter, day))
            if day.weekday() == 4:
                self.export_sheet(day, pending)
                pending = []
        # Rows added in a week whose Friday export lies past the window are never exported.
        self.master_rows = sorted(
            (row.master for row in self.sheet_rows if row.master is not None),
            key=lambda row: (row.created_at, row.txn_id),
        )
        for index, row in enumerate(self.master_rows):
            row.row_id = index + 1
            row.sku = sku_code(row.category_code, FIRST_SKU_COUNTER + index)

    def build_sheet_row(self, counter: int, day: date) -> SheetRow:
        rng = self.rng
        added_at = (
            clock.business_open(day) + draw_minutes(rng, 0, 8 * 60 - 1) + draw_seconds(rng, 0, 59)
        )
        category, code = rng.choice(SKU_CATEGORIES)
        colour = rng.choice(SKU_COLOURS)
        noun = rng.choice(SKU_NOUNS)
        size = f"{rng.randint(2, 900)} {rng.choice(SKU_UNITS)}"
        description = f"{colour} {noun} {size}"
        unit_price = f"{round(rng.uniform(0.5, 250.0), 2):.2f}"
        return SheetRow(
            txn_id=f"sku_{counter:06d}",
            legacy_sku=str(FIRST_LEGACY_SKU + counter - 1),
            description=description,
            unit_price=unit_price,
            category=category,
            category_code=code,
            added_at=added_at,
            export_at=added_at,
            file_name="",
            master=None,
        )

    def export_sheet(self, friday: date, rows: list[SheetRow]) -> None:
        """The Friday export lists the week's rows; a product clerk keys each into the master
        1 to 2 business days later with 3% description typos (the price is copied exactly)."""
        rng = self.rng
        export_at = self.local(friday, SHEET_EXPORT_TIME)
        if not rows or not self.in_window(export_at):
            return
        file_name = f"sku_sheet_{friday:%Y%m%d}.csv"
        for row in rows:
            row.export_at = export_at
            row.file_name = file_name
            keyed_day = self.business_days_after(friday, rng.randint(1, 2))
            created_at = self.business_instant(keyed_day)
            created_by = rng.choice(PRODUCT_CLERKS)
            typo = rng.random() < MASTER_DESCRIPTION_TYPO_RATE
            description = apply_text_typo(rng, row.description) if typo else row.description
            if self.in_window(created_at):
                row.master = MasterRow(
                    txn_id=row.txn_id,
                    description=description,
                    unit_price=row.unit_price,
                    category_code=row.category_code,
                    created_by=created_by,
                    created_at=created_at,
                    typo=typo,
                )
            self.sheet_rows.append(row)

    # -- phase C: emitting the per-hire lines ---------------------------------------------------

    def emit_hire(self, hire: Hire) -> None:
        if hire.legacy:
            self.emit_legacy_hire(hire)
        else:
            self.emit_main_hire(hire)
        if self.in_window(hire.enrolled_at):
            self.lms.add(
                Truth(N_ENROLLED, hire.enrolled_at, hire.txn_id, None, SERVICE),
                ndjson_line(
                    {
                        "ts": self.lms.timestamp(hire.enrolled_at),
                        "level": "info",
                        "msg": "enrollment created",
                        "username": hire.username,
                        "user_email": hire.person.email,
                        "course": hire.course,
                        "actor": LMS_SERVICE_ACCOUNT,
                    }
                ),
            )
            self.record_identifier(SYS_LMS, SRC_LMS, "username", hire.username)
            self.pii_values.update((hire.username, hire.person.email))
            self.marker_tokens.add(hire.person.email_marker)

    def emit_main_hire(self, hire: Hire) -> None:
        person = hire.person
        if self.in_window(hire.applied_at):
            fields: dict[str, object] = {
                "ts": self.onboarding.timestamp(hire.applied_at),
                "level": "info",
                "msg": "application received",
                "candidate_id": hire.candidate_id,
                "channel": hire.channel,
            }
            if hire.application_pii:
                fields["candidate_name"] = person.name
                self.pii_values.add(person.name)
            self.onboarding.add(
                Truth(N_APPLICATION, hire.applied_at, hire.txn_id, None, SERVICE),
                ndjson_line(fields),
            )
            self.record_identifier(
                SYS_ONBOARDING, SRC_ONBOARDING, "candidate_id", hire.candidate_id
            )
        if self.in_window(hire.offered_at):
            self.onboarding.add(
                Truth(N_OFFER, hire.offered_at, hire.txn_id, None, SERVICE),
                ndjson_line(
                    {
                        "ts": self.onboarding.timestamp(hire.offered_at),
                        "level": "info",
                        "msg": "offer accepted",
                        "candidate_id": hire.candidate_id,
                        "candidate_name": person.name,
                        "start_date": hire.start_date.isoformat(),
                        "department": hire.department,
                    }
                ),
            )
            self.record_identifier(
                SYS_ONBOARDING, SRC_ONBOARDING, "candidate_id", hire.candidate_id
            )
            self.pii_values.add(person.name)
        if self.in_window(hire.started_at):
            self.onboarding.add(
                Truth(N_ONBOARDED, hire.started_at, hire.txn_id, None, SERVICE),
                ndjson_line(
                    {
                        "ts": self.onboarding.timestamp(hire.started_at),
                        "level": "info",
                        "msg": "onboarding started",
                        "candidate_id": hire.candidate_id,
                        "candidate_name": person.name,
                        "work_email": person.email,
                        "start_date": hire.start_date.isoformat(),
                        "department": hire.department,
                    }
                ),
            )
            self.record_identifier(
                SYS_ONBOARDING, SRC_ONBOARDING, "candidate_id", hire.candidate_id
            )
            self.record_person(person.name, person.email, person.email_marker)

    def emit_legacy_hire(self, hire: Hire) -> None:
        person = hire.person
        start = f"{hire.start_date:%d/%m/%Y}"
        if self.in_window(hire.applied_at):
            pairs: list[tuple[str, str | int]] = [
                ("time", self.legacy.timestamp(hire.applied_at)),
                ("lvl", "INFO"),
                ("msg", "application logged"),
                ("cand_ref", hire.candidate_id),
                ("site", hire.site),
            ]
            if hire.application_pii:
                pairs.append(("applicant", person.name))
                self.pii_values.add(person.name)
            self.legacy.add(
                Truth(N_LEGACY_APPLICATION, hire.applied_at, hire.txn_id, None, SERVICE),
                logfmt_line(pairs),
            )
            self.record_identifier(SYS_LEGACY, SRC_LEGACY, "cand_ref", hire.candidate_id)
        if self.in_window(hire.offered_at):
            self.legacy.add(
                Truth(N_LEGACY_OFFER, hire.offered_at, hire.txn_id, None, SERVICE),
                logfmt_line(
                    [
                        ("time", self.legacy.timestamp(hire.offered_at)),
                        ("lvl", "INFO"),
                        ("msg", "offer signed"),
                        ("cand_ref", hire.candidate_id),
                        ("applicant", person.name),
                        ("startDt", start),
                        ("site", hire.site),
                    ]
                ),
            )
            self.record_identifier(SYS_LEGACY, SRC_LEGACY, "cand_ref", hire.candidate_id)
            self.pii_values.add(person.name)
        if self.in_window(hire.started_at):
            self.legacy.add(
                Truth(N_LEGACY_FIRST_DAY, hire.started_at, hire.txn_id, None, SERVICE),
                logfmt_line(
                    [
                        ("time", self.legacy.timestamp(hire.started_at)),
                        ("lvl", "INFO"),
                        ("msg", "first day"),
                        ("cand_ref", hire.candidate_id),
                        ("applicant", person.name),
                        ("mail", person.email),
                        ("startDt", start),
                        ("site", hire.site),
                    ]
                ),
            )
            self.record_identifier(SYS_LEGACY, SRC_LEGACY, "cand_ref", hire.candidate_id)
            self.record_person(person.name, person.email, person.email_marker)

    # -- phase D: noise -----------------------------------------------------------------------

    def emit_noise(self) -> None:
        """Poisson count per source and hour (expectation scaled by noise_rate), uniform within."""
        rng = self.rng
        rate = self.request.noise_rate
        hour = timedelta(hours=1)
        for source_id in (SRC_ONBOARDING, SRC_LEGACY, SRC_LMS):
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
        if source_id == SRC_ONBOARDING:
            fields: dict[str, object] = {"ts": self.onboarding.timestamp(at), "level": "info"}
            if rng.random() < 0.5:
                fields.update({"msg": "health check ok", "path": "/healthz", "status": 200})
            else:
                fields.update({"msg": "job queue drained", "queue": "notifications", "jobs": 0})
            self.onboarding.add(Truth(N_ONBOARDING_NOISE, at), ndjson_line(fields))
        elif source_id == SRC_LEGACY:
            at = clock.floor_seconds(at)
            self.legacy.add(
                Truth(N_LEGACY_NOISE, at),
                logfmt_line(
                    [
                        ("time", self.legacy.timestamp(at)),
                        ("lvl", "INFO"),
                        ("msg", "scheduler heartbeat"),
                        ("queue", "hires"),
                    ]
                ),
            )
        else:
            self.lms.add(
                Truth(N_LMS_NOISE, at),
                ndjson_line(
                    {
                        "ts": self.lms.timestamp(at),
                        "level": "info",
                        "msg": "session sweep",
                        "expired": rng.randint(0, 40),
                    }
                ),
            )

    # -- native files -------------------------------------------------------------------------

    def rendered(self, instant: datetime) -> str:
        return naive_local_seconds(clock.to_local(instant, self.zone))

    def write_hris_table(self) -> None:
        """employees.sql plus employees.csv; registers the row records (ADR 0006)."""
        directory = self.out_dir / "hris"
        directory.mkdir(parents=True, exist_ok=True)
        rows: list[list[str]] = []
        statements = [CREATE_HRIS_TABLE]
        for row in self.hris_rows:
            values = [
                str(row.row_id),
                row.employee_id,
                row.full_name,
                row.start_date.isoformat(),
                row.source_ref,
                row.department,
                row.created_by,
                self.rendered(row.created_at),
            ]
            rows.append(values)
            literals = [values[0], *(sql_string(v) for v in values[1:])]
            statements.append(sql_insert(HRIS_TABLE, HRIS_COLUMNS, literals))
            self.record_identifier(SYS_HRIS, SRC_HRIS, "employee_id", row.employee_id)
            if row.source_ref:
                self.record_identifier(SYS_HRIS, SRC_HRIS, "source_ref", row.source_ref)
            self.pii_values.add(row.full_name)
            self.actor_values.add(row.created_by)
            truth = Truth(N_EMPLOYEE_CREATED, row.created_at, row.txn_id, None, HUMAN)
            key = row_key(SRC_HRIS, HRIS_TABLE, row.row_id)
            self.emitted.append(Emitted(key, SRC_HRIS, SYS_HRIS, truth))
        self.write_text(directory / f"{HRIS_TABLE}.sql", "\n".join(statements) + "\n")
        self.write_text(directory / f"{HRIS_TABLE}.csv", csv_text(HRIS_COLUMNS, rows))

    def write_payroll_files(self) -> None:
        """One payroll_YYYYMMDD.csv per local day with entries; rows keyed by worker_number."""
        directory = self.out_dir / "payroll"
        directory.mkdir(parents=True, exist_ok=True)
        by_day: dict[date, list[PayrollRow]] = {}
        for row in self.payroll_rows:
            by_day.setdefault(clock.to_local(row.entered_at, self.zone).date(), []).append(row)
        for day in sorted(by_day):
            rows: list[list[str]] = []
            for row in by_day[day]:
                rows.append(
                    [
                        row.worker_number,
                        row.employee_ref,
                        row.legal_name,
                        row.start_date.isoformat(),
                        row.pay_group,
                        row.entered_by,
                        self.rendered(row.entered_at),
                    ]
                )
                self.record_identifier(SYS_PAYROLL, SRC_PAYROLL, "worker_number", row.worker_number)
                self.record_identifier(SYS_PAYROLL, SRC_PAYROLL, "employee_ref", row.employee_ref)
                self.pii_values.add(row.legal_name)
                self.actor_values.add(row.entered_by)
                truth = Truth(N_WORKER_ENTERED, row.entered_at, row.txn_id, None, HUMAN)
                key = row_key(SRC_PAYROLL, PAYROLL_TABLE, row.worker_number)
                self.emitted.append(Emitted(key, SRC_PAYROLL, SYS_PAYROLL, truth))
            self.write_text(
                directory / f"payroll_{day:%Y%m%d}.csv", csv_text(PAYROLL_COLUMNS, rows)
            )

    def write_sheet_files(self) -> None:
        """One sku_sheet_YYYYMMDD.csv per Friday export; rows keyed by legacy_sku."""
        directory = self.out_dir / "sku_sheets"
        directory.mkdir(parents=True, exist_ok=True)
        by_file: dict[str, list[SheetRow]] = {}
        for row in self.sheet_rows:
            by_file.setdefault(row.file_name, []).append(row)
        for file_name in sorted(by_file):
            rows: list[list[str]] = []
            for row in by_file[file_name]:
                rows.append(
                    [
                        row.legacy_sku,
                        row.description,
                        row.unit_price,
                        row.category,
                        self.rendered(row.added_at),
                    ]
                )
                self.record_identifier(SYS_SKU_SHEETS, SRC_SKU_EXPORT, "legacy_sku", row.legacy_sku)
                self.record_identifier(
                    SYS_SKU_SHEETS, SRC_SKU_EXPORT, "description", row.description
                )
                truth = Truth(N_SKU_ADDED, row.added_at, row.txn_id, None, HUMAN)
                key = row_key(SRC_SKU_EXPORT, SHEET_TABLE, row.legacy_sku)
                self.emitted.append(Emitted(key, SRC_SKU_EXPORT, SYS_SKU_SHEETS, truth))
            self.write_text(directory / file_name, csv_text(SHEET_COLUMNS, rows))

    def write_master_table(self) -> None:
        """sku_master.sql plus sku_master.csv; registers the row records (ADR 0006)."""
        directory = self.out_dir / "sku_master"
        directory.mkdir(parents=True, exist_ok=True)
        rows: list[list[str]] = []
        statements = [CREATE_MASTER_TABLE]
        for row in self.master_rows:
            values = [
                str(row.row_id),
                row.sku,
                row.description,
                row.unit_price,
                row.created_by,
                self.rendered(row.created_at),
            ]
            rows.append(values)
            literals = [
                values[0],
                sql_string(values[1]),
                sql_string(values[2]),
                values[3],
                sql_string(values[4]),
                sql_string(values[5]),
            ]
            statements.append(sql_insert(MASTER_TABLE, MASTER_COLUMNS, literals))
            self.record_identifier(SYS_SKU_MASTER, SRC_SKU_MASTER, "sku", row.sku)
            self.record_identifier(SYS_SKU_MASTER, SRC_SKU_MASTER, "description", row.description)
            self.actor_values.add(row.created_by)
            truth = Truth(N_SKU_CREATED, row.created_at, row.txn_id, None, HUMAN)
            key = row_key(SRC_SKU_MASTER, MASTER_TABLE, row.row_id)
            self.emitted.append(Emitted(key, SRC_SKU_MASTER, SYS_SKU_MASTER, truth))
        self.write_text(directory / f"{MASTER_TABLE}.sql", "\n".join(statements) + "\n")
        self.write_text(directory / f"{MASTER_TABLE}.csv", csv_text(MASTER_COLUMNS, rows))

    @staticmethod
    def write_text(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8", newline="\n")

    def write_native_files(self) -> None:
        for sink in (self.onboarding, self.legacy, self.lms):
            self.emitted.extend(sink.write(self.out_dir))
        self.write_hris_table()
        self.write_payroll_files()
        self.write_sheet_files()
        self.write_master_table()

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
        """Create ``out_dir`` or empty it, with the same refusals as scenario A: only an empty
        directory or a previous run (one holding ``ground_truth/manifest.json``) is emptied."""
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
        self.build_hires()
        self.number_rows()
        self.record_faults()
        self.build_skus()
        for hire in self.hires:
            self.emit_hire(hire)
        self.emit_noise()
        self.write_native_files()
        hashes = self.native_file_hashes()
        transactions = len(self.hires) + len(self.sheet_rows)
        truth = GroundTruth(
            manifest=people_ops_truth.build_manifest(
                self.request, PeopleOpsScenario.name, self.emitted, transactions, hashes
            ),
            sources=list(SOURCES),
            links=people_ops_truth.build_links(
                people_ops_truth.composite_agreement(self.hires, self.sheet_rows)
            ),
            entities=people_ops_truth.build_entities(),
            batches=[],
            faults=people_ops_truth.build_faults(
                self.request.faults, self.faults, self.window_start
            ),
            manual_hops=people_ops_truth.build_manual_hops(),
            markers=people_ops_truth.build_marker_set(
                self.marker_tokens,
                self.pii_values,
                self.identifier_values,
                actor_values=self.actor_values,
                amount_values={row.unit_price for row in self.sheet_rows},
            ),
        )
        ground_truth_dir = write_ground_truth(self.out_dir, truth, self.events())
        people_ops_truth.write_readme(self.out_dir, truth, self.start_day, self.zone)
        return GenerationResult(
            out_dir=self.out_dir, ground_truth_dir=ground_truth_dir, manifest=truth.manifest
        )


class PeopleOpsScenario:
    """Scenario C registered as ``people_ops`` (spec 19, 21 M2)."""

    name = "people_ops"
    description = (
        "Scenario C: a hire across two onboarding systems, HRIS, payroll and the LMS, "
        "plus SKU sheets re-keyed into a master; mostly manual hops."
    )

    def generate(self, request: GenerationRequest, out_dir: Path) -> GenerationResult:
        if request.scenario != self.name:
            msg = f"request is for scenario {request.scenario!r}, not {self.name!r}"
            raise ValueError(msg)
        return _PeopleOpsRun(request, out_dir).run()
