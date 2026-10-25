#!/usr/bin/env python3
"""Щоденна аналітика опалення: газ vs температура котла vs погода.

Джерело: InfluxDB bucket "ebus" (дані котла з ebusd + погода з telegraf/Open-Meteo).
Результат: measurement "heating_daily" у тому ж bucket + CSV /root/heating_log/heating_daily.csv

  heating_daily.py                 — вчора і сьогодні (cron щогодини)
  heating_daily.py 2026-09-20      — з цієї дати до сьогодні
  heating_daily.py --weather-backfill 2026-09-20   — довантажити погодинну погоду з Open-Meteo
  heating_daily.py --report        — газ на градусо-день за температурою котла
"""
import csv, json, os, sys, urllib.request, urllib.parse, datetime as dt
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Kyiv')
UTC = dt.timezone.utc
INFLUX = 'http://127.0.0.1:8086'
ORG, BUCKET = 'myorg', 'ebus'
CSV_PATH = '/root/heating_log/heating_daily.csv'
LAT, LON = 50.45, 30.52  # свої координати (Open-Meteo)
# Та сама калібрована формула, що й у HA (sensor.kotel_rozrakhunkova_vitrata_gazu)
MAX_GAS_KW, CALORIFIC = 28, 9.65
BASE_T = 18.0  # база градусо-днів
DHW_FLOW_MIN = 0.5  # л/хв — горіння з водорозбором = ГВП, не опалення


def token():
    # Токен InfluxDB: змінна оточення INFLUX_TOKEN або рядок token = "..." з telegraf.conf
    if os.environ.get('INFLUX_TOKEN'):
        return os.environ['INFLUX_TOKEN']
    for line in open('/opt/monitoring/telegraf.conf'):
        if 'token' in line and '"' in line:
            return line.split('"')[1]
    raise SystemExit('no influx token')


TOKEN = token()


