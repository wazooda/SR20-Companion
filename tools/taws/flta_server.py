"""FLTA server for the SR20's TAWS-B (docs/plans/taws_b.md draft 3 §3.4): reads the aircraft over SimConnect at
2 Hz, runs flta_core on Copernicus GLO-90 terrain (tools/glide/glide_dem.py, the glide ring's tile cache) and
publishes the result as L-vars for the PFD's Sr20FltaModule (tools/taws/sr20_taws_pfd.js). Own process under the
SR20 Companion (tools/companion), beside the glide ring.

Inputs:  PLANE LATITUDE / LONGITUDE / ALTITUDE, VELOCITY WORLD Y (the SDK's own GPS datum), GPS GROUND TRUE TRACK,
         GROUND VELOCITY, PLANE BANK DEGREES, SIM ON GROUND, ABSOLUTE TIME (the stamp clock), and from the PFD
         L:SR20_TAWS_GPS_VALID and the runway table L:SR20_TAWS_RWY_N / _i_LAT / _LON / _ELEV / _CRS / _LEN (i < 10).
         The approach manager's target L:SR20_APPR_T_LAT / _LON (read only) prefetches tiles before FLY.
         Obstacles: the FAA DOF cache tools/obstacles/cache (obstacle_index.py; docs/plans/obstacles_o4.md).
Outputs: L:SR20_TAWS_FLTA 0 / 1 caution / 2 warning, _FLTA_KIND 0 RTC / 1 ITI / 2 ROC / 3 IOI, _FLTA_T (s to conflict),
         _FLTA_STAMP (ABSOLUTE TIME of the sample), _DEM 1 ok / 0 unknown terrain in the corridor, _SRV_HB (+1 per
         tick, 0-999), _OBS_DB obstacle data 1 ok / 2 ok, package tiles from another snapshot / 0 unavailable (obstacle
         alerts off, terrain unaffected), debug _FLTA_D (nearest runway NM x10), _FLTA_RC (ft), _FLTA_CLR (min clearance ft).
HTTP:    GET http://127.0.0.1:47821/terrain_max?lat=&lon=&hdg=<true deg>&id=<n> -> {"id", "ok", "max_ft", "why"}: the highest
         terrain (DEM, ft MSL, no allowance) within 3 NM of the point and in a 5 NM x 2 NM corridor ahead along hdg, for the
         instructor approach setup's placement check (docs/plans/approach_setup_v2.md §7.1). ok false with why "loading"
         (tiles queued, ask again) or "no-dem" (server has no terrain yet).
         GET http://127.0.0.1:47821/prefetch?lat=&lon=&r=<NM> -> {"ok", "queued"}: queue the terrain tiles within r NM (max 60)
         of a point, e.g. an airport just picked on the Instructor Station, so the placement check finds them loaded.
         GET http://127.0.0.1:47821/cells -> {"stamp": ABSOLUTE TIME, "level", "kind", "along_m", "cells": [[lat, lon, terrain ft,
         level 1 / 2, heading deg true, width m], ...], "obstacles": [{id, lat, lon, top (ft MSL), agl, sym, level 1 / 2}, ...]}:
         the potential impact areas (PG Table 6-12, Fig. 2-47) for the Map - TAWS-B page and the PFD's synthetic vision,
         and the obstacles of a ROC / IOI alert for the maps; empty unless an alert is asserted (CORS for coui://).
Every alert transition is logged with the state, the runway, d, RC and the offending sample (tools/taws/logs or
SR20_LOGDIR/taws).

    python  tools\\taws\\flta_server.py [--offline] [--no-turn] [--no-obstacles]
"""
import argparse
import datetime
import http.server
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "glide"))
sys.path.insert(0, os.path.dirname(HERE))   # tools/ for probe_electrical, sim_batch
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "obstacles"))
LOGDIR = os.path.join(os.environ["SR20_LOGDIR"], "taws") if os.environ.get("SR20_LOGDIR") else os.path.join(HERE, "logs")

import flta_core as F  # noqa: E402

