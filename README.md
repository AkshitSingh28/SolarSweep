# SolarSweep

A rail-guided solar panel cleaning robot: a spring-loaded carriage rides two
stainless rails across a panel array, dragging a rotating brush and microfiber
mop with pulsed water.

This is **v2** — a rewrite of the control software. The v1 code in this repo's
history did not run: no entry point, no package structure, and a state machine
that drove a lift axis the physical machine does not have. What is here now
runs, is tested, and is honest about which parts of the hardware have been
verified and which have not.

```bash
pip install -e .
solarsweep run --sim                    # a full cleaning cycle, no hardware needed
solarsweep run --sim --fault "rain@30"  # …and one that gets rained on
```

---

## Status

| | |
|---|---|
| Control software | Working, 142 tests, runs against a simulator |
| Simulator | Working — kinematics, sensors, 10 injectable faults |
| Dashboard + scheduler | Working |
| Mechanical prototype | Built, **never commissioned** — see [docs/COMMISSIONING.md](docs/COMMISSIONING.md) |
| On-panel validation | Not done |

The prototype in `models/` and the photographs was assembled but never run
end to end. The gap between "assembled" and "working" is what
[docs/COMMISSIONING.md](docs/COMMISSIONING.md) exists to close, and the reason
`solarsweep selftest` and `solarsweep calibrate` are the first two commands
you should run on real hardware.

---

## How it works

```
        ┌──────────── carriage ────────────┐
        │  ╭──╮  ╭────────────╮  ╭──╮      │   4× spring posts hold the
   ═════╪══╡▓▓╞══╡  brush +   ╞══╡▓▓╞══════╪═  brush against the glass;
   ═════╪══╡▓▓╞══╡  mop head  ╞══╡▓▓╞══════╪═  no lift axis, no actuator
        │  ╰──╯  ╰────────────╯  ╰──╯      │
        └───────────────┬──────────────────┘
   home switch          │ tether: 12 V + water        far switch
   ●────────────────────┴───────────────────────────────────●
   0 mm                                            rail_travel_mm
```

One degree of freedom. The carriage homes against a switch, then makes
alternating passes along the rail with the brush turning and water pulsed on a
fixed cadence, then parks back at home. Every move is bounded by a deadline;
every interlock raises rather than returning a status a caller can ignore.

A cycle:

```
IDLE → PREFLIGHT → HOMING ─(first run)─ referencing traverse
                      ↓
              POSITIONING → CLEANING ─┐  ×N passes
                      ↑───────────────┘
                      ↓
                   PARKING → IDLE

any state ──(interlock)──→ FAULTED / ESTOPPED ──(operator ack)──→ IDLE
```

---

## Commands

| Command | What it does |
|---|---|
| `solarsweep run [--sim]` | One cleaning cycle |
| `solarsweep selftest` | Reads every sensor and reports what looks wrong |
| `solarsweep selftest --motion --yes` | …and pulses the relays and jogs the motors |
| `solarsweep calibrate --yes` | Times a full traverse and prints the `duty_to_mmps` to configure |
| `solarsweep serve` | Dashboard on `:5000` plus the cron scheduler |
| `solarsweep runs` | Summaries of past runs |
| `solarsweep status` | One JSON snapshot |

Global flags: `-c/--config`, `--sim`, `--time-scale`, `-v`, `-q`.

### Fault injection

The simulator will misbehave on demand, which is how the safety paths get
tested without risking the machine:

```bash
solarsweep run --sim --fault stuck_limit_far      # a dead limit switch
solarsweep run --sim --fault "drive_stall@900"    # jams after 900 mm
solarsweep run --sim --fault "rain@30"            # rain starts 30 s in
solarsweep run --sim --fault "obstacle@1200"      # something on the rail
solarsweep run --sim --fault "estop@12,no_water"  # several at once
```

Full list: `stuck_limit_home`, `stuck_limit_far`, `drive_stall`, `brush_stall`,
`rain`, `obstacle`, `estop`, `no_water`, `overcurrent`, `encoder_dead`.

---

## Layout

```
solarsweep/
├── config.py            settings dataclasses + validation
├── safety.py            e-stop latch, deadlines, stall detector, watchdog
├── telemetry.py         per-run JSONL trace
├── hal/                 hardware abstraction
│   ├── base.py            the Board interface
│   ├── rpi.py             Raspberry Pi + L298N + relay board
│   └── sim.py             kinematic simulator with fault injection
├── control/
│   ├── axis.py            rail motion, homing, referencing
│   ├── cleaning_head.py   brush + pulsed water
│   ├── states.py          state machine and legal transitions
│   └── robot.py           cycle orchestration
├── web/                 Flask dashboard (SSE, no socketio)
├── scheduling.py        cron jobs
└── cli.py

config/
├── settings.yaml           recommended build (encoder fitted)
└── settings.as-built.yaml  the prototype as photographed

docs/
├── V1_AUDIT.md          what was wrong with v1 and why
├── COMMISSIONING.md     bring-up procedure for the physical machine
├── WIRING.md            pin map and wiring notes
├── BOM.md               parts, with what is confirmed vs assumed
├── ARCHITECTURE.md      why the code is shaped this way
└── COMMERCIAL.md        what this could be sold as, and what would have to change
```

---

## Configuration

Two files ship. `config/settings.yaml` describes the **recommended** build —
the prototype plus a drive encoder. `config/settings.as-built.yaml` describes
the machine exactly as photographed, with no encoder:

```bash
solarsweep -c config/settings.as-built.yaml run --sim
```

Run that and it warns that stall detection is unavailable, then refuses any
unattended run. That is deliberate. Without a measured position, software
cannot distinguish "travelling along the rail" from "pinned against a hard
stop with the motor stalled". Fitting one hall sensor and a magnet ring closes
the gap; see [docs/COMMISSIONING.md](docs/COMMISSIONING.md) step 6.

Every key in `settings.yaml` is read by running code. Unknown keys are a load
error, not a shrug.

---

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

Tests run against the simulator at `time_scale=0`, so the full suite —
including several complete cleaning cycles — takes about two seconds.

---

## Safety

This machine moves several kilograms along a rail above glass, drives a
mains-powered pump, and is meant to be left alone on a roof. Before it runs
unsupervised:

- Wire the limit switches and the e-stop **normally closed**, so a cut wire
  reads as "triggered" rather than "all clear".
- Fit the 10 kΩ pull-ups on the relay inputs. Between power-on and this
  software starting, the Pi's pins are inputs with weak pull-downs, which an
  active-LOW relay board reads as *on*.
- Fit the encoder. See above.
- Run `solarsweep selftest` and fix everything it reports.
- Do the first `solarsweep run` with a finger on the e-stop.

[docs/COMMISSIONING.md](docs/COMMISSIONING.md) is the ordered version of this.

---

## Licence

MIT — see [LICENSE](LICENSE).
