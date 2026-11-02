"""Ground truth assembly for scenario C: links, entities, faults, hops, markers, README.

Everything here is a pure function of what :mod:`carto_simulator.scenarios.people_ops`
generated; the models are the contract in :mod:`carto_simulator.ground_truth` (ADR 0006). Link
ids C01 to C14, hop ids H01 to H10 and fault ids c1 and c2 are stable names the eval harness can
rely on (plan M2 package H).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path

from carto_simulator import clock
from carto_simulator.api import GENERATOR_VERSION, GenerationRequest
from carto_simulator.ground_truth import (
    EVENTS_FILE,
    GROUND_TRUTH_DIR,
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
from carto_simulator.scenarios.people_ops_model import (
    BACKLOG_FIRST_DAY,
    BACKLOG_LANDING_DAY,
    BACKLOG_LAST_DAY,
    ENT_EMPLOYEE,
    ENT_SKU,
    HR_CLERKS,
    HRIS_NAME_TYPO_RATE,
    MASTER_DESCRIPTION_TYPO_RATE,
    N_APPLICATION,
    N_EMPLOYEE_CREATED,
    N_ENROLLED,
    N_LEGACY_APPLICATION,
    N_LEGACY_FIRST_DAY,
    N_LEGACY_OFFER,
    N_OFFER,
    N_ONBOARDED,
    N_SKU_ADDED,
    N_SKU_CREATED,
    N_WORKER_ENTERED,
    PAYROLL_CLERKS,
    PAYROLL_DROP_RATE,
    PAYROLL_REF_TYPO_RATE,
    PRODUCT_CLERKS,
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
    SheetRow,
)
from carto_simulator.writers import Emitted

PHONETIC = "phonetic.0"


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
    *,
    manual: bool = False,
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
            "manual": manual,
            "notes": notes,
        }
    )


def _component(
    a: FieldRef, form_a: str, b: FieldRef, form_b: str, rate: float
) -> CompositeComponent:
    return CompositeComponent(a=a, form_a=form_a, b=b, form_b=form_b, agreement_rate=rate)


@dataclass(frozen=True, slots=True)
class AgreementRates:
    """Measured agreement of the composite components over the generated pairs (spec 9.5).

    Name and description rates count exact equality after the clerk's keying, which is a lower
    bound of the phonetic agreement the edge computes (a swapped letter may leave the Double
    Metaphone code unchanged) and the exact value of the ``alnum`` agreement. Dates and prices
    are copied exactly, so their rate is 1.0 whenever a pair exists.
    """

    main_name: float
    main_date: float
    legacy_name: float
    legacy_date: float
    payroll_name: float
    payroll_date: float
    payroll_ref: float
    sku_description: float
    sku_price: float


def _share(pairs: list[bool]) -> float:
    return sum(pairs) / len(pairs) if pairs else 1.0


def composite_agreement(hires: Iterable[Hire], sheet_rows: Iterable[SheetRow]) -> AgreementRates:
    """Agreement rates of C04, C05, C08 and C14 over the rows that exist in the run."""
    main_names: list[bool] = []
    legacy_names: list[bool] = []
    payroll_names: list[bool] = []
    payroll_refs: list[bool] = []
    for hire in hires:
        row = hire.hris
        if row is None:
            continue
        (legacy_names if hire.legacy else main_names).append(row.full_name == hire.person.name)
        payroll = hire.payroll
        if payroll is not None:
            payroll_names.append(payroll.legal_name == row.full_name)
            payroll_refs.append(payroll.employee_ref == row.employee_id)
    descriptions = [
        row.master.description == row.description for row in sheet_rows if row.master is not None
    ]
    return AgreementRates(
        main_name=_share(main_names),
        main_date=1.0,
        legacy_name=_share(legacy_names),
        legacy_date=1.0,
        payroll_name=_share(payroll_names),
        payroll_date=1.0,
        payroll_ref=_share(payroll_refs),
        sku_description=_share(descriptions),
        sku_price=1.0,
    )


def build_links(rates: AgreementRates) -> list[LinkTruth]:
    """C01 to C14 (spec 9.3 exact, 9.4 bridges, 9.5 composite; plan M2 package H)."""
    onb_cand = _ref(SYS_ONBOARDING, SRC_ONBOARDING, "candidate_id")
    onb_name = _ref(SYS_ONBOARDING, SRC_ONBOARDING, "candidate_name")
    onb_email = _ref(SYS_ONBOARDING, SRC_ONBOARDING, "work_email")
    onb_start = _ref(SYS_ONBOARDING, SRC_ONBOARDING, "start_date")
    leg_cand = _ref(SYS_LEGACY, SRC_LEGACY, "cand_ref")
    leg_name = _ref(SYS_LEGACY, SRC_LEGACY, "applicant")
    leg_mail = _ref(SYS_LEGACY, SRC_LEGACY, "mail")
    leg_start = _ref(SYS_LEGACY, SRC_LEGACY, "startDt")
    hr_emp = _ref(SYS_HRIS, SRC_HRIS, "employee_id")
    hr_ref = _ref(SYS_HRIS, SRC_HRIS, "source_ref")
    hr_name = _ref(SYS_HRIS, SRC_HRIS, "full_name")
    hr_start = _ref(SYS_HRIS, SRC_HRIS, "start_date")
    pay_worker = _ref(SYS_PAYROLL, SRC_PAYROLL, "worker_number")
    pay_ref = _ref(SYS_PAYROLL, SRC_PAYROLL, "employee_ref")
    pay_name = _ref(SYS_PAYROLL, SRC_PAYROLL, "legal_name")
    pay_start = _ref(SYS_PAYROLL, SRC_PAYROLL, "start_date")
    lms_user = _ref(SYS_LMS, SRC_LMS, "username")
    lms_email = _ref(SYS_LMS, SRC_LMS, "user_email")
    sheet_desc = _ref(SYS_SKU_SHEETS, SRC_SKU_EXPORT, "description")
    sheet_price = _ref(SYS_SKU_SHEETS, SRC_SKU_EXPORT, "unit_price")
    master_desc = _ref(SYS_SKU_MASTER, SRC_SKU_MASTER, "description")
    master_price = _ref(SYS_SKU_MASTER, SRC_SKU_MASTER, "unit_price")

    def composite(
        link_id: str,
        a: FieldRef,
        form_a: str,
        b: FieldRef,
        form_b: str,
        entity_id: str,
        components: list[CompositeComponent],
        notes: str,
    ) -> LinkTruth:
        return LinkTruth(
            link_id=link_id,
            a=a,
            form_a=form_a,
            b=b,
            form_b=form_b,
            link_type="composite",
            role="transaction",
            direction="a_to_b",
            entity_id=entity_id,
            manual=True,
            components=components,
            notes=notes,
        )

    ref_note = (
        f"HR clerks key the candidate id into source_ref for {SOURCE_REF_SHARE:.0%} of rows; "
        "the rest are blank and only the composite link (C04/C05) reaches them."
    )
    return [
        _link(
            "C01",
            onb_cand,
            "raw",
            hr_ref,
            "raw",
            "exact",
            "transaction",
            "a_to_b",
            ENT_EMPLOYEE,
            ref_note,
            manual=True,
        ),
        _link(
            "C02",
            leg_cand,
            "raw",
            hr_ref,
            "raw",
            "exact",
            "transaction",
            "a_to_b",
            ENT_EMPLOYEE,
            ref_note,
            manual=True,
        ),
        _link(
            "C03",
            hr_emp,
            "raw",
            hr_ref,
            "raw",
            "bridge",
            "transaction",
            "undirected",
            ENT_EMPLOYEE,
            "Bridge inside employees rows (spec 9.4); source_ref is blank in 40% of them.",
        ),
        composite(
            "C04",
            onb_name,
            PHONETIC,
            hr_name,
            PHONETIC,
            ENT_EMPLOYEE,
            [
                _component(onb_name, PHONETIC, hr_name, PHONETIC, rates.main_name),
                _component(onb_start, "date", hr_start, "date", rates.main_date),
            ],
            f"HR keys the name by hand with {HRIS_NAME_TYPO_RATE:.0%} typos 1 to 2 business days "
            "after the start; phonetic name plus start date is the composite path (spec 9.5).",
        ),
        composite(
            "C05",
            leg_name,
            PHONETIC,
            hr_name,
            PHONETIC,
            ENT_EMPLOYEE,
            [
                _component(leg_name, PHONETIC, hr_name, PHONETIC, rates.legacy_name),
                _component(leg_start, "date", hr_start, "date", rates.legacy_date),
            ],
            "Same path from the legacy system; startDt is %d/%m/%Y in naive Europe/London time.",
        ),
        _link(
            "C06",
            hr_emp,
            "raw",
            pay_ref,
            "raw",
            "exact",
            "transaction",
            "a_to_b",
            ENT_EMPLOYEE,
            f"Keyed by payroll clerks with {PAYROLL_REF_TYPO_RATE:.0%} typos.",
            manual=True,
        ),
        _link(
            "C07",
            pay_worker,
            "raw",
            pay_ref,
            "raw",
            "bridge",
            "transaction",
            "undirected",
            ENT_EMPLOYEE,
            "Bridge inside payroll rows (spec 9.4).",
        ),
        composite(
            "C08",
            hr_name,
            PHONETIC,
            pay_name,
            PHONETIC,
            ENT_EMPLOYEE,
            [
                _component(hr_name, PHONETIC, pay_name, PHONETIC, rates.payroll_name),
                _component(hr_start, "date", pay_start, "date", rates.payroll_date),
                _component(hr_emp, "digits.0", pay_ref, "digits.0", rates.payroll_ref),
            ],
            "Name plus start date plus the employee id digits: what survives a mis-keyed "
            "employee_ref (spec 9.5, 11.2 mismatch rate).",
        ),
        _link(
            "C09",
            onb_email,
            "raw",
            lms_email,
            "raw",
            "exact",
            "transaction",
            "a_to_b",
            ENT_EMPLOYEE,
            "The LMS provisioner creates the enrollment from the work email (automated).",
        ),
        _link(
            "C10",
            leg_mail,
            "raw",
            lms_email,
            "raw",
            "exact",
            "transaction",
            "a_to_b",
            ENT_EMPLOYEE,
            "Same for legacy hires.",
        ),
        _link(
            "C11",
            lms_user,
            "raw",
            lms_email,
            "raw",
            "bridge",
            "transaction",
            "undirected",
            ENT_EMPLOYEE,
            "Bridge inside enrollment lines: username = local part, '+' as '.'.",
        ),
        _link(
            "C12",
            onb_cand,
            "raw",
            onb_email,
            "raw",
            "bridge",
            "transaction",
            "undirected",
            ENT_EMPLOYEE,
            "Bridge inside 'onboarding started' lines.",
        ),
        _link(
            "C13",
            leg_cand,
            "raw",
            leg_mail,
            "raw",
            "bridge",
            "transaction",
            "undirected",
            ENT_EMPLOYEE,
            "Bridge inside 'first day' lines.",
        ),
        composite(
            "C14",
            sheet_desc,
            "alnum",
            master_desc,
            "alnum",
            ENT_SKU,
            [
                _component(sheet_desc, "alnum", master_desc, "alnum", rates.sku_description),
                _component(sheet_desc, PHONETIC, master_desc, PHONETIC, rates.sku_description),
                _component(sheet_price, "amount", master_price, "amount", rates.sku_price),
            ],
            "No shared identifier: a product clerk re-keys the weekly sheet export into the "
            f"master with {MASTER_DESCRIPTION_TYPO_RATE:.0%} description typos; the master "
            "assigns the sku and never carries the legacy_sku (spec 19 scenario C).",
        ),
    ]


def build_entities() -> list[EntityTruth]:
    def ent(system_id: str, source_id: str, field_name: str, form: str = "raw") -> EntityField:
        return EntityField(ref=_ref(system_id, source_id, field_name), form=form)

    return [
        EntityTruth(
            entity_id=ENT_EMPLOYEE,
            name="Employee",
            fields=[
                ent(SYS_ONBOARDING, SRC_ONBOARDING, "candidate_id"),
                ent(SYS_ONBOARDING, SRC_ONBOARDING, "candidate_name", PHONETIC),
                ent(SYS_ONBOARDING, SRC_ONBOARDING, "work_email"),
                ent(SYS_LEGACY, SRC_LEGACY, "cand_ref"),
                ent(SYS_LEGACY, SRC_LEGACY, "applicant", PHONETIC),
                ent(SYS_LEGACY, SRC_LEGACY, "mail"),
                ent(SYS_HRIS, SRC_HRIS, "employee_id"),
                ent(SYS_HRIS, SRC_HRIS, "source_ref"),
                ent(SYS_HRIS, SRC_HRIS, "full_name", PHONETIC),
                ent(SYS_PAYROLL, SRC_PAYROLL, "worker_number"),
                ent(SYS_PAYROLL, SRC_PAYROLL, "employee_ref"),
                ent(SYS_PAYROLL, SRC_PAYROLL, "legal_name", PHONETIC),
                ent(SYS_LMS, SRC_LMS, "username"),
                ent(SYS_LMS, SRC_LMS, "user_email"),
            ],
        ),
        EntityTruth(
            entity_id=ENT_SKU,
            name="SKU",
            fields=[
                ent(SYS_SKU_SHEETS, SRC_SKU_EXPORT, "legacy_sku"),
                ent(SYS_SKU_SHEETS, SRC_SKU_EXPORT, "description", "alnum"),
                ent(SYS_SKU_MASTER, SRC_SKU_MASTER, "sku"),
                ent(SYS_SKU_MASTER, SRC_SKU_MASTER, "description", "alnum"),
            ],
        ),
    ]


def build_faults(enabled: bool, faults: FaultState, window_start: datetime) -> list[FaultTruth]:
    """c1 (the week-3 payroll backlog) when it affected a row, and c2 (hires that never reach
    payroll) when there is one; spec 19 scenario C, 18.4 detection metrics."""
    if not enabled:
        return []
    result: list[FaultTruth] = []
    if faults.backlog_start is not None:
        result.append(
            FaultTruth(
                fault_id="c1_payroll_backlog_week3",
                kind=FaultKind.PARTIAL_STALL,
                start=faults.backlog_start,
                end=faults.backlog_end,
                system_id=SYS_PAYROLL,
                source_id=SRC_PAYROLL,
                affected_txn_ids=faults.backlog_affected,
                attributes={"hop": "H03", "due_days": f"{BACKLOG_FIRST_DAY}-{BACKLOG_LAST_DAY}"},
                expected_alert_kind="hop_deadline",
                expected_cause_kind="systemic_stall",
                notes=(
                    f"Every payroll entry due on run days {BACKLOG_FIRST_DAY} to "
                    f"{BACKLOG_LAST_DAY} is keyed on day {BACKLOG_LANDING_DAY}: three daily "
                    "exports are missing and one file carries them all. The whole hop stalls "
                    "(hop volume zero, spec 10.4 item 6: systemic), bounded in time, so the "
                    "closest contract kind is partial_stall; 'end' is the first backlog entry."
                ),
            )
        )
    if faults.dropped:
        result.append(
            FaultTruth(
                fault_id="c2_payroll_never_reached",
                kind=FaultKind.PARTIAL_STALL,
                start=window_start,
                end=None,
                system_id=SYS_PAYROLL,
                source_id=SRC_PAYROLL,
                affected_txn_ids=faults.dropped,
                attributes={"hop": "H03", "share": f"{PAYROLL_DROP_RATE:.2f}"},
                expected_alert=False,
                expected_alert_kind=None,
                notes=(
                    f"{PAYROLL_DROP_RATE:.0%} of hires with an HRIS row never get a payroll "
                    "row: per-transaction stalls at the manual hop (spec 11.2 drop rate). "
                    "Truth only in M2: no business alert is expected until M4's detector."
                ),
            )
        )
    return result


def build_manual_hops() -> list[ManualHopTruth]:
    """H01 to H04 are the manual hops of spec 19 scenario C; H05 to H10 are automated."""
    manual = [
        ("H01", N_ONBOARDED, N_EMPLOYEE_CREATED, ENT_EMPLOYEE, HR_CLERKS, HRIS_NAME_TYPO_RATE),
        (
            "H02",
            N_LEGACY_FIRST_DAY,
            N_EMPLOYEE_CREATED,
            ENT_EMPLOYEE,
            HR_CLERKS,
            HRIS_NAME_TYPO_RATE,
        ),
        (
            "H03",
            N_EMPLOYEE_CREATED,
            N_WORKER_ENTERED,
            ENT_EMPLOYEE,
            PAYROLL_CLERKS,
            PAYROLL_REF_TYPO_RATE,
        ),
        ("H04", N_SKU_ADDED, N_SKU_CREATED, ENT_SKU, PRODUCT_CLERKS, MASTER_DESCRIPTION_TYPO_RATE),
    ]
    automated = [
        ("H05", N_APPLICATION, N_OFFER),
        ("H06", N_OFFER, N_ONBOARDED),
        ("H07", N_LEGACY_APPLICATION, N_LEGACY_OFFER),
        ("H08", N_LEGACY_OFFER, N_LEGACY_FIRST_DAY),
        ("H09", N_ONBOARDED, N_ENROLLED),
        ("H10", N_LEGACY_FIRST_DAY, N_ENROLLED),
    ]
    hops = [
        ManualHopTruth(
            hop_id=hop_id,
            from_node=a,
            to_node=b,
            entity_id=entity_id,
            manual=True,
            share_manual=1.0,
            actors=list(actors),
            typo_rate=typo_rate,
        )
        for hop_id, a, b, entity_id, actors, typo_rate in manual
    ]
    hops.extend(
        ManualHopTruth(
            hop_id=hop_id,
            from_node=a,
            to_node=b,
            entity_id=ENT_EMPLOYEE,
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
        "# Scenario C (people_ops): generated data",
        "",
        (
            f"Generated by carto-sim {GENERATOR_VERSION}: {manifest.days} days from "
            f"{start_day.isoformat()} (local time {clock.LOCAL_ZONE_KEY}; the legacy onboarding "
            f"system logs Europe/London time), daily volume {manifest.daily_volume} (a scale: "
            f"{manifest.daily_volume // 20} main and {-(-manifest.daily_volume // 80)} legacy "
            f"hires per weekday before the week-start weights), seed {manifest.seed}, faults "
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
        lines.append("None (generated with faults off, or nothing was affected in this run).")
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
