#!/bin/bash
# Raspberry Pi setup script for Solar Cleaner Robot
set -e

echo "Installing Solar Panel Cleaner Robot..."

sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv git

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
pip install RPi.GPIO spidev

# Enable SPI for MCP3008 dust sensor
sudo raspi-config nonint do_spi 0

# Create logs directory
mkdir -p logs

# Create systemd service
sudo tee /etc/systemd/system/solar-cleaner.service > /dev/null << SERVICE
[Unit]
Description=Solar Panel Cleaner Robot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/venv/bin/python main.py --mode schedule
Restart=on-failure

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable solar-cleaner
echo "Done! Run: sudo systemctl start solar-cleaner"
