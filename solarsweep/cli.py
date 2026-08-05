"""Command line interface.

``solarsweep --help`` is the whole surface. The commands that matter for
getting the physical machine working again are ``selftest`` and ``calibrate``:
between them they replace the "wire it up and hope" step that left v1 untested.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import textwrap
from dataclasses import replace

from . import __version__
from .config import ConfigError, Settings, load_settings
from .control import SolarSweepRobot
from .control.axis import Direction
from .hal import InputId, MotorId, RelayId, make_board
from .logging_setup import setup_logging
from .safety import SafetyAbort
from .telemetry import RunHistory

logger = logging.getLogger(__name__)

OK = "  ok  "
FAIL = " FAIL "
WARN = " warn "


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solarsweep",
        description="Rail-guided solar panel cleaning robot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              solarsweep run --sim                      one simulated cycle
              solarsweep run --sim --fault stuck_limit_far
              solarsweep selftest                       sensor bring-up check
              solarsweep selftest --motion --yes        adds motor/relay tests
              solarsweep calibrate                      measure drive.duty_to_mmps
              solarsweep serve                          dashboard + scheduler
              solarsweep runs                           past run summaries
            """
        ),
    )
    parser.add_argument("--version", action="version", version=f"solarsweep {__version__}")
    parser.add_argument("-c", "--config", default="config/settings.yaml",
                        help="path to settings.yaml (default: %(default)s)")
    parser.add_argument("--sim", action="store_true",
                        help="use the simulator instead of real hardware")
    parser.add_argument("--time-scale", type=float, default=None, metavar="X",
                        help="simulator speedup; 0 runs as fast as possible")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one cleaning cycle")
    run.add_argument("--mode", choices=("full_cycle", "quick_pass"), default="full_cycle")
    run.add_argument("--fault", default="", metavar="SPEC",
                     help="simulator fault injection, e.g. 'rain@30' or "
                          "'stuck_limit_far'; comma-separate for several")
    run.add_argument("--json", action="store_true", help="print the run summary as JSON")

    sub.add_parser("status", help="print one status snapshot and exit")

    st = sub.add_parser("selftest", help="check sensors, and optionally actuators")
    st.add_argument("--motion", action="store_true",
                    help="also pulse the relays and jog the motors")
    st.add_argument("--yes", action="store_true",
                    help="confirm that the machine is clear of people and obstructions")

    cal = sub.add_parser(
        "calibrate",
        help="measure carriage speed and print the drive.duty_to_mmps to configure",
    )
    cal.add_argument("--duty", type=int, default=None,
                     help="duty %% to calibrate at (default: drive.transit_duty_pct)")
    cal.add_argument("--yes", action="store_true",
                     help="confirm the rail is clear; the carriage will traverse it")

    serve = sub.add_parser("serve", help="run the dashboard and scheduler")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--no-scheduler", action="store_true")

    runs = sub.add_parser("runs", help="list recent run summaries")
    runs.add_argument("-n", "--limit", type=int, default=10)
    runs.add_argument("--json", action="store_true")

    return parser


def _load(args: argparse.Namespace) -> Settings:
    settings = load_settings(args.config)
    sim = settings.sim
    if getattr(args, "fault", ""):
        if not args.sim:
            raise ConfigError("--fault only applies with --sim")
        sim = replace(sim, fault=args.fault)
    if args.time_scale is not None:
        sim = replace(sim, time_scale=args.time_scale)
    if sim is not settings.sim:
        settings = replace(settings, sim=sim)
        settings.validate()
    return settings


def _make_robot(settings: Settings, args: argparse.Namespace) -> SolarSweepRobot:
    board = make_board(settings, simulate=True if args.sim else None)
    return SolarSweepRobot(board, settings)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    with _make_robot(settings, args) as robot:
        _install_signal_handlers(robot)
        summary = robot.run_cycle(args.mode)

        if args.json:
            print(json.dumps(summary.__dict__, indent=2))
        else:
            _print_summary(summary, robot)

        if args.sim:
            board = robot.board
            coverage = getattr(board, "coverage_pct", None)
            if coverage is not None:
                print(f"  simulated coverage    {coverage:.0f}% of the cleanable span")
                print(f"  simulated water used  {board.state.water_dispensed_ml:.0f} ml")
                print(f"  residual soiling      {board.state.soiling * 100:.0f}%")

    return 0 if summary.outcome == "completed" else 1


