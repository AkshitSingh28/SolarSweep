# Bill of materials

Two columns matter here: what is **visible in the photographs** of the built
prototype, and what is **recommended** for a machine meant to run unattended.
Where they differ, the difference is explained.

Nothing in this file is a trace of the actual build — the wiring was never
documented. Confirm against your own machine.

---

## Structure

| Item | Qty | Notes |
|---|---|---|
| Aluminium extrusion frame | 1 | Surrounds the panel array; carries the rails |
| Stainless round rail, ~10–12 mm | 2 | Full length of the array, parallel, above the glass |
| Rail end blocks / bearing housings | 4 | Grey blocks at the frame corners |
| Acrylic carriage plate | 1 | ~6 mm, clear, visible in the photographs |
| Spring posts | 4 | Telescoping, blue-collared compression springs |
| Linear bearings / bushings | 4 | Carriage to rail |

The springs are the load path that replaces v1's imaginary lift axis: they
hold the brush against the glass and absorb the panel's surface variation.
Preload determines contact pressure — too little and the pads skim over dust,
too much and the drive motor labours and the pads wear fast.

## Motion

| Item | Qty | Photographed | Recommended |
|---|---|---|---|
| 12 V DC gearmotor | 4 | Four on the carriage | 2 drive + 2 brush, or 2 + 1 |
| Drive wheels / hubs | 2–4 | On the motor shafts | Knurled or O-ringed for grip |
| Rotary brush | 1 | Red bristle disc | |
| Microfiber mop pads | 2 | White, under the carriage | Replaceable |

> **Uncertainty worth resolving.** Four gearmotors are visible on the
> carriage. Two plausible readings: (a) four-wheel drive on the two rails with
> the brush driven separately, or (b) two rail-drive motors and two
> downward-facing brush/mop motors. Trace the wiring before configuring —
> the software drives one `drive` channel and one `brush` channel, and motors
> that share a channel must be wired in parallel *and* within the driver's
> current limit (see [WIRING.md](WIRING.md)).

## Electronics

| Item | Qty | Photographed | Notes |
|---|---|---|---|
| Raspberry Pi Zero 2 W | 1 | Yes | Ample for a 50 Hz control loop |
| Motor driver | 2 | Heatsinked boards, likely L298N | **Prefer BTS7960/IBT-2** — see WIRING.md |
| Relay module, 2-channel | 1 | Yes, blue, marked "Relay Module" | Active LOW; needs pull-ups |
| SMPS 12 V | 1 | Metal enclosure in the box | Mains in — keep covered |
| 5 V buck converter | 1 | — | Separate rail for the Pi, not shared |
| Digital timer / display module | 1 | Green 4-digit 7-segment | Standalone; **not used by this software** |
| Micro limit switch | 2 | — | Wire **normally closed** |
| E-stop, mushroom, latching | 1 | — | **Not fitted.** Fit one. NC contact. |
| Rain sensor | 1 | — | |
| IR obstacle sensor | 1 | — | |
| MCP3008 ADC | 1 | — | For the dust sensor, over SPI |
| Dust / particulate sensor | 1 | — | Optional; `triggers.dust_threshold: 0` disables |
| Hall sensor + magnet ring | 1 | **Not fitted** | See below |
| Water pump, 12 V | 1 | — | Tethered supply, not onboard |
| 10 kΩ resistor | 2 | — | Relay input pull-ups. Not optional. |

### What is missing from the prototype

Three items in that table are absent from the built machine and matter more
than anything else on this page:

**1. A wheel encoder.** Without a measured position, no software can detect a
stall — the dead-reckoned estimate keeps counting up while the carriage sits
pinned against a hard stop with the motor at locked-rotor current. This is why
`config/settings.as-built.yaml` refuses unattended runs. A hall sensor and a
magnet ring is the cheapest safety improvement available to this machine.
[COMMISSIONING.md](COMMISSIONING.md) step 6.

**2. A physical e-stop.** A software stop cannot help when the software is
what has gone wrong. A latching mushroom button in series with the motor
supply, with its NC auxiliary contact on `pins.estop_button`, cuts the motors
in hardware and tells the software why they stopped.

**3. Relay input pull-ups.** Two resistors. Without them the pump energises on
every reboot. [WIRING.md](WIRING.md).

### Worth adding

| Item | Why |
|---|---|
| ACS712 current sensor | Catches a jam in ~1 s rather than the encoder's 2.5 s. Set `safety.overcurrent_a`. |
| IP65 enclosure with downward glands | The control box sits centimetres from a water line |
| Inline water filter | Grit through the pump is grit onto the glass |
| Cable chain or retractor for the tether | A snagged tether is the most common way this class of machine kills itself |

---

## 3D printed parts

`models/stl/` holds seventeen STL files from the original build. The v1 README
listed twelve *different* filenames, none of which are in the repository —
that table was fabricated. `models/stl/README.md` has the real inventory with
measured bounding boxes.

Print in PETG or ASA, not PLA. PLA creeps under sustained load and softens in
the sun; a bracket on a rooftop panel sees both.
