"""``carto-sim``: simulator command line, batch mode (spec 19, M0 ``make sim``) and live mode.

    carto-sim generate --scenario shop --days 14 --seed 1 --daily-volume 800 \\
        --start-date 2026-09-23 --out sim-out/shop [--no-faults] [--noise-rate 1.0] \\
        [--pii-density 0.3]
    carto-sim live --scenario shop --days 2 --speed 60 --out /data/sim \\
        [--duration-seconds 3600] [the generate options]
    carto-sim as-of --out sim-out/shop
    carto-sim list

Exit codes: 0 on success (a live replay stopped with Ctrl-C or SIGTERM counts as success), 2 for
an unknown scenario, an invalid request or an output path that cannot be used (a file, or a
populated directory that is not a previous run).
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from carto_simulator import api, live
from carto_simulator.scenarios import list_scenarios


def _add_request_arguments(parser: argparse.ArgumentParser, *, days: int) -> None:
    """The spec 19 inputs shared by ``generate`` and ``live``."""
    parser.add_argument("--scenario", default=api.DEFAULT_SCENARIO)
    parser.add_argument("--days", type=int, default=days)
    parser.add_argument("--seed", type=int, default=api.DEFAULT_SEED)
    parser.add_argument("--daily-volume", type=int, default=api.DEFAULT_DAILY_VOLUME)
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=api.DEFAULT_START_DATE,
        help="first local day, ISO format (default %(default)s)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="output directory; an empty one or a previous run is emptied first",
    )
    parser.add_argument("--no-faults", action="store_true", help="generate a fault-free run")
    parser.add_argument("--noise-rate", type=float, default=api.DEFAULT_NOISE_RATE)
    parser.add_argument("--pii-density", type=float, default=api.DEFAULT_PII_DENSITY)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="carto-sim", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="write native files and ground truth")
    _add_request_arguments(generate, days=api.DEFAULT_DAYS)
    replay = commands.add_parser(
        "live",
        help="generate, then replay the run into --out in real time (spec 19 live mode)",
    )
    _add_request_arguments(replay, days=live.DEFAULT_LIVE_DAYS)
    replay.add_argument(
        "--speed",
        type=float,
        default=live.DEFAULT_SPEED,
        help="simulated seconds per real second (default %(default)s: a minute per second)",
    )
    replay.add_argument(
        "--duration-seconds",
        type=float,
        default=None,
        help="stop after this much real time; default: replay everything",
    )
    as_of = commands.add_parser(
        "as-of",
        help="print the time to analyze a batch run as of (carto-edge analyze --as-of, ADR 0036)",
    )
    as_of.add_argument("--out", type=Path, required=True, help="the run's output directory")
    commands.add_parser("list", help="print the known scenarios")
    return parser


def _request(args: argparse.Namespace) -> api.GenerationRequest:
    return api.GenerationRequest(
        scenario=args.scenario,
        days=args.days,
        seed=args.seed,
        daily_volume=args.daily_volume,
        start_date=args.start_date,
        faults=not args.no_faults,
        noise_rate=args.noise_rate,
        pii_density=args.pii_density,
    )


def _generate(args: argparse.Namespace) -> int:
    try:
        request = _request(args)
    except ValueError as exc:
        print(f"carto-sim: {exc}", file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        result = api.generate(request, args.out)
    except (KeyError, ValueError, OSError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) else exc
        print(f"carto-sim: {message}", file=sys.stderr)
        return 2
    elapsed = time.perf_counter() - started
    counts = result.manifest.counts
    sources = len(counts) - 4  # minus the totals: transactions, events, noise, files
    print(f"scenario {request.scenario}: {request.days} days from {request.start_date}")
    print(f"out: {result.out_dir}")
    print(f"sources: {sources}, events: {counts['events']}, transactions: {counts['transactions']}")
    faults = "on" if request.faults else "off"
    print(f"noise: {counts['noise']}, files: {counts['files']}, faults: {faults}")
    print(f"elapsed: {elapsed:.1f} s")
    return 0


def _live_plan(args: argparse.Namespace) -> live.LivePlan:
    """Validate the request and plan the replay; ``ValueError`` names every invalid input."""
    request = _request(args)
    if args.speed <= 0:
        msg = "speed must be positive"
        raise ValueError(msg)
    if args.duration_seconds is not None and args.duration_seconds <= 0:
        msg = "duration-seconds must be positive"
        raise ValueError(msg)
    anchor = datetime.now(UTC)  # the only wall-clock read: where the first record lands
    plan = live.plan_live(request, args.out, now=anchor)
    live.write_static_files(plan)
    return plan


def _live(args: argparse.Namespace) -> int:
    """Plan, write the static files, replay. Prints counts and file names only (spec 2.3 #7)."""
    try:
        plan = _live_plan(args)
    except (KeyError, ValueError, OSError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) else exc
        print(f"carto-sim: {message}", file=sys.stderr)
        return 2
    request = plan.request
    print(f"scenario {request.scenario} live: {request.days} days from {request.start_date}")
    print(f"out: {plan.out_dir}")
    print(
        f"records: {len(plan.items)}, first at {plan.anchor.isoformat()}, "
        f"simulated span {plan.simulated_span}, speed: {args.speed:g}x, "
        f"replay takes about {plan.real_seconds(args.speed) / 60:.1f} min"
    )
    stop = {"requested": False}

    def request_stop(_signum: int, _frame: object) -> None:
        stop["requested"] = True

    def progress(stats: live.ReplayStats) -> None:
        at = stats.simulated_at.isoformat() if stats.simulated_at is not None else "-"
        print(f"replayed {stats.written}/{stats.planned} records, simulated clock {at}")

    previous = signal.signal(signal.SIGTERM, request_stop)
    try:
        stats = live.replay(
            plan,
            speed=args.speed,
            duration_seconds=args.duration_seconds,
            should_stop=lambda: stop["requested"],
            progress=progress,
        )
    except KeyboardInterrupt:
        print("stopped by user; files are flushed and the ground truth describes the whole run")
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(
        f"{stats.stopped_by}: records {stats.written}/{stats.planned} "
        f"(lines {stats.lines}, rows {stats.rows}, files {stats.files})"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list":
        for name in list_scenarios():
            print(name)
        return 0
    if args.command == "live":
        return _live(args)
    if args.command == "as-of":
        try:
            print(api.run_as_of(args.out).isoformat().replace("+00:00", "Z"))
        except (OSError, ValueError):
            print(f"carto-sim: no readable run manifest under {args.out}", file=sys.stderr)
            return 2
        return 0
    return _generate(args)


if __name__ == "__main__":
    sys.exit(main())
