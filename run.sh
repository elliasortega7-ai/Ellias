#!/usr/bin/env bash
# Start the inverter simulator and the poller/dashboard together.
# Ctrl-C stops both.
set -e
cd "$(dirname "$0")"

python3 simulator/inverter_sim.py &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null' EXIT

sleep 1
python3 poller/poller.py