def _print_summary(summary, robot: SolarSweepRobot) -> None:
    print()
    print(f"  run {summary.run_id}  [{summary.outcome.upper()}]")
    print(f"  mode                  {summary.mode}")
    print(f"  passes                {summary.passes_completed}/{summary.passes_planned}")
    print(f"  distance swept        {summary.distance_mm:.0f} mm")
    print(f"  water pulses          {summary.water_pulses}")
    if summary.dust_before is not None:
        after = summary.dust_after if summary.dust_after is not None else "-"
        print(f"  dust before/after     {summary.dust_before} / {after}")
    print(f"  duration              {summary.duration_s:.1f} s")
    if summary.stop_reason:
        print(f"  stopped by            {summary.stop_reason}")
    if summary.detail:
        print(f"  detail                {summary.detail}")
    print(f"  final state           {robot.machine.state.value}")
    print()


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    with _make_robot(settings, args) as robot:
        print(json.dumps(robot.status, indent=2))
    return 0


def cmd_selftest(args: argparse.Namespace, settings: Settings) -> int:
    """Bring-up check. Sensors always; actuators only when asked.

    Motion is opt-in and gated behind --yes because the first time this runs
    on a real machine, nobody knows yet whether the wiring is right.
    """
    failures = 0
    board = make_board(settings, simulate=True if args.sim else None)
    try:
        print(f"\nSolarSweep selftest — backend: {board.info.backend}\n")

        print("configuration")
        print(f"  [{OK}] settings parsed and validated")
        stall_ok = settings.drive.closed_loop or settings.safety.overcurrent_a > 0
        if stall_ok:
            print(f"  [{OK}] stall protection configured")
        else:
            print(f"  [{WARN}] no encoder and no current sensing — stall detection "
                  "unavailable, unattended runs will be refused")

        print("\ninputs (asserted = the condition is present)")
        expectations = {
            InputId.ESTOP: (False, "e-stop should be released"),
            InputId.RAIN: (False, "should read dry indoors"),
            InputId.OBSTACLE: (False, "should read clear"),
            InputId.WATER_PRESSURE: (True, "supply should be pressurised"),
        }
        for channel in InputId:
            value = board.read_input(channel)
            expected = expectations.get(channel)
            if expected is None:
                print(f"  [{OK}] {channel.value:16s} {value}")
                continue
            want, note = expected
            if value == want:
                print(f"  [{OK}] {channel.value:16s} {value}")
            else:
                failures += 1
                print(f"  [{FAIL}] {channel.value:16s} {value}  ({note})")

        home = board.read_input(InputId.LIMIT_HOME)
        far = board.read_input(InputId.LIMIT_FAR)
        if home and far:
            failures += 1
            print(f"  [{FAIL}] both limit switches assert at once — check for a "
                  "short or swapped wiring")

        dust = board.read_dust()
        if dust == 0:
            print(f"  [{WARN}] dust sensor reads 0 — MCP3008 may not be wired or "
                  "SPI may be disabled")
        else:
            print(f"  [{OK}] dust sensor      {dust} counts")

        encoder = board.read_encoder_mm()
        if settings.drive.closed_loop and encoder is None:
            failures += 1
            print(f"  [{FAIL}] encoder configured but not readable")

        if args.motion:
            if not args.yes:
                print("\n  motion tests skipped: pass --yes to confirm the machine "
                      "is clear of people and obstructions")
            else:
                failures += _motion_checks(board, settings)
        else:
            print("\n  motion tests skipped (pass --motion --yes to include them)")

        print()
        if failures:
            print(f"{failures} check(s) failed. See docs/COMMISSIONING.md.\n")
        else:
            print("All checks passed.\n")
        return 1 if failures else 0
    finally:
        board.close()


def _motion_checks(board, settings: Settings) -> int:
    failures = 0
    clock = board.clock
    print("\nactuators")

    for relay in RelayId:
        board.set_relay(relay, True)
        clock.sleep(0.4)
        board.set_relay(relay, False)
        print(f"  [{OK}] {relay.value:16s} pulsed 400ms — you should have heard a click")

    for motor, label in ((MotorId.BRUSH, "brush"), (MotorId.DRIVE, "drive")):
        for direction, name in ((Direction.FAR, "forward"), (Direction.HOME, "reverse")):
            duty = settings.drive.homing_duty_pct
            board.set_motor(motor, direction * duty)
            clock.sleep(0.6)
            board.brake_motor(motor)
            board.set_motor(motor, 0)
            clock.sleep(0.3)
            print(f"  [{OK}] {label:16s} {name} at {duty}% for 600ms")

    start = board.read_encoder_mm()
    if start is not None:
        board.set_motor(MotorId.DRIVE, settings.drive.homing_duty_pct)
        clock.sleep(1.0)
        board.set_motor(MotorId.DRIVE, 0)
        moved = (board.read_encoder_mm() or 0) - start
        if abs(moved) < 1.0:
            failures += 1
            print(f"  [{FAIL}] encoder did not change while driving — check the "
                  "sensor, the magnet ring and the wiring")
        else:
            print(f"  [{OK}] encoder moved {moved:+.1f}mm while driving")

    board.all_stop()
    return failures


