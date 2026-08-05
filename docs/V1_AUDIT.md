# Audit of v1

The v1 code was generated from notes about a half-finished project. It reads
convincingly — docstrings, a state machine, a config schema, an infographic —
and none of it ran. This document records what was actually wrong, because
several of the faults are the kind that look like working code right up until
a motor is connected.

Everything below is recoverable from git history (`git log`, branch `main`).

---

## 1. It could not be imported, let alone run

| Symptom | Detail |
|---|---|
| No entry point | `main.py` was an empty file. The README, the install script and the systemd unit all invoked `python main.py --mode schedule`. |
| Real code in a typo'd file | `src/core/robot.py` was empty; the 324-line orchestrator lived in `src/core/robott.py`, which nothing imported. |
| No packages | No `__init__.py` anywhere under `src/`, so `from src.hardware.motors import …` could not resolve. |
| Case mismatch | Code did `from config import Settings`; the directory was `Config/`. Works on a case-insensitive Mac, fails on the Pi. |
| Broken module | `Config/settings.py` contained one line: `from config import Settings`. |
| Wrong filename | The loader wanted `config/__init__.py`; the file was committed as `Config/config__init__.py`. |

Confirmed:

```
$ python -c "from src.core.robott import SolarCleanerRobot"
ModuleNotFoundError: No module named 'config'
```

## 2. The README described a repository that did not exist

Claimed and absent: `tests/test_robot.py`, `.github/workflows/ci.yml`,
`src/web/templates/index.html`, `.gitignore`, `src/utils/`. The dashboard
called `render_template("index.html")`, so `/` would have raised
`TemplateNotFound` on the first request.

The STL table listed twelve files by name — `head.stl`, `motor_base_2_.stl`,
`sinfinity.stl`, `arduino_caes.stl` and others. **None of them are in
`models/stl/`.** The seventeen files that *are* there have names like
`CE3V2NEO_difference_1 (5).stl` and `ffggh.stl`, and are not mentioned
anywhere.

## 3. The software modelled a machine that was never built

This is the most important one.

v1 was built around two axes: X along the rail, and a **Z lift** that raised
the chassis on steel rods via linear bearings before each pass, waiting on
`limit_switch_top` and `limit_switch_bottom`.

The photographs show no lift axis. The carriage rides two round rails on
spring-loaded posts at four corners; the springs hold the brush against the
glass passively. There is nothing to raise and nothing to lower.

So `LiftMotor`, `_lift_to_clean()`, `_lower_chassis()`, `_home_z()`, the
`LIFTING`/`LOWERING` states, and two of the four limit switches all referred to
hardware that does not exist. Had the rest of the code run, the first cycle
would have blocked forever inside:

```python
while not self.sensors.limit_top():
    time.sleep(0.05)
```

driving a motor that was not connected, waiting on a switch that was not
fitted, with no timeout.

## 4. Position was computed from incompatible units

```python
speed    = self.settings.robot.cleaning_speed_mmps   # mm/s
duration = distance_mm / speed                       # seconds — fine so far
...
self.drive_motor.set_speed(int(speed * 100 / self.settings.robot.travel_speed_mmps))
```

`set_speed` writes a **PWM duty cycle**. The code divides one speed by another
to produce 33% duty, then separately assumes the carriage covers
`distance_mm` in `duration` seconds. Those two numbers have no relationship to
each other. The carriage travelled for a fixed time at an arbitrary speed and
the software recorded it as having covered the panel.

`_lift_to_clean()` had the same shape: it accepted a `lift_height_mm` target,
ignored it, drove until the top switch closed, and then assigned
`self._position_z_mm = target` regardless of where it actually was.

**v2:** `drive.duty_to_mmps` is a measured constant produced by
`solarsweep calibrate`, position is measured by encoder where one is fitted,
and every limit switch contact re-zeroes the estimate.

## 5. Nothing bounded any motion

Every motion loop had this shape, with no timeout, no stall check, and no
maximum travel:

```python
self.drive_motor.backward()
while not self.sensors.limit_rear():
    time.sleep(0.05)
```