def flux(q):
    req = urllib.request.Request(f'{INFLUX}/api/v2/query?org={ORG}', data=q.encode(), headers={
        'Authorization': 'Token ' + TOKEN, 'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})
    rows, hdr = [], None
    for line in urllib.request.urlopen(req, timeout=120).read().decode().splitlines():
        if not line.strip():
            hdr = None
            continue
        cells = next(csv.reader([line]))
        if hdr is None:
            hdr = cells
            continue
        rows.append(dict(zip(hdr, cells)))
    return rows


def write(lines):
    if not lines:
        return
    req = urllib.request.Request(f'{INFLUX}/api/v2/write?org={ORG}&bucket={BUCKET}&precision=s',
                                 data='\n'.join(lines).encode(),
                                 headers={'Authorization': 'Token ' + TOKEN})
    urllib.request.urlopen(req, timeout=60).read()


def parse_time(s):
    """Influx RFC3339 з наносекундами → datetime (UTC)."""
    s = s.rstrip('Z')
    if '.' in s:
        main, frac = s.split('.')
        s = main + '.' + frac[:6].ljust(6, '0')
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


def series(topic, field, start, stop, lookback_h=12):
    """Точки [(t, value)] + остання точка до start (щоб знати стан на початок доби)."""
    def q(a, b, extra=''):
        return flux(f'from(bucket:"{BUCKET}") |> range(start:{a}, stop:{b}) '
                    f'|> filter(fn:(r)=>r.sensor_topic=="{topic}" and r._field=="{field}") '
                    f'|> keep(columns:["_time","_value"]){extra}')
    iso = lambda t: t.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    prev = q(iso(start - dt.timedelta(hours=lookback_h)), iso(start), ' |> last()')
    pts = [(start, float(prev[-1]['_value']))] if prev else []
    for r in q(iso(start), iso(stop), ' |> sort(columns:["_time"])'):
        pts.append((parse_time(r['_time']), float(r['_value'])))
    return pts


def value_at(pts, t):
    v = None
    for tt, x in pts:
        if tt > t:
            break
        v = x
    return v


def integrate(pts, a, b):
    """Ліва сума Рімана (ступінчасто) значення*секунди на [a,b] → (сума, секунди з даними)."""
    total = secs = 0.0
    for i, (t, v) in enumerate(pts):
        nt = pts[i + 1][0] if i + 1 < len(pts) else b
        lo, hi = max(t, a), min(nt, b)
        if hi > lo:
            d = (hi - lo).total_seconds()
            total += v * d
            secs += d
    return total, secs


def counter_delta(topic, start, stop):
    pts = series(topic, 'value_value', start, stop, lookback_h=48)
    return round(pts[-1][1] - pts[0][1], 1) if len(pts) >= 2 else None


def weather_hours(start, stop):
    iso = lambda t: t.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    rows = flux(f'from(bucket:"{BUCKET}") |> range(start:{iso(start)}, stop:{iso(stop)}) '
                f'|> filter(fn:(r)=>r._measurement=="weather" and r._field=="temperature_2m") '
                f'|> group() |> aggregateWindow(every:1h, fn:mean, createEmpty:false)')
    return [float(r['_value']) for r in rows if r.get('_value') not in (None, '')]


def day_stats(day):
    start = dt.datetime.combine(day, dt.time(0), TZ)
    stop = min(start + dt.timedelta(days=1), dt.datetime.now(TZ))
    flame = series('ebusd/bai/Flame', 'flame_state', start, stop)
    mod = series('ebusd/bai/ModulationDesired', 'value_value', start, stop)
    flow = series('ebusd/bai/FlowTemp', 'temp_value', start, stop)
    ret = series('ebusd/bai/ReturnTemp', 'temp_value', start, stop)
    sp = series('ebusd/bai/FlowTempDesired', 'value_value', start, stop, lookback_h=24 * 14)
    water = series('ebusd/bai/HwcWaterflow', 'value_value', start, stop)

    # сегменти горіння
    segs, on_since = [], None
    for t, v in flame:
        if v >= 1 and on_since is None:
            on_since = t
        elif v < 1 and on_since is not None:
            segs.append((on_since, t)); on_since = None
    if on_since is not None:
        segs.append((on_since, stop))

    s = dict(gas_heat=0.0, gas_dhw=0.0, heat_s=0.0, dhw_s=0.0, heat_starts=0, dhw_starts=0)
    acc = {k: [0.0, 0.0] for k in ('flow', 'ret', 'sp', 'mod')}
    # Усередині одного горіння котел може перемикатися опалення ↔ ГВП (відкрили кран),
    # тому класифікуємо кожен відрізок між змінами модуляції/водорозбору окремо.
    for a, b in segs:
        if b <= a:
            continue
        cuts = sorted({a, b} | {t for t, _ in mod if a < t < b} | {t for t, _ in water if a < t < b})
        was_heat = was_dhw = False
        for lo, hi in zip(cuts, cuts[1:]):
            d = (hi - lo).total_seconds()
            m = value_at(mod, lo) or 0
            gas = m / 100 * MAX_GAS_KW / CALORIFIC * d / 3600
            if (value_at(water, lo) or 0) >= DHW_FLOW_MIN:
                s['gas_dhw'] += gas; s['dhw_s'] += d
                was_dhw = True
            else:
                s['gas_heat'] += gas; s['heat_s'] += d
                was_heat = True
                for k, src in (('flow', flow), ('ret', ret), ('sp', sp), ('mod', mod)):
                    tot, sec = integrate(src, lo, hi)
                    acc[k][0] += tot; acc[k][1] += sec
        # горіння, що почалося з водорозбором, — запуск ГВП; інакше — запуск опалення
        if (value_at(water, a) or 0) >= DHW_FLOW_MIN:
            s['dhw_starts'] += 1
        elif was_heat:
            s['heat_starts'] += 1

    temps = weather_hours(start, stop)
    hdd = sum(max(0.0, BASE_T - x) for x in temps) / 24 if temps else None
    out = {
        'date': day.isoformat(),
        'complete': int(stop >= start + dt.timedelta(days=1)),
        't_out_mean': round(sum(temps) / len(temps), 1) if temps else None,
        't_out_min': round(min(temps), 1) if temps else None,
        't_out_max': round(max(temps), 1) if temps else None,
        'hdd18': round(hdd, 2) if hdd is not None else None,
        'gas_heat_m3': round(s['gas_heat'], 3),
        'gas_dhw_m3': round(s['gas_dhw'], 3),
        'gas_total_m3': round(s['gas_heat'] + s['gas_dhw'], 3),
        'heat_burn_h': round(s['heat_s'] / 3600, 2),
        'heat_starts': s['heat_starts'],
        'heat_cycle_min': round(s['heat_s'] / 60 / s['heat_starts'], 1) if s['heat_starts'] else None,
        'dhw_burn_min': round(s['dhw_s'] / 60, 1),
        'dhw_starts': s['dhw_starts'],
        'setpoint_avg': None, 'flow_avg': None, 'return_avg': None, 'modulation_avg': None,
        'gas_per_hdd': None,
        'energy_hc_units': counter_delta('ebusd/bai/PrEnergySumHc1', start, stop),
        'energy_hwc_units': counter_delta('ebusd/bai/PrEnergySumHwc1', start, stop),
    }
    for k, name in (('sp', 'setpoint_avg'), ('flow', 'flow_avg'), ('ret', 'return_avg'), ('mod', 'modulation_avg')):
        if acc[k][1]:
            out[name] = round(acc[k][0] / acc[k][1], 1)
    if hdd and hdd >= 1 and s['gas_heat'] > 0:
        out['gas_per_hdd'] = round(s['gas_heat'] / hdd, 3)
    return out, start


def save(rows):
    # Influx: один пункт на добу (перезаписується з тим самим часом)
    lines = []
    for r, start in rows:
        f = ','.join(f'{k}={v}' if isinstance(v, (int, float)) and not isinstance(v, bool) else ''
                     for k, v in r.items() if k != 'date' and v is not None)
        f = ','.join(x for x in f.split(',') if x)
        lines.append(f'heating_daily {f} {int(start.timestamp())}')
    write(lines)
    # CSV: оновлюємо рядки за датою
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    existing = {}
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, newline='') as fh:
            for row in csv.DictReader(fh):
                existing[row['date']] = row
    for r, _ in rows:
        existing[r['date']] = r
    fields = list(rows[0][0].keys())
    with open(CSV_PATH, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for d in sorted(existing):
            w.writerow(existing[d])


def weather_backfill(since):
    today = dt.date.today()
    url = ('https://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode({
        'latitude': LAT, 'longitude': LON, 'timezone': 'UTC',
        'hourly': 'temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,cloud_cover,wind_speed_10m,shortwave_radiation',
        'start_date': since.isoformat(), 'end_date': today.isoformat()}))
    h = json.load(urllib.request.urlopen(url, timeout=60))['hourly']
    now = dt.datetime.now(UTC)
    lines = []
    for i, ts in enumerate(h['time']):
        t = dt.datetime.fromisoformat(ts).replace(tzinfo=UTC)
        if t > now:
            break
        f = ','.join(f'{k}={h[k][i]}' for k in h if k != 'time' and h[k][i] is not None)
        if f:
            lines.append(f'weather,source=hourly_backfill {f} {int(t.timestamp())}')
    write(lines)
    print('weather points written:', len(lines))


