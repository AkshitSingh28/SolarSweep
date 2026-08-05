# Commissioning

The prototype was assembled and never run. This is the ordered procedure for
closing that gap. Work through it in order; each step assumes the previous one
passed.

Budget roughly a day, most of it in steps 2 and 6.

> **Before anything else.** The carriage is several kilograms travelling above
> glass, with a mains-powered pump and a mains SMPS in the control box. Do
> steps 1–5 with the panel on the ground or on a bench, never on a roof.

---

## 1. Software, on a laptop

Nothing to do with the machine yet. Confirm the control software behaves.

```bash
git clone <this repo> && cd SolarSweep
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                       # expect 142 passed
solarsweep run --sim            # expect a completed cycle
```

Then watch it fail properly:

```bash
solarsweep run --sim --fault stuck_limit_far
solarsweep run --sim --fault "drive_stall@900"
```

Both should abort with a specific diagnosis and leave the state `faulted`. If
they do not, stop here — the safety layer is what makes the rest of this
procedure survivable.

## 2. Control box, powered, motors disconnected

**Disconnect the motor outputs from the driver boards.** Everything in this
step is about proving the wiring before anything can move.

1. Flash Raspberry Pi OS Lite, enable SSH and SPI (`sudo raspi-config nonint do_spi 0`).
2. `bash scripts/install.sh`
3. Wire per [WIRING.md](WIRING.md). Pay attention to two things:
   - **Limit switches and the e-stop must be normally closed.** Wired
     normally-open, a cut or corroded wire reads as "all clear" — the failure
     points the wrong way. NC means a broken wire reads as triggered and the
     machine refuses to move.
   - **Fit the 10 kΩ pull-ups on the relay inputs.** Between power-on and the
     software starting, the Pi's GPIOs are inputs with weak pull-downs, which
     an active-LOW relay board reads as *on*. Without the pull-up, the pump
     runs every time the Pi reboots.
4. Copy `config/settings.as-built.yaml` to `config/settings.yaml` and correct
   the pin numbers to match what you actually wired.

Then:

```bash
solarsweep selftest
```

Fix everything it reports. Work through each input by hand — press each limit
switch, press the e-stop, wet the rain sensor, block the obstacle sensor — and
re-run after each, confirming the right line changes. A switch that reads
correctly but is wired to the wrong pin will pass a static check and fail on
the roof.

Common findings at this stage:

| Symptom | Cause |
|---|---|
| Both limit switches assert at once | Shared ground gone open, or a short |
| `dust sensor reads 0` | SPI not enabled, or MCP3008 CS on the wrong pin |
| `water pressure: none` | Supply off, inline filter blocked, or sensor inverted |
| e-stop reads clear when pressed | Wired normally-open — rewire it |

## 3. Actuators, still on the bench

Reconnect the motors with the carriage **off the rails**, or with the rails
blocked so it cannot travel.

```bash
solarsweep selftest --motion --yes
```

You should hear each relay click, and see each motor turn briefly in both
directions. Check:

- The drive motor turns **toward the far end** when the test says "forward".
  If not, swap its two motor leads — not the code.
- The brush turns the direction that sweeps debris off the panel rather than
  into the frame.
- Nothing gets hot. An L298N driving a 12 V gearmotor is close to its limit;
  see [BOM.md](BOM.md) on why BTS7960 is the better part here.

## 4. First motion on the rails, supervised

Carriage on the rails, panel flat and on the ground, water disconnected,
**hand on the e-stop.**

```bash
solarsweep calibrate --yes
```

This homes, then traverses the rail once at transit duty and times it. It
prints a `duty_to_mmps` value. Two things to check:

- **Measure the rail with a tape**, switch to switch, and put that number in
  `panel.rail_travel_mm`. Every open-loop distance depends on it.
- Put the printed `duty_to_mmps` into `drive.duty_to_mmps`.

Re-run `calibrate` after editing. The second run should agree with the first
to within a few percent. If it does not, something is slipping.