A limit switch with a broken wire, a corroded contact, or a bracket knocked
out of alignment means the motor drives the carriage into the hard stop until
someone notices. The switches were also wired normally-open, so a **cut wire
reads as "not triggered"** — the failure mode points the wrong way.

**v2:** every wait is a `Deadline` with a stated purpose; switches are wired
normally-closed; a `StallDetector` cuts power when commanded motion produces
no measured motion.

## 6. The emergency stop was advisory

```python
if self._check_obstacle():
    self.emergency_stop()
    return
```

`emergency_stop()` logged, set an enum, and stopped the motors. `_home_x()`
returned. `run_full_cycle()` ignored the return value and carried straight on
to the next step — lifting, starting the brush, and traversing again. The stop
was undone by the next command.

There was also no way to *stay* stopped: `RobotState.EMERGENCY_STOP` was
overwritten by the next `_set_state()` call.

**v2:** interlocks raise `SafetyAbort`, which cannot be ignored by forgetting a
return value. `EStopLatch` persists until an operator clears it, and refuses to
clear while the e-stop input is still asserted.

## 7. Pause was unreachable code

```python
def pause(self):
    if self.state == RobotState.CLEANING:      # only from CLEANING
        self._set_state(RobotState.PAUSED)
```

but the traverse loop that checked for `PAUSED` ran with the state set to
`TRAVERSING` — `_traverse()` sets it on entry. Pressing pause during a pass did
nothing at all. Nothing caught this because nothing checked transitions.

**v2:** `TRANSITIONS` is an explicit table, illegal transitions raise, and pause
is a gate that every motion loop passes through on each tick. There is a test
that pauses mid-traverse and asserts the carriage does not move.

## 8. Half of settings.yaml was decoration

Declared, parsed into dataclasses, and never read by any code:
`water_pulse_ms`, `water_interval_s`, `overlap_mm`, `panel_width_mm`,
`suspension.spring_preload_mm`, `suspension.max_compression_mm`,
`sensors.obstacle_stop`, `logging.*` (see below), `web.secret_key`,
`web.enable_camera_feed`.

`dust_threshold` was read, logged, and then ignored — the cycle ran regardless.
The pump was switched on for the whole pass rather than pulsed.

**v2:** unknown keys are a load error, and there is a test asserting every
section round-trips.

## 9. Logging was configured nowhere

Every module did `logger = logging.getLogger(__name__)` and called
`logger.info(...)`. No `basicConfig`, no handler, no file. With no handler
attached, everything below `WARNING` was discarded. `settings.yaml` specified
`logging.file: logs/robot.log`; nothing read it.

For a machine you cannot stand next to, the log is the only instrument panel.

**v2:** `logging_setup.py`, plus a per-run JSONL telemetry trace.

## 10. Smaller things

- `install.sh` was committed twice, at the repo root and in `scripts/`.
- `scripts/g` was an empty file.
- The scheduler used `BlockingScheduler`; the dashboard used
  `socketio.run()`. Both block, so they could never share a process — which
  did not matter, because `main.py` never started either.
- `RelayController` set `initial=GPIO.HIGH` for active-LOW relays, which is
  right, but there is a window between Pi power-on and this code running where
  the pins are inputs with pull-downs and the relays close. That needs a
  hardware pull-up; see [WIRING.md](WIRING.md).
- Flask-SocketIO + eventlet is a heavy and fragile dependency on a Pi Zero.
  v2 uses server-sent events over plain Flask.
- The infographic (`flowchrtt.png`, kept in the repo root) claims a 12 V
  battery pack. The photographs show a mains SMPS in the control box and a
  power cable trailing off the roof — the machine is tethered. The same poster
  advertises a watchdog timer, a camera feed and a "smart automation" dust
  trigger; none of the three existed in code. It is a good-looking picture of
  a machine that was never built, and worth keeping only as a record of the
  gap between the pitch and the artefact.

---

## What survived

The mechanical design, which is the hard part and the part that was actually
built. The STL files, the rail-and-spring concept, the pulsed-water idea, and
the overall cycle shape are all sound. v1's problem was never the idea.
