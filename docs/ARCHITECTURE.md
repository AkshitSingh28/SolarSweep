# Architecture

Why the code is shaped this way. Each decision below is a response to
something concrete in [V1_AUDIT.md](V1_AUDIT.md).

```
   cli.py ── web/app.py ── scheduling.py        entry points
        └────────┬────────────┘
                 ▼
       control/robot.py                         cycle orchestration
        ├── control/states.py                   legal transitions, pause gate
        ├── control/axis.py                     rail motion
        ├── control/cleaning_head.py            brush + pulsed water
        ├── safety.py                           latch, deadlines, stall, watchdog
        └── telemetry.py                        JSONL run trace
                 ▼
            hal/base.py                         the Board interface
             ├── hal/rpi.py                     GPIO, PWM, SPI
             └── hal/sim.py                     kinematics + fault injection
```

## The HAL exists so the machine can be tested

The single most important fact about v1 is that it was never run. Not because
of laziness — because running it required a person standing next to a machine
on a roof. Every safety path in it was written blind.

`hal/base.Board` is the seam. Above it, nothing knows what a GPIO pin is.
Below it there are two implementations: the Pi, and a kinematic model of the
carriage. The same `control/` code drives both, so the test suite exercises
the real state machine, the real timeouts and the real interlocks — a full
cleaning cycle in about 20 ms.

Two conventions in the interface pay for themselves repeatedly:

- **Signed duty.** One number, `[-100, 100]`, direction is the sign. v1 had
  `forward()`, `backward()` and `set_speed()` as separate calls that could
  disagree with each other.
- **Logical inputs.** `read_input()` returns "is this asserted", not "is this
  pin high". Active-low, normally-closed and inverted-meaning sensors are all
  resolved in the backend. The state machine never reasons about polarity.

## Time is injectable

`Board.clock` is a `Clock`, not `time.monotonic`. Every timeout in the control
layer measures against it. The simulator's clock is virtual: `sleep()` advances
it and steps the physics, so a 90-second homing timeout is reachable in a test
in microseconds.

The one deliberate exception is `Watchdog`, which uses wall-clock time. A
wedged process is a wall-clock event, and a watchdog that only fires when
petted would be useless.

## Interlocks raise; they do not return

v1's `emergency_stop()` returned, and its callers ignored the return value and
kept cleaning. `SafetyMonitor.tick()` raises `SafetyAbort` instead. There is no
way to forget to check it, and the one place that catches it —
`SolarSweepRobot.run_cycle` — records the reason and transitions to a fault
state. It never resumes motion.

`EStopLatch` is separate from the state machine on purpose. A state can be
transitioned out of; a latch has to be *cleared*, and it refuses to clear while
the physical e-stop is still asserted.

## Every wait is a Deadline with a purpose

```python
deadline = Deadline(clock, timeout_s, "homing to the near end stop")
```

The string is not decoration — it is what appears in the abort message, the
log and the telemetry, and it is the difference between "timeout" and knowing
which end of the machine to walk to. Timeouts for traverses are derived from
the predicted duration (`distance / (duty × duty_to_mmps)`) times a margin, so
they scale with the configured machine rather than being a magic number.

## Stall detection needs measured position, and says so when it has none

The subtle one. Dead reckoning keeps counting up while the carriage sits
pinned against a hard stop — the estimate and reality diverge precisely when
you most need them to agree. So `RailAxis.measured_position_mm` returns `None`
without an encoder, `StallDetector` is disarmed rather than fed a fiction, and
`stall_protection_available` is `False`.

The robot then refuses **unattended** runs while still permitting supervised
ones, because an operator with a finger on the e-stop is a valid substitute for
an encoder. That distinction is enforced in `_preflight`, and there are tests
for both halves.

## The referencing run

A cleaning pass stops on a *position* target (`rail_travel - lead_out`), which
is short of the far limit switch. So during normal operation the far switch is
never exercised, and a dead one stays invisible until the day something else
fails.

`RailAxis.reference()` runs once per process: home, then drive to the far stop
and confirm it asserts. It also measures the rail, which catches the other
common error — `panel.rail_travel_mm` copied from a panel datasheet instead of
measured switch-to-switch. Both failures produce a specific message naming what
to check.

## State transitions are a table

`TRANSITIONS` maps each state to what may follow it, and `StateMachine.to()`
raises on anything else. This is how v1's dead pause button would have been
caught: it was reachable only from `CLEANING` while the loop that read it ran
in `TRAVERSING`, and nothing checked.

`force()` exists for exactly one caller — e-stop, which must be reachable from
any state including ones nobody anticipated.

## Telemetry is per-run JSONL

Appendable, tailable, greppable, and readable with `jq`. A run that ends
without a `run_end` record is reported as `interrupted` rather than silently
omitted, because "the process died mid-cycle" is information.

## What is deliberately not here

- **No PID loop.** The carriage moves at a few cm/s along a straight rail with
  a limit switch at each end. Ramped open-loop duty with encoder feedback for
  position is sufficient, and a control loop nobody can tune is worse than
  none.
- **No socketio.** Server-sent events over plain Flask do the same job with no
  eventlet monkey-patching and no extra dependency on a Pi Zero.
- **No camera or computer-vision dirt detection.** The v1 infographic
  advertised both. Neither existed, and neither earns its place before the
  machine can reliably complete a pass.
- **No lift axis.** The machine does not have one. See
  [V1_AUDIT.md](V1_AUDIT.md) §3.
