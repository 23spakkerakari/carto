"""The Compose stack bounds every service's memory, CPU and process count (spec 14.6 container
hardening, 2.3 invariant 8): one runaway container (a zip bomb, a fork loop, a ClickHouse merge)
must not take the VM down with it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose" / "compose.yaml"
_MEMORY = re.compile(r"^[1-9][0-9]*[mg]$")


def _services() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services: dict[str, dict[str, Any]] = document["services"]
    return services


def _mebibytes(value: str) -> int:
    number = int(value[:-1])
    return number * 1024 if value.endswith("g") else number


@pytest.mark.parametrize("name", sorted(_services()))
def test_every_service_has_memory_cpu_and_pid_limits(name: str) -> None:
    service = _services()[name]
    memory = service.get("mem_limit")
    assert isinstance(memory, str), f"{name}: mem_limit missing"
    assert _MEMORY.fullmatch(memory), f"{name}: mem_limit {memory!r} is not like 512m or 2g"
    assert 32 <= _mebibytes(memory) <= 8 * 1024
    cpus = service.get("cpus")
    assert isinstance(cpus, float | int) and not isinstance(cpus, bool), f"{name}: cpus missing"
    assert 0 < cpus <= 4
    pids = service.get("pids_limit")
    assert isinstance(pids, int) and not isinstance(pids, bool), f"{name}: pids_limit missing"
    assert 16 <= pids <= 4096
    # One way of saying it: the swarm-only deploy.resources form would be a second source.
    assert "deploy" not in service


def test_the_largest_budgets_go_to_the_databases() -> None:
    services = _services()
    clickhouse = _mebibytes(services["clickhouse"]["mem_limit"])
    others = [
        _mebibytes(service["mem_limit"])
        for name, service in services.items()
        if name != "clickhouse"
    ]
    assert clickhouse >= max(others)
