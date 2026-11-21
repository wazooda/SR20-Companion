"""Glide range ring calculator (docs/plans/glide_range_ring.md): reads the aircraft over SimConnect, computes the ring
against Copernicus GLO-90 terrain (glide_dem.py) with glide_calc.py, publishes it as L-vars for the PFD/MFD map layer
(tools/glide/sr20_glide_ring.js). Own process and SimConnect client: a crash here cannot touch the yoke helper.

L-vars (frame protocol, §3.3): VALID=0 -> LAT0..71 / LON0..71 (missing ray: LAT = -999) and MISS -> SEQ+1 -> VALID=1.
Hidden (VALID=0) on the ground, below 1000 ft AGL (shown again from 1050), with no data, or with more than 18 rays
missing. Logs: tools/glide/logs/glide_ring.log, terrain check (sim ground vs DEM, P0-2) glide_terrain.csv.

    pythonw tools\\glide\\glide_ring.py --supervise      (startup shortcut)
    python  tools\\glide\\glide_ring.py [--offline]      (foreground)
"""
import argparse
import datetime
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))   # tools/ for probe_electrical, sim_batch
LOGDIR = os.path.join(os.environ["SR20_LOGDIR"], "glide") if os.environ.get("SR20_LOGDIR") else os.path.join(HERE, "logs")   # SR20_LOGDIR: set by the SR20 Companion (tools/companion)

import glide_calc  # noqa: E402

COLS = [("lat", "PLANE LATITUDE", "degrees"), ("lon", "PLANE LONGITUDE", "degrees"), ("alt_ft", "PLANE ALTITUDE", "feet"),
        ("agl_ft", "PLANE ALT ABOVE GROUND", "feet"), ("hdg_true", "PLANE HEADING DEGREES TRUE", "degrees"),
        ("wind_from", "AMBIENT WIND DIRECTION", "degrees"), ("wind_kt", "AMBIENT WIND VELOCITY", "knots"),
        ("weight_lb", "TOTAL WEIGHT", "pounds"), ("density", "AMBIENT DENSITY", "slugs per cubic feet"),
        ("on_ground", "SIM ON GROUND", "Bool"), ("ground_ft", "GROUND ALTITUDE", "feet")]
PERIOD = 0.5
MAX_MISSING = 18


def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    path = os.path.join(LOGDIR, "glide_ring.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


class Publisher:
    def __init__(self, sm):
        from probe_electrical import Reader
        self.rd, self.seq = Reader(sm), 0
        self.rd.set("L:SR20_GLIDE_VALID", "Number", 0)

    def put(self, lat=None, lon=None, missing=None):
        s = self.rd.set
        s("L:SR20_GLIDE_VALID", "Number", 0)
        if lat is not None:
            for i in range(len(lat)):
                s(f"L:SR20_GLIDE_LAT{i}", "Number", -999.0 if missing[i] else float(lat[i]))
                s(f"L:SR20_GLIDE_LON{i}", "Number", float(lon[i]))
            s("L:SR20_GLIDE_MISS", "Number", int(missing.sum()))
        self.seq += 1
        s("L:SR20_GLIDE_SEQ", "Number", self.seq)
        if lat is not None:
            s("L:SR20_GLIDE_VALID", "Number", 1)


def run(offline=False):
    from glide_dem import Dem
    from probe_electrical import connect_sdk
    from sim_batch import BatchReader
    dem = Dem(log=log, offline=offline)
    sm = br = pub = None
    shown, last_count, last_change, next_prefetch, next_tlog, last_info = False, 0, 0.0, 0.0, 0.0, 0.0
    while True:
        t0 = time.time()
        if sm is None:
            try:
                sm = connect_sdk()
                br = BatchReader(sm, COLS)
                pub = Publisher(sm)
                last_count, last_change = 0, time.time()
                log("SimConnect connected")
            except Exception as e:
                sm = None
                time.sleep(5)
                continue
        try:
            br.poll()
            if br.count != last_count:
                last_count, last_change = br.count, time.time()
            elif time.time() - last_change > 5:
                raise ConnectionError("sim stopped answering")
            v = br.latest
            if not v:
                time.sleep(PERIOD)
                continue
            agl = v["agl_ft"]
            shown = (not v["on_ground"]) and (agl >= (1000 if shown else 1050))
            now = time.time()
            if now >= next_prefetch:
                reach = min(glide_calc.MAX_NM, max(agl, 0) * glide_calc.RATIO / 6076 * 1.6 + 5)
                dem.prefetch(v["lat"], v["lon"], reach + 10)
                next_prefetch = now + 10
            if now >= next_tlog:   # P0-2: sim terrain vs DEM under the aircraft
                next_tlog = now + 10
                e = dem.lookup([v["lat"]], [v["lon"]], pooled=False)[0]
                if e == e:
                    path = os.path.join(LOGDIR, "glide_terrain.csv")
                    try:   # capped: keep one 5 MB generation (release review 2026-11-20)
                        if os.path.getsize(path) > 5_000_000:
                            os.replace(path, path + ".1")
                    except OSError:
                        pass
                    new = not os.path.exists(path)
                    with open(path, "a", encoding="utf-8") as f:
                        if new:
                            f.write("time,lat,lon,sim_ground_m,dem_m,diff_m\n")
                        g = v["ground_ft"] * 0.3048
                        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')},{v['lat']:.5f},{v['lon']:.5f},"
                                f"{g:.1f},{e:.1f},{g - e:.1f}\n")
            if not shown:
                pub.put()
            else:
                lat, lon, missing, info = glide_calc.ring(v, dem.lookup)
                if info["missing"] > MAX_MISSING:
                    pub.put()
                else:
                    pub.put(lat, lon, missing)
                if now - last_info > 60:
                    last_info = now
                    log(f"ring: TAS {info['tas_kt']:.0f} kt, sink {info['sink_fpm']:.0f} fpm, {info['min_nm']:.1f}-"
                        f"{info['max_nm']:.1f} NM, {info['missing']} rays missing, {time.time() - t0:.2f} s")
        except Exception as e:
            log(f"error: {e.__class__.__name__}: {e}")
            try:
                sm.exit()
            except Exception:
                pass
            sm = br = pub = None
            time.sleep(2)
            continue
        time.sleep(max(0.05, PERIOD - (time.time() - t0)))


def supervise():
    quick = 0
    while True:
        t0 = time.time()
        rc = subprocess.call([sys.executable, os.path.abspath(__file__)], cwd=HERE)
        log(f"supervisor: calculator exited with {rc & 0xFFFFFFFF:#x}")
        quick = quick + 1 if time.time() - t0 < 10 else 0
        if quick >= 5:
            log("supervisor: calculator keeps exiting at once, giving up")
            return
        time.sleep(3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--supervise", action="store_true")
    ap.add_argument("--offline", action="store_true", help="no downloads (cached tiles only)")
    a = ap.parse_args()
    if a.supervise:
        supervise()
    else:
        try:
            run(a.offline)
        except KeyboardInterrupt:
            pass
