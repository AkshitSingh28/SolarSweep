#!/usr/bin/env bash
# Raspberry Pi setup for SolarSweep.
#
# Installs the package, enables SPI for the dust-sensor ADC, and creates a
# systemd unit. It deliberately does NOT enable or start the service, which v1's
# installer did: the machine must be commissioned first (docs/COMMISSIONING.md),
# and a robot that starts cleaning the moment you finish installing it is a
# robot that surprises whoever is still standing next to it.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_DIR/.venv"
SERVICE=/etc/systemd/system/solarsweep.service

echo "==> SolarSweep install"
echo "    repo: $REPO_DIR"

if ! grep -qi "raspberry\|BCM" /proc/cpuinfo 2>/dev/null; then
  echo
  echo "    This does not look like a Raspberry Pi."
  echo "    On a laptop you want:  pip install -e '.[dev]'  &&  solarsweep run --sim"
  echo
  read -r -p "    Continue anyway? [y/N] " reply
  [[ "$reply" == [yY] ]] || exit 1
fi

echo "==> System packages"
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv python3-dev git

echo "==> Virtualenv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -e "$REPO_DIR"

echo "==> GPIO libraries"
# Kept separate: these fail to build off-Pi, and a failure here should not
# take the rest of the install down with it.
if ! "$VENV/bin/pip" install -e "$REPO_DIR[hardware]"; then
  echo "    WARNING: RPi.GPIO/spidev did not install."
  echo "    The simulator still works; real hardware will not."
fi

echo "==> Enabling SPI (MCP3008 dust sensor)"
sudo raspi-config nonint do_spi 0 || echo "    could not enable SPI automatically"

mkdir -p "$REPO_DIR/logs/runs"

if [[ ! -f "$REPO_DIR/config/settings.yaml" ]]; then
  cp "$REPO_DIR/config/settings.as-built.yaml" "$REPO_DIR/config/settings.yaml"
  echo "==> Created config/settings.yaml from the as-built template"
fi

echo "==> systemd unit"
sudo tee "$SERVICE" > /dev/null <<UNIT
[Unit]
Description=SolarSweep panel cleaning robot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO_DIR
Environment=PYTHONUNBUFFERED=1
# Bind the dashboard beyond loopback only with a token set:
#   Environment=SOLARSWEEP_WEB_TOKEN=<something long>
ExecStart=$VENV/bin/solarsweep serve
# On any failure the process exits; systemd restarts it, and the robot
# re-homes before it will move. It will not resume a half-finished pass.
Restart=on-failure
RestartSec=30
# Belt and braces alongside the in-process signal handlers: SIGTERM reaches
# the process, which de-energises every output before exiting.
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload

cat <<'DONE'

==> Installed.

    The service is NOT enabled. Commission the machine first:

      .venv/bin/solarsweep selftest              # sensors
      .venv/bin/solarsweep selftest --motion --yes
      .venv/bin/solarsweep calibrate --yes       # measure duty_to_mmps
      .venv/bin/solarsweep run                   # first supervised cycle

    Full procedure: docs/COMMISSIONING.md

    When you trust it:

      sudo systemctl enable --now solarsweep

DONE
