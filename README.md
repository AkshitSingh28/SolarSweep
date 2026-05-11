# ☀️ Solar Panel Cleaner Robot

> An autonomous, rail-guided solar panel cleaning robot built with 3D-printed parts, DC motors, spring suspension, and a Raspberry Pi controller.

![CI](https://github.com/YOUR_USERNAME/solar-panel-cleaner/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi-red)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 📸 Overview

This robot autonomously cleans solar panels using a two-axis rail-guided mechanism:

- **X-axis** — Outer rectangular frame traverses the full length of the panel along steel rails
- **Z-axis** — Inner chassis moves up/down on two steel rods via linear bearings
- **Cleaning head** — Center rotating brush + mop assembly with spring suspension for adaptive surface contact
- **Electronics** — Raspberry Pi Zero 2W, relay module, digital timer, and a 3D-printed enclosure

---

## 🏗️ Hardware Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   SOLAR PANEL SURFACE                   │
└─────────────────────────────────────────────────────────┘
         ↑ Spring Suspension (4x corners)
┌────────────────────────────────┐
│     OUTER FRAME (X-axis)      │  ← Moves FORWARD / BACKWARD along rails
│  ┌──────────────────────────┐ │
│  │   INNER CHASSIS (Z-axis) │ │  ← Moves UP / DOWN on steel rods
│  │  ┌────────────────────┐  │ │
│  │  │  CLEANING HEAD     │  │ │  ← Rotating brush + mop
│  │  │  (Center motor)    │  │ │
│  │  └────────────────────┘  │ │
│  │  Linear Bearings (x2)    │ │
│  └──────────────────────────┘ │
└────────────────────────────────┘
```

### 3D Printed Components

| File | Description |
|------|-------------|
| `head.stl` | Main cleaning head housing |
| `motor_base_2_.stl` | DC motor mount base |
| `CE3V2NEO_node__1_.stl` | Frame corner node |
| `CE3V2NEO_nbearingode__1_.stl` | Bearing-integrated node |
| `bearinghod323.stl` | Linear bearing holder |
| `joint.stl` | Frame joint connector |
| `rodsupport.stl` | Steel rod support bracket |
| `arduino_caes.stl` / `aarCE3V2NEO_case_arduino.stl` | RPi/Arduino enclosure |
| `vents.stl` | Ventilation grille for electronics box |
| `sinfinity.stl` | Structural infinity-shaped connector |
| `CE3V2NEO_X-Belt-Holder-GT2.stl` | GT2 belt holder for drive system |
| `union_1__10___1___1_.stl` | Full assembly reference model |

### Electronics

| Component | Purpose |
|-----------|---------|
| Raspberry Pi Zero 2W | Main controller (Python runtime) |
| DC Motors (5×) | 4× corner drive/lift + 1× cleaning head |
| Relay Module (2-channel) | Water pump & auxiliary control |
| Digital Timer/Display | Standalone timing backup |
| Limit Switches (4×) | Rail end detection (front/rear/top/bottom) |
| Rain Sensor | Weather-based auto-disable |
| Dust Sensor (ADC) | Trigger cleaning by contamination level |
| IR Obstacle Sensor | Emergency stop on obstruction |
| MCP3008 ADC | Analog sensor reading via SPI |
| Spring Suspension (4×) | Panel surface conformance |
| Blue spring dampers (4×) | Vibration isolation |

---

## 📂 Project Structure

```
solar-panel-cleaner/
├── main.py                     # Entry point (auto/manual/schedule/web)
├── requirements.txt
├── .gitignore
├── config/
│   ├── settings.yaml           # All hardware & behavior configuration
│   └── __init__.py             # Settings dataclasses
├── src/
│   ├── core/
│   │   ├── robot.py            # Main robot orchestrator & state machine
│   │   └── scheduler.py        # Cron-based cleaning scheduler
│   ├── hardware/
│   │   ├── gpio_manager.py     # GPIO init/cleanup
│   │   ├── motors.py           # DriveMotor, LiftMotor, BrushMotor
│   │   ├── relay.py            # Water pump & relay control
│   │   └── sensors.py          # Limit switches, rain, dust, IR
│   ├── web/
│   │   ├── dashboard.py        # Flask + Socket.IO live dashboard
│   │   └── templates/
│   │       └── index.html      # Web UI
│   └── utils/
├── models/
│   └── stl/                    # All 3D-printable STL files
├── tests/
│   └── test_robot.py           # Pytest test suite (dry-run)
├── scripts/
│   └── install.sh              # Raspberry Pi setup script
├── docs/
│   └── wiring.md               # GPIO wiring reference
└── .github/
    └── workflows/
        └── ci.yml              # GitHub Actions CI
```

---

## 🚀 Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/solar-panel-cleaner.git
cd solar-panel-cleaner
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

On Raspberry Pi, also uncomment in `requirements.txt`:
```
RPi.GPIO>=0.7.1
spidev>=3.6
```

### 3. Configure hardware

Edit `config/settings.yaml` to match your GPIO wiring and panel dimensions.

### 4. Run

```bash
# Autonomous cleaning cycle
python main.py --mode auto

# Web dashboard (visit http://<pi-ip>:5000)
python main.py --mode web

# Scheduled mode (reads cron from settings.yaml)
python main.py --mode schedule

# Dry-run simulation (no hardware needed)
python main.py --mode auto --dry-run
```

---

## 🌐 Web Dashboard

Access the live control panel at `http://<raspberry-pi-ip>:5000`

Features:
- Real-time position tracking (X and Z axes)
- Start full cycle / quick pass
- Pause / Resume / Emergency Stop
- Live state updates via WebSocket

---

## 🧪 Testing

```bash
pytest tests/ -v
```

Tests run in dry-run mode (no hardware required).

---

## ⚙️ Cleaning Cycle

```
IDLE → INIT → HOME X+Z → CHECK SENSORS →
  [for each pass]:
    LIFT → START BRUSH + WATER → TRAVERSE →
    STOP BRUSH + WATER → LOWER →
  HOME X → IDLE
```

---

## 📋 GPIO Wiring Reference

See `docs/wiring.md` for full pin-by-pin wiring diagram.

| Signal | GPIO (BCM) |
|--------|-----------|
| Drive Motor IN1 | 17 |
| Drive Motor IN2 | 27 |
| Drive Motor EN (PWM) | 22 |
| Lift Motor IN1 | 23 |
| Lift Motor IN2 | 24 |
| Lift Motor EN (PWM) | 25 |
| Brush Motor IN1 | 5 |
| Brush Motor IN2 | 6 |
| Brush Motor EN (PWM) | 13 |
| Water Pump Relay | 16 |
| Limit Front | 18 |
| Limit Rear | 19 |
| Limit Top | 21 |
| Limit Bottom | 26 |
| Rain Sensor | 12 |
| IR Obstacle | 4 |

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgements

Built with 💛 using 3D printing, off-the-shelf hardware, and open-source Python.
