#!/usr/bin/env python3
"""Ресурс вузлів котла → Home Assistant (MQTT discovery, retained).

100% = типовий ресурс вузла. Залишок = 100% − використано.
Темп зносу рахується за останні 365 днів даних з InfluxDB; поки даних менше року
(старт 2026-07-09), темп занижений літом і прогноз років — оптимістичний.
"""
import json, subprocess, datetime as dt, sys
sys.path.insert(0, '/root/heating_log')
from heating_daily import flux, parse_time, BUCKET

# Типовий ресурс (виробник не публікує — консервативні значення для конденсаційних котлів)
PARTS = [
    # key, назва, eBUS-лічильник, ресурс, одиниця
    ('ventyliator', 'вентилятор', 'FanHours', 30000, 'год'),
    ('zapaliuvannia', 'запалювання / газовий клапан', 'FanStarts', 200000, 'запусків'),
    ('nasos', 'насос', 'PumpHours', 50000, 'год'),
    ('klapan_3h', 'триходовий клапан', 'ValveStarts', 100000, 'перемикань'),
]
MIN_DAYS_FOR_RATE = 30
# Котел встановлено 2025-12-08: поки даних в Influx менше року, темп = середній за весь час роботи
# (лічильники котла стартували з нуля), бо він охоплює і зиму, і літо.
INSTALL = dt.datetime(2025, 12, 8, tzinfo=dt.timezone.utc)


def counter(topic):
    q = lambda extra, start: flux(
        f'from(bucket:"{BUCKET}") |> range(start:{start}) '
        f'|> filter(fn:(r)=>r.sensor_topic=="ebusd/bai/{topic}" and r._field=="value_value") '
        f'|> keep(columns:["_time","_value"]) |> {extra}()')
    last = q('last', '-30d')
    first = q('first', '-365d')
    if not last:
        return None
    lt, lv = parse_time(last[-1]['_time']), float(last[-1]['_value'])
    ft, fv = (parse_time(first[0]['_time']), float(first[0]['_value'])) if first else (lt, lv)
    return lv, lt, fv, ft


def pub(topic, payload, retain=True):
    subprocess.run(['mosquitto_pub', '-h', '127.0.0.1', '-t', topic, '-m', payload] + (['-r'] if retain else []),
                   check=True, timeout=10)


DEVICE = {'identifiers': ['kotel_resurs'], 'name': 'Котел: ресурс', 'manufacturer': 'heating_log'}

for key, name, topic, rated, unit in PARTS:
    c = counter(topic)
    if not c:
        continue
    used, lt, fv, ft = c
    days = (lt - ft).total_seconds() / 86400
    if days >= 330:
        per_year, basis = (used - fv) / days * 365, 'останні 365 днів'
    else:
        life_days = (lt - INSTALL).total_seconds() / 86400
        per_year, basis = used / life_days * 365, f'середнє з установки ({round(life_days)} дн.)'
    left = max(0.0, rated - used)
    left_pct = round(left / rated * 100, 1)
    years = round(left / per_year, 1) if per_year else None
    attrs = {
        'використано': round(used), 'ресурс': rated, 'залишилось': round(left), 'одиниця': unit,
        'використано_відсотків': round(100 - left_pct, 1),
        'темп_за_рік': round(per_year) if per_year else None,
        'років_залишилось': years,
        'темп_рахується_за': basis,
    }
    base = f'kotel/resurs/{key}'
    pub(f'homeassistant/sensor/kotel_resurs_{key}/config', json.dumps({
        'name': f'Ресурс: {name}', 'unique_id': f'kotel_resurs_{key}',
        'default_entity_id': f'sensor.kotel_resurs_{key}',
        'state_topic': f'{base}/pct', 'json_attributes_topic': f'{base}/attr',
        'unit_of_measurement': '%', 'state_class': 'measurement', 'icon': 'mdi:wrench-clock',
        'device': DEVICE}, ensure_ascii=False))
    pub(f'homeassistant/sensor/kotel_resurs_{key}_roky/config', json.dumps({
        'name': f'Ресурс: {name} (років)', 'unique_id': f'kotel_resurs_{key}_roky',
        'default_entity_id': f'sensor.kotel_resurs_{key}_roky',
        'state_topic': f'{base}/years', 'unit_of_measurement': 'р', 'icon': 'mdi:calendar-clock',
        'device': DEVICE}, ensure_ascii=False))
    pub(f'{base}/pct', str(left_pct))
    pub(f'{base}/years', str(years if years is not None else 'unknown'))
    pub(f'{base}/attr', json.dumps(attrs, ensure_ascii=False))
    print(key, left_pct, '% left,', years, 'years,', attrs['темп_за_рік'], unit, '/yr')