def cmd_calibrate(args: argparse.Namespace, settings: Settings) -> int:
    """Measure the number v1 guessed.

    Drives home, then traverses the rail at a fixed duty and times it. The
    result is the only honest basis for open-loop motion.
    """
    if not args.yes and not args.sim:
        print("Calibration drives the carriage the full length of the rail.\n"
              "Clear the rail, then re-run with --yes.")
        return 2

    duty = args.duty or settings.drive.transit_duty_pct
    with _make_robot(settings, args) as robot:
        _install_signal_handlers(robot)
        robot.monitor.start()
        try:
            print(f"\nHoming, then traversing at {duty}% duty...")
            robot.axis.home()
            clock = robot.board.clock
            t0 = clock.now()
            result = robot.axis.move_to(
                settings.panel.rail_travel_mm, duty, "calibration traverse"
            )
            elapsed = clock.now() - t0
        except SafetyAbort as exc:
            print(f"\nCalibration aborted: {exc}\n")
            return 1
        finally:
            # The context manager stops the monitor and de-energises the board
            # on the way out; this just does it before the message is printed.
            robot.monitor.stop()
            robot.stop()

    if result.stopped_by != "limit":
        print("\nThe carriage did not reach the far limit switch, so the travel "
              "distance is unknown. Fix the switch and retry.\n")
        return 1

    travel = settings.panel.rail_travel_mm
    measured_mmps = travel / max(elapsed, 1e-6)
    at_full_duty = measured_mmps * 100.0 / duty

    print(f"\n  rail travel           {travel:.0f} mm (from settings)")
    print(f"  elapsed               {elapsed:.2f} s")
    print(f"  speed at {duty:>3d}% duty    {measured_mmps:.1f} mm/s")
    print("\nPut this in config/settings.yaml:\n")
    print("drive:")
    print(f"  duty_to_mmps: {at_full_duty:.1f}")
    print("\nThen measure the rail with a tape and correct panel.rail_travel_mm "
          "if it disagrees — every open-loop distance depends on both numbers.\n")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    from .web.app import serve

    if args.host:
        settings = replace(settings, web=replace(settings.web, host=args.host))
    if args.port:
        settings = replace(settings, web=replace(settings.web, port=args.port))
    settings.validate()

    with _make_robot(settings, args) as robot:
        _install_signal_handlers(robot)
        serve(robot, settings, with_scheduler=not args.no_scheduler)
    return 0


def cmd_runs(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    history = RunHistory(Path(settings.logging.telemetry_dir), limit=args.limit).load()
    if args.json:
        print(json.dumps(history, indent=2))
        return 0
    if not history:
        print(f"\nNo runs recorded in {settings.logging.telemetry_dir}\n")
        return 0
    print(f"\n  {'run':<18} {'outcome':<11} {'passes':<8} {'dur':<8} detail")
    print(f"  {'-' * 18} {'-' * 11} {'-' * 8} {'-' * 8} {'-' * 30}")
    for entry in history:
        passes = f"{entry.get('passes_completed', '?')}/{entry.get('passes_planned', '?')}"
        duration = entry.get("duration_s")
        duration_s = f"{duration:.0f}s" if isinstance(duration, (int, float)) else "-"
        detail = entry.get("detail") or entry.get("stop_reason") or ""
        print(f"  {entry.get('run_id', '?'):<18} {entry.get('outcome', '?'):<11} "
              f"{passes:<8} {duration_s:<8} {detail[:40]}")
    print()
    return 0


# ---------------------------------------------------------------------------


def _install_signal_handlers(robot: SolarSweepRobot) -> None:
    """Ctrl-C must stop the machine, not just the Python process."""

    def handler(signum, _frame):
        logger.warning("signal %s received — stopping", signum)
        robot.estop(f"signal {signum}")
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except ValueError:
            pass  # not on the main thread


COMMANDS = {
    "run": cmd_run,
    "status": cmd_status,
    "selftest": cmd_selftest,
    "calibrate": cmd_calibrate,
    "serve": cmd_serve,
    "runs": cmd_runs,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = _load(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.logging, verbose=args.verbose, quiet=args.quiet)

    try:
        return COMMANDS[args.command](args, settings)
    except KeyboardInterrupt:
        print("\ninterrupted — outputs de-energised", file=sys.stderr)
        return 130
    except SafetyAbort as exc:
        print(f"safety abort: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