def report():
    if not os.path.exists(CSV_PATH):
        raise SystemExit('no data yet')
    # лише дні, коли опалення справді працювало (≥ 30 хв горіння на опалення)
    rows = [r for r in csv.DictReader(open(CSV_PATH))
            if r['complete'] == '1' and r['gas_per_hdd'] and float(r['heat_burn_h']) >= 0.5]
    print(f'Повних днів з опаленням: {len(rows)}\n')
    print('уставка  днів  t_вул  газ/день  газ на градусо-день  цикл,хв  обратка')
    bands = {}
    for r in rows:
        b = int(float(r['setpoint_avg']) // 2 * 2) if r['setpoint_avg'] else None
        bands.setdefault(b, []).append(r)
    for b in sorted(bands, key=lambda x: (x is None, x)):
        rs = bands[b]
        avg = lambda k: sum(float(r[k]) for r in rs if r[k]) / max(1, sum(1 for r in rs if r[k]))
        hdd = sum(float(r['hdd18']) for r in rs)
        gas = sum(float(r['gas_heat_m3']) for r in rs)
        label = f'{b}-{b + 1}' if b is not None else '  ?  '
        print(f'{label:>7} {len(rs):5d} {avg("t_out_mean"):6.1f} {gas / len(rs):9.2f} {gas / hdd:14.3f} m³'
              f'      {avg("heat_cycle_min"):6.1f} {avg("return_avg"):8.1f}')
    print('\nМенше "газ на градусо-день" = ефективніше (не залежить від погоди).')


if __name__ == '__main__':
    args = sys.argv[1:]
    if args[:1] == ['--report']:
        report(); sys.exit()
    if args[:1] == ['--weather-backfill']:
        weather_backfill(dt.date.fromisoformat(args[1])); sys.exit()
    today = dt.datetime.now(TZ).date()
    first = dt.date.fromisoformat(args[0]) if args else today - dt.timedelta(days=1)
    days = [first + dt.timedelta(days=i) for i in range((today - first).days + 1)]
    rows = [day_stats(d) for d in days]
    save(rows)
    for r, _ in rows:
        print(r['date'], 't=%s' % r['t_out_mean'], 'hdd=%s' % r['hdd18'], 'heat=%s m3' % r['gas_heat_m3'],
              'dhw=%s' % r['gas_dhw_m3'], 'h=%s' % r['heat_burn_h'], 'starts=%s' % r['heat_starts'],
              'sp=%s' % r['setpoint_avg'], 'ret=%s' % r['return_avg'], 'g/hdd=%s' % r['gas_per_hdd'])