PERIOD = 0.5
PORT = 47821                        # next to the SurfaceWatch airport server (47820)
EMPTY = {"stamp": 0.0, "level": 0, "kind": 0, "along_m": F.STEP_M, "cells": [], "obstacles": []}
LATEST = EMPTY   # replaced whole each tick (atomic for the HTTP thread)
RWY_MAX = 10
COLS = [("lat", "PLANE LATITUDE", "degrees"), ("lon", "PLANE LONGITUDE", "degrees"), ("alt", "PLANE ALTITUDE", "feet"),
        ("vs", "VELOCITY WORLD Y", "feet per minute"), ("track", "GPS GROUND TRUE TRACK", "degrees"),
        ("gs", "GROUND VELOCITY", "knots"), ("bank", "PLANE BANK DEGREES", "degrees"), ("on_ground", "SIM ON GROUND", "Bool"),
        ("abs_t", "ABSOLUTE TIME", "seconds"), ("gps_valid", "L:SR20_TAWS_GPS_VALID", "number"),
        ("rwy_n", "L:SR20_TAWS_RWY_N", "number"), ("appr_lat", "L:SR20_APPR_T_LAT", "number"), ("appr_lon", "L:SR20_APPR_T_LON", "number")]
for i in range(RWY_MAX):
    for f in ("LAT", "LON", "ELEV", "CRS", "LEN"):
        COLS.append((f"rwy{i}_{f.lower()}", f"L:SR20_TAWS_RWY_{i}_{f}", "number"))


KINDS = ("RTC", "ITI", "ROC", "IOI")



class _ExclusiveServer(http.server.ThreadingHTTPServer):
    # SO_REUSEADDR (the stdlib default) lets a second program bind the same port on Windows without an error;
    # exclusive, as in efb_server, so a clash fails at start (release review 2026-11-20)
    allow_reuse_address = False
    daemon_threads = True

