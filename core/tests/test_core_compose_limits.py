"""The Compose stack bounds every service's memory, CPU and process count (spec 14.6 container
hardening, 2.3 invariant 8): one runaway container (a zip bomb, a fork loop, a ClickHouse merge)
must not take the VM down with it. Every service also carries the hardening anchor, runs as a
non-root user, and only the console and the push-route proxies publish a port, bound to the
loopback interface unless ``CARTO_BIND`` says otherwise (spec 14.5, 2.3 invariant 9, ADR 0072)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose" / "compose.yaml"
BASE_DOCKERFILE = COMPOSE.parents[1] / "docker" / "base.Dockerfile"
WEB_DOCKERFILE = COMPOSE.parents[2] / "web" / "deploy" / "Dockerfile"
_MEMORY = re.compile(r"^[1-9][0-9]*[mg]$")

# The documented exceptions to "runs as uid 10001 with no capability" (deploy/compose/README.md):
# the official database images start as root, fix their data directory and drop to their own
# user, which needs exactly the x-db-capabilities set (ClickHouse's entrypoint also signals its
# temporary server); volumes-init is root with CHOWN only, no network, to chown fresh volumes.
DB_CAPABILITIES = frozenset({"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"})
ROOT_STARTERS: dict[str, frozenset[str]] = {
    "postgres": DB_CAPABILITIES,
    "clickhouse": DB_CAPABILITIES | {"KILL"},
    "volumes-init": frozenset({"CHOWN"}),
}
# The only services that publish a port (spec 14.5): the console origin and the push routes.
PUBLISHERS = frozenset({"web", "proxy-otlp", "proxy-webhooks"})
_PUBLISHED = re.compile(r"^\$\{CARTO_BIND:-127\.0\.0\.1\}:\$\{CARTO_[A-Z]+_PORT:-[0-9]+\}:[0-9]+$")


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


@pytest.mark.parametrize("name", sorted(_services()))
def test_every_service_carries_the_hardening_anchor(name: str) -> None:
    service = _services()[name]
    assert service.get("read_only") is True, f"{name}: read_only missing"
    assert "no-new-privileges:true" in service.get("security_opt", []), f"{name}: no-new-privileges"
    assert service.get("cap_drop") == ["ALL"], f"{name}: cap_drop ALL missing"
    added = set(service.get("cap_add", []))
    assert added <= ROOT_STARTERS.get(name, frozenset()), f"{name}: unexpected cap_add {added}"
    for key in ("privileged", "pid", "ipc", "devices", "sysctls"):
        assert key not in service, f"{name}: {key} must not be set"


@pytest.mark.parametrize("name", sorted(_services()))
def test_every_service_runs_as_a_non_root_user(name: str) -> None:
    service = _services()[name]
    user = service.get("user")
    if name in ROOT_STARTERS:
        # Root at start by design; volumes-init says so explicitly and has no network.
        assert user in (None, "0:0"), f"{name}: unexpected user {user!r}"
        if name == "volumes-init":
            assert user == "0:0" and service.get("network_mode") == "none"
        return
    if user is None:
        # The carto images switch to uid 10001 in the Dockerfile (asserted below).
        assert str(service["image"]).startswith("carto-"), f"{name}: no user and not a carto image"
        return
    uid = str(user).split(":")[0]
    assert uid.isdigit() and int(uid) >= 10000, f"{name}: user {user!r} is not a dedicated uid"


def test_the_carto_images_switch_to_uid_10001() -> None:
    for dockerfile in (BASE_DOCKERFILE, WEB_DOCKERFILE):
        lines = [line.strip() for line in dockerfile.read_text(encoding="utf-8").splitlines()]
        assert "USER 10001:10001" in lines, f"{dockerfile.name}: USER 10001:10001 missing"


def test_only_the_console_and_the_push_proxies_publish_a_port_on_loopback() -> None:
    for name, service in _services().items():
        ports = service.get("ports")
        networks = service.get("networks", [])
        if name in PUBLISHERS:
            assert isinstance(ports, list) and ports, f"{name}: must publish its port"
            for entry in ports:
                assert isinstance(entry, str) and _PUBLISHED.fullmatch(entry), f"{name}: {entry!r}"
            assert "ingress" in networks, f"{name}: a published port needs the ingress network"
        else:
            assert ports is None, f"{name}: must not publish a port"
            assert "expose" not in service, f"{name}: must not expose a port"
            assert "ingress" not in networks, f"{name}: only publishers join ingress"
