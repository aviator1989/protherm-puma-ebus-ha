#!/bin/bash
# Force-read FlowTemp/ReturnTemp every 5s so passive cache/MQTT don't go stale when the boiler is idle (flame off).
while true; do
  docker exec ebusd ebusctl read -f FlowTemp > /dev/null 2>&1
  docker exec ebusd ebusctl read -f ReturnTemp > /dev/null 2>&1
  sleep 5
done
