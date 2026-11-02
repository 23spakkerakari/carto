"""Statistical properties on a larger run: clerk share, typo rate (spec 19, 11.1)."""

from __future__ import annotations

import csv
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from carto_simulator.api import GenerationRequest, generate
from carto_simulator.ground_truth import ActorKind, iter_events, read_ground_truth
from carto_simulator.names import SERVICE_ACCOUNT
from carto_simulator.scenarios.shop_model import CounterBases

pytestmark = pytest.mark.slow


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_clerk_and_typo_shares_on_a_14_day_run(tmp_path: Path) -> None:
    request = GenerationRequest(
        days=14, seed=21, daily_volume=200, start_date=date(2026, 9, 23), noise_rate=0.1
    )
    generate(request, tmp_path)
    truth = read_ground_truth(tmp_path)
    table = rows(tmp_path / "warehouse/purchase_orders.csv") + rows(
        tmp_path / "warehouse/purchase_orders.renamed.csv"
    )
    txn_of = {e.key: e.txn_id for e in iter_events(tmp_path) if e.source_id == "src_wms_db"}
    clerk_rows = [row for row in table if row["created_by"] != SERVICE_ACCOUNT]
    share = len(clerk_rows) / len(table)
    assert 0.30 - 0.08 <= share <= 0.30 + 0.08
    typos = 0
    for row in clerk_rows:
        txn_id = txn_of[f"src_wms_db:purchase_orders:row:{row['id']}"]
        assert txn_id is not None
        order_id = CounterBases.for_seed(21).order + int(txn_id.removeprefix("txn_")) - 1
        if int(row["order_ref"][3:]) != order_id:
            typos += 1
    typo_share = typos / len(clerk_rows)
    assert 0.0 <= typo_share <= 0.04
    assert typos > 0
    actors = Counter(row["created_by"] for row in clerk_rows)
    assert len(actors) == 8
    human_events = sum(1 for e in iter_events(tmp_path) if e.actor_kind == ActorKind.HUMAN)
    assert human_events == len(clerk_rows)
    assert truth.manifest.counts["transactions"] == 10 * 200 + 4 * 100
