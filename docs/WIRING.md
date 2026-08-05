# Wiring

Pin numbers below are **BCM**, matching `pins:` in `config/settings.yaml`.
If you wire it differently, change the config — do not change the code.

> The pin map is a *proposal* consistent with the photographed control box,
> not a trace of it. Before powering anything, confirm each connection against
> your own build and correct `settings.yaml`. `solarsweep selftest` will tell
> you when a switch is on the wrong pin.

## Pin map

| Signal | BCM | Physical | Notes |
|---|---|---|---|
| Drive IN1 | 17 | 11 | H-bridge direction |
| Drive IN2 | 27 | 13 | |
| Drive EN | 22 | 15 | PWM, 1 kHz |
| Brush IN1 | 5 | 29 | |
| Brush IN2 | 6 | 31 | |
| Brush EN | 13 | 33 | PWM, 1 kHz |
| Pump relay | 16 | 36 | active LOW, **needs a pull-up** |
| Aux relay | 20 | 38 | active LOW, **needs a pull-up** |
| Limit · home | 19 | 35 | normally closed |
| Limit · far | 18 | 12 | normally closed |
| E-stop | 4 | 7 | normally closed |
| Rain sensor | 12 | 32 | |
| Obstacle sensor | 23 | 16 | |
| Water pressure | 24 | 18 | |
| Encoder A | 25 | 22 | hall sensor, see COMMISSIONING step 6 |
| Encoder B | 26 | 37 | optional, gives direction |
| MCP3008 MOSI | 10 | 19 | SPI0 |
| MCP3008 MISO | 9 | 21 | SPI0 |
| MCP3008 SCLK | 11 | 23 | SPI0 |
| MCP3008 CE0 | 8 | 24 | SPI0 |

Set `encoder_a: -1` and `encoder_b: -1` when no encoder is fitted.

---

## The three things that matter

### 1. Limit switches and the e-stop must be normally closed

```
        3V3
         │
        10k          (internal pull-up, GPIO.PUD_UP)
         │
   GPIO ─┼───────────┐
         │           │
         └──── NC switch ──── GND
```

Undisturbed, the switch is closed, the pin is pulled to ground, and reads LOW.
Press the switch — or cut the wire, or corrode a contact — and the pin floats
up to 3V3 and reads HIGH. Both "triggered" and "broken" read the same, and the
software treats both as stop.

Wired normally-open (as v1 specified) it is backwards: a broken wire reads as
"all clear" and the machine keeps driving into whatever the switch was there
to protect it from.

The polarity is handled in `hal/rpi.py`, controlled by
`pins.limits_normally_closed`. The control layer only ever sees "asserted".

### 2. The relay board needs a hardware pull-up

The common blue 2-channel boards are **active LOW**: pulling the input to
ground energises the relay.

From power-on until this software runs — several seconds of boot — the Pi's
GPIOs are inputs with weak internal pull-downs. The relay board reads that as
LOW, and closes. **The pump runs on every reboot until the software starts.**

`GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)` closes the window once we are
running, but it cannot help during boot. Fit a 10 kΩ resistor from each relay
input to +3V3:

```
   3V3 ──10k──┬── relay IN
              │
    GPIO ─────┘
```

Now the input idles HIGH (relay open) whatever the Pi is doing.

Better still, power the pump through a separate contactor that the Pi has to
actively hold closed.

### 3. Motor driver current

An L298N drops about 2 V across its bridge and is rated ~2 A per channel with
a heatsink. A 12 V DC gearmotor of the size in the photographs draws roughly
0.3–0.8 A running and 2–3 A stalled. That is marginal, and a stall is exactly
when you least want the driver to give up.

If two motors share one channel, the numbers stop working entirely.

Prefer a **BTS7960** (43 A, ~0.1 V drop) or an IBT-2 module for the drive. The
interface is the same three pins, so no code changes.

---

## Power

```
   mains ──▶ SMPS 12 V ──┬──▶ motor drivers ──▶ motors
                         │
                         ├──▶ pump relay ──▶ pump
                         │
                         └──▶ buck 5 V ──▶ Raspberry Pi
```

- Star-ground at the SMPS. Do not daisy-chain motor ground through the Pi.
- The Pi needs its own regulator, not a shared rail with the motors. Motor
  inrush pulls the rail down far enough to brown out a Pi Zero mid-write and
  corrupt the SD card.
- A 470 µF electrolytic across the motor supply at the driver, and a 100 nF
  ceramic across each motor's terminals, keeps the brush noise out of
  everything else.
- The machine in the photographs is **tethered** — mains power and a water
  hose trail off the frame. It is not battery powered, whatever the v1
  infographic says. Route the tether so it cannot snag at either end of travel.

## Outdoors

The control box needs to be IP65 or better with the glands facing down.
Everything in it is unprotected electronics a few centimetres from a water
line. The ceramic terminal blocks visible in the photographs are for mains and
must stay covered.
