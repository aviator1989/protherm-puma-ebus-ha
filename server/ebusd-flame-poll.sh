#!/bin/bash
# Poll ebusd Flame/Modulation/HwcDemand/HwcWaterflow/WaterPressure every 3s for near-realtime dashboard.
# WaterPressure added 2026-07-12 to test the low-flow pressure-instability hypothesis for DHW overheat.
while true; do
  docker exec ebusd ebusctl read -f Flame > /dev/null 2>&1
  docker exec ebusd ebusctl read -f ModulationDesired > /dev/null 2>&1
  docker exec ebusd ebusctl read -f HwcWaterflow > /dev/null 2>&1
  docker exec ebusd ebusctl read -f HwcDemand > /dev/null 2>&1
  docker exec ebusd ebusctl read -f WaterPressure > /dev/null 2>&1
  sleep 3
done
