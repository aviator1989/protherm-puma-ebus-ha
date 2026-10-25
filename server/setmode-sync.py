#!/usr/bin/env python3
import sqlite3, subprocess, time, datetime

DB = "file:/mnt/usb/homeassistant/home-assistant_v2.db?mode=ro"
LOG = "/var/log/setmode-sync.log"

ACTIVE_INTERVAL = 2
IDLE_INTERVAL = 60

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"{ts} {msg}\n")

def get_state(entity_id):
    con = sqlite3.connect(DB, uri=True, timeout=5)
    con.execute("PRAGMA busy_timeout=5000")
    cur = con.cursor()
    cur.execute("""
        SELECT s.state FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id
        WHERE m.entity_id = ? ORDER BY s.last_updated_ts DESC LIMIT 1
    """, (entity_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None

def ebus_read(field):
    out = subprocess.run(["docker", "exec", "ebusd", "ebusctl", "read", "-f", field],
                          capture_output=True, text=True, timeout=10)
    return out.stdout.strip()

def safe_float(val, lo, hi, default):
    try:
        f = float(val)
        if lo <= f <= hi:
            return f
    except (TypeError, ValueError):
        pass
    return default

def run_once():
    heating_switch = get_state("input_boolean.boiler_heating_switch")
    heating_temp_raw = get_state("input_number.boiler_heating_temp")
    hwc_switch = get_state("input_boolean.zapit_na_gvp_kotla")
    hwc_temp_raw = get_state("input_number.temperatura_gvp_kotla")

    heating_on = heating_switch == "on"
    hwc_on = hwc_switch == "on"
    write_active = heating_on or hwc_on

    flame_on = get_state("binary_sensor.kotel_polum_ia") == "on"
    waterflow = safe_float(get_state("sensor.kotel_vitrata_gariachoi_vodi"), 0, 100, 0.0)
    physical_active = flame_on or waterflow > 0
    active = write_active or physical_active

    if not write_active:
        return active

    if heating_switch == "on":
        flowtemp = safe_float(heating_temp_raw, 20, 80, None)
        if flowtemp is None:
            log(f"SKIP: invalid heating temp value: {heating_temp_raw!r}")
            return active
        source_desc = "manual"
    else:
        current = ebus_read("FlowTempDesired")
        flowtemp = safe_float(current, 20, 80, None)
        if flowtemp is None:
            log(f"SKIP: could not read current FlowTempDesired: {current!r}")
            return active
        source_desc = "passthrough"

    if hwc_on:
        hwctemp = safe_float(hwc_temp_raw, 30, 70, None)
        if hwctemp is None:
            log(f"SKIP: invalid hwc temp value: {hwc_temp_raw!r}")
            return active
    else:
        current = ebus_read("StorageTempDesired")
        hwctemp = safe_float(current, 30, 70, None)
        if hwctemp is None:
            log(f"SKIP: could not read current StorageTempDesired: {current!r}")
            return active

    payload = f"auto;{flowtemp:g};{hwctemp:g};{hwctemp:g};0;0;0;0;0;0;0;0"
    subprocess.run(["mosquitto_pub", "-h", "127.0.0.1", "-t", "ebusd/bai/SetMode/set", "-m", payload],
                   timeout=10)
    log(f"WRITE heating_source={source_desc} hwc_on={hwc_on} payload={payload}")
    return active

def main():
    while True:
        try:
            active = run_once()
        except Exception as e:
            log(f"ERROR: {e}")
            active = False
        time.sleep(ACTIVE_INTERVAL if active else IDLE_INTERVAL)

if __name__ == "__main__":
    main()