## 5. First dry cycle

Water still disconnected. `cleaning.dry_pass: true`.

```bash
solarsweep run
```

Watch the whole thing. What you are looking for:

- It homes cleanly and the referencing traverse finds the far switch.
- The brush spins up **before** the carriage starts moving.
- The carriage does not judder at the start of a move — if it does, raise
  `drive.ramp_time_s`.
- The mop pads maintain contact across the full span. If they lift in the
  middle, the springs need more preload or the rails are bowing.
- It parks back at home.

Then read the trace:

```bash
solarsweep runs
cat logs/runs/run-*.jsonl | jq -c 'select(.kind=="pass_complete")'
```

## 6. Fit the encoder

**This is the step that turns a supervised toy into something you can leave
alone**, and it is the single most valuable upgrade to the as-built machine.

Without a measured position, software cannot tell "travelling along the rail"
apart from "pinned against a hard stop with the motor stalled" — the
dead-reckoned estimate keeps counting up either way. That is why
`settings.as-built.yaml` refuses unattended runs.

What to fit: a hall-effect sensor (A3144 or similar) and a small magnet ring
on the drive wheel or motor shaft. One channel is enough; two gives direction.

1. Mount the sensor, wire it to `pins.encoder_a` (and `encoder_b` if fitted).
2. Work out ticks per mm: `magnets_per_revolution / (π × wheel_diameter_mm)`.
   Put it in `drive.encoder_ticks_per_mm`.
3. Verify it: `solarsweep selftest --motion --yes` reports how far the encoder
   thought the carriage moved.
4. Sanity-check against the rail: home, `calibrate`, and confirm the encoder
   distance matches the tape measure.

Then re-run step 5. `solarsweep status` should now report
`"position_source": "encoder"` and `"stall_protection": true`.

Optional but worth it: an ACS712 current sensor on the drive motor supply,
with `safety.overcurrent_a` set to about 1.5× the motor's running current.
It catches jams the encoder would only notice 2.5 s later.

## 7. Water

Reconnect the supply. `cleaning.dry_pass: false`.

Run one cycle and watch the pulses. `cleaning.water_pulse_ms` and
`water_interval_s` control how much water lands; the defaults (600 ms every
8 s) are a starting point, not a recommendation. Too much and it runs off the
panel and dries as mineral streaks, which is worse than the dust. Too little
and the brush works dry.

Check afterwards that the pump relay is open and the line is not dribbling.

## 8. On the panel, in place

Only now. Mount the frame, run one supervised cycle with a hand on the e-stop,
and check:

- Tilt matters. On an inclined panel the carriage will run away downhill if
  the drive coasts. `brake_motor()` shorts the winding to hold it — confirm it
  actually holds at your tilt angle before trusting it.
- The tether does not snag at either end of travel. This is the most common
  way this class of machine kills itself.
- Water goes on the panel, not into the frame, the junction box, or the
  connectors.

## 9. Unattended

Only after several supervised cycles have completed cleanly, and only with the
encoder fitted.

```yaml
scheduler:
  enabled: true
  jobs:
    - cron: "30 6 * * *"
      mode: "full_cycle"
```

```bash
sudo systemctl enable --now solarsweep
```

Early morning is right: the glass is cool, so water does not flash-dry into
streaks, and the panel is generating little enough that shading it costs
nothing.

Then leave it alone for a week and read `solarsweep runs` before trusting it
further. `safety.max_consecutive_faults` will latch the machine out rather
than let the scheduler retry a jam every morning.

---

## When something goes wrong

Every run leaves `logs/runs/run-<timestamp>.jsonl`. Start there:

```bash
solarsweep runs                                        # summaries
jq -c 'select(.kind|test("state|pass|run_end"))' logs/runs/run-*.jsonl
```

The abort message names the interlock and what to check. A `stall` at a
plausible position is usually mechanical; a `stall` right at one end is
usually the limit switch at that end.
