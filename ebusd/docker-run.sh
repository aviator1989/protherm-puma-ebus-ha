#!/bin/sh
# ebusd для Protherm Puma Condens через адаптер ebusd v5-C6 (TCP, enhanced-режим).
# ADAPTER_IP — IP адаптера в локальній мережі.
ADAPTER_IP=192.168.1.50

docker run -d --name ebusd --network host --restart unless-stopped \
  -v /opt/ebusd/config:/etc/ebusd/config \
  john30/ebusd:latest \
  --device=ens:${ADAPTER_IP}:9999 \
  --scanconfig --configpath=https://ebus.github.io/ \
  --latency=50000 --pollinterval=5 \
  --mqtthost=127.0.0.1 --mqttport=1883 --mqttjson --mqtttopic=ebusd \
  --mqttclientid=ebusd --mqttchanges --mqttretain \
  --mqttint=/etc/ebusd/config/mqtt-hassio.cfg --mqttverbose \
  '--mqttvar=filter-direction=r|u|^w' \
  --httpport=8889 --enabledefine \
  --accesslevel=install   # без цього SetMode мовчки не записується