def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    path = os.path.join(LOGDIR, "flta_server.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def runways_from(v):
    out = []
    n = int(v.get("rwy_n", 0) or 0)
    for i in range(min(n, RWY_MAX)):
        lat, lon = v.get(f"rwy{i}_lat", 0.0), v.get(f"rwy{i}_lon", 0.0)
        if not lat and not lon:
            continue
        out.append({"lat": lat, "lon": lon, "elev": v.get(f"rwy{i}_elev", 0.0), "crs": v.get(f"rwy{i}_crs", 0.0), "len_m": v.get(f"rwy{i}_len", 0.0)})
    return out


def aircraft_state(v):
    """The SimConnect sample as flta_core's state. Bank: flta_core takes a right bank as positive; PLANE BANK DEGREES is
    negative in a right bank (measured 2026-11-01: -25 deg in an autopilot right turn). Until then the turn projection
    curved the wrong way in every turn."""
    return {"lat": v["lat"], "lon": v["lon"], "alt": v["alt"], "vs": v["vs"], "track": v["track"], "gs": v["gs"], "bank": -v["bank"],
            "on_ground": bool(v["on_ground"]), "t": v["abs_t"], "gps_valid": bool(v.get("gps_valid", 1))}


DEM = None   # the run loop's Dem, shared with the HTTP thread for /terrain_max
TERRAIN_R_NM, TERRAIN_AHEAD_NM, TERRAIN_HALF_NM, TERRAIN_STEP_NM = 3.0, 5.0, 1.0, 0.05


def terrain_max(dem, lat, lon, hdg):
    """Highest terrain (ft MSL) around (lat, lon) and ahead along hdg (true): (max_ft, None) or (None, why)."""
    import math
    import numpy as np
    if dem is None:
        return None, "no-dem"
    k = max(math.cos(math.radians(lat)), 0.05)
    n = int(round((TERRAIN_R_NM + TERRAIN_AHEAD_NM) / TERRAIN_STEP_NM))
    g = np.arange(-n, n + 1) * TERRAIN_STEP_NM
    e, nn = np.meshgrid(g, g)                                   # east / north offsets, NM
    h = math.radians(hdg)
    along = e * math.sin(h) + nn * math.cos(h)
    across = np.abs(e * math.cos(h) - nn * math.sin(h))
    keep = (np.hypot(e, nn) <= TERRAIN_R_NM) | ((along >= 0) & (along <= TERRAIN_AHEAD_NM) & (across <= TERRAIN_HALF_NM))
    la = lat + nn[keep] / 60.0
    lo = lon + e[keep] / (60.0 * k)
    v = dem.lookup(la, lo)
    if np.isnan(v).any():
        dem.prefetch(lat, lon, TERRAIN_R_NM + TERRAIN_AHEAD_NM + 2)
        return None, "loading"
    return float(np.max(v)) / F.FT_M, None


class CellsHandler(http.server.BaseHTTPRequestHandler):
    def _json(self, obj):
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/terrain_max":
            from urllib.parse import parse_qs
            q = {k: v[0] for k, v in parse_qs(query).items()}
            try:
                lat, lon, hdg = float(q["lat"]), float(q["lon"]), float(q.get("hdg", 0))
            except (KeyError, ValueError):
                self._json({"id": q.get("id"), "ok": False, "why": "bad-request"})
                return
            mx, why = terrain_max(DEM, lat, lon, hdg)
            self._json({"id": q.get("id"), "ok": mx is not None, "max_ft": mx, "why": why})
            return
        if path == "/prefetch":
            from urllib.parse import parse_qs
            q = {k: v[0] for k, v in parse_qs(query).items()}
            try:
                lat, lon, r = float(q["lat"]), float(q["lon"]), min(float(q.get("r", 40)), 60.0)
            except (KeyError, ValueError):
                self._json({"ok": False, "why": "bad-request"})
                return
            if DEM is None:
                self._json({"ok": False, "why": "no-dem"})
                return
            before = len(DEM.pending)
            DEM.prefetch(lat, lon, r)
            log(f"prefetch {lat:.3f} {lon:.3f} r {r:.0f} NM: {len(DEM.pending) - before} tiles queued")
            self._json({"ok": True, "queued": len(DEM.pending) - before})
            return
        if path != "/cells":
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        data = json.dumps(LATEST, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")   # the instruments' origin is coui://
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def start_http():
    try:
        srv = _ExclusiveServer(("127.0.0.1", PORT), CellsHandler)
    except OSError as e:
        log(f"impact cells: port {PORT} unavailable ({e}); the page and SVT show no cells")
        return None
    threading.Thread(target=srv.serve_forever, name="flta-cells", daemon=True).start()
    log(f"impact cells on http://127.0.0.1:{PORT}/cells")
    return srv


class Publisher:
    def __init__(self, sm):
        from probe_electrical import Reader
        self.rd = Reader(sm)
        self.hb = 0
        self.last = {}

    def put(self, name, value):
        # written every tick, never change-detected: a flight reload resets every L-var to 0 behind the server's back
        # (2026-10-31: DEM stayed 0 after a reload, the PFD sat in TAWS N/A and the self-test never finished)
        self.rd.set("L:SR20_TAWS_" + name, "Number", value)

    def publish(self, out, raw, obs_db):
        self.put("OBS_DB", int(obs_db))
        self.put("FLTA", int(out["level"]))
        self.put("FLTA_KIND", int(out["kind"]))
        self.put("FLTA_T", float(out["t"]))
        self.put("DEM", int(out["dem"]))
        self.put("FLTA_D", -1.0 if raw.get("d_nm") is None else round(raw["d_nm"] * 10.0))
        self.put("FLTA_RC", float(raw.get("rc", 0.0)))
        clr = raw.get("min_clear")
        self.put("FLTA_CLR", -9999.0 if clr is None or clr == float("inf") else round(float(clr)))
        self.rd.set("L:SR20_TAWS_FLTA_STAMP", "Number", float(out["stamp"]))   # last: the PFD reads the stamp to validate the rest
        self.hb = (self.hb + 1) % 1000
        self.rd.set("L:SR20_TAWS_SRV_HB", "Number", self.hb)


def transition_line(out, raw, v, runways, obs_db):
    """The log line for an alert transition (level or kind): the state, the runway geometry, both candidates and the
    first conflicting obstacle. Tested in test_flta_obstacles.py (a crash here took the whole tick down, 2026-11-08)."""
    tc, oc = raw.get("terrain"), raw.get("obstacle")
    o0 = ((oc or {}).get("obstacles") or [{}])[0]
    return (f"FLTA {out['level']} kind {out['kind']} ({KINDS[int(out['kind'])]}) t {out['t']} s | {v['lat']:.4f} {v['lon']:.4f} alt {v['alt']:.0f} vs {v['vs']:.0f} (proj {raw.get('vs_proj', v['vs']):.0f}) trk {v['track']:.0f} "
            f"gs {v['gs']:.0f} bank R{-v['bank']:.0f} desc {raw.get('descending')} | runways {len(runways)} d {raw.get('d_nm')} RC {raw.get('rc')} "
            f"clear {raw.get('min_clear')} terrain {raw.get('conflict_terrain_ft')} proj {raw.get('conflict_proj_ft')} dem {out['dem']} clipped {raw.get('clipped')} "
            f"| cand terrain {tc and (tc['level'], tc['kind'], round(tc['t'], 1))} obstacle {oc and (oc['level'], oc['kind'], round(oc['t'], 1))} "
            f"{o0.get('id')} top {o0.get('top')} at {o0.get('s_nm')} NM clear {o0.get('clear_ft')} | obs db {obs_db}")


def cells_payload(out):
    """The /cells answer for a tick (LATEST)."""
    return {"stamp": float(out["stamp"]), "level": int(out["level"]), "kind": int(out["kind"]), "along_m": float(out["cell_along_m"]),
            "cells": out["cells"], "obstacles": [{k: o[k] for k in ("id", "lat", "lon", "top", "agl", "sym", "level")} for o in out["obstacles"]]}


def run(offline=False, turn=True, obstacles=True):
    from glide_dem import Dem
    from obstacle_index import ObstacleIndex
    from probe_electrical import connect_sdk
    from sim_batch import BatchReader
    global LATEST, DEM
    dem = Dem(log=log, offline=offline)
    DEM = dem
    flta = F.Flta(turn=turn)
    obs = ObstacleIndex(log=log) if obstacles else None
    obs_q = obs.query_box if obs is not None and obs.ok else None
    obs_db = obs.db if obs is not None else 0
    start_http()
    sm = br = pub = None
    last_count, last_change, next_prefetch, last_level, last_appr = 0, 0.0, 0.0, (0, 0), None
    while True:
        t0 = time.time()
        if sm is None:
            try:
                sm = connect_sdk()
                br = BatchReader(sm, COLS)
                pub = Publisher(sm)
                last_count, last_change = 0, time.time()
                log("SimConnect connected")
            except Exception as e:  # noqa: BLE001
                log(f"connect: {e.__class__.__name__}: {e}")
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
            now = time.time()
            if now >= next_prefetch:
                next_prefetch = now + 10
                dem.prefetch(v["lat"], v["lon"], 12.0)
                appr = (round(v.get("appr_lat", 0.0), 3), round(v.get("appr_lon", 0.0), 3))
                if appr != last_appr and appr != (0.0, 0.0):
                    last_appr = appr
                    dem.prefetch(appr[0], appr[1], 15.0)
                    log(f"approach target {appr}: tiles prefetched")
            runways = runways_from(v)
            st = aircraft_state(v)
            out = flta.tick(st, runways, lambda la, lo: dem.lookup(la, lo), now=v["abs_t"] + (time.time() - t0), obstacles=obs_q)
            raw = out["raw"]
            pub.publish(out, raw, obs_db)
            LATEST = cells_payload(out)
            if (out["level"], out["kind"]) != last_level:
                last_level = (out["level"], out["kind"])
                log(transition_line(out, raw, v, runways, obs_db))
        except Exception as e:  # noqa: BLE001
            log(f"error: {e.__class__.__name__}: {e}")
            LATEST = EMPTY
            try:
                sm.exit()
            except Exception:  # noqa: BLE001
                pass
            sm = br = pub = None
            time.sleep(2)
            continue
        time.sleep(max(0.05, PERIOD - (time.time() - t0)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="no downloads (cached tiles only)")
    ap.add_argument("--no-turn", action="store_true", help="straight-track projection only")
    ap.add_argument("--no-obstacles", action="store_true", help="terrain only (no ROC / IOI)")
    a = ap.parse_args()
    try:
        run(a.offline, not a.no_turn, not a.no_obstacles)
    except KeyboardInterrupt:
        pass
