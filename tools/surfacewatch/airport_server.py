"""SR20 SurfaceWatch airport server: hands the G1000 any airport's taxiway map while the sim runs (the author 2026-10-26:
"if I put it in the flight plan or if a user loads up a particular airport ... automatically download the data").

The maps come from the sim itself (SimConnect facility data, add_airport.py), so they match the scenery, add-ons included.
A map the package does not have yet is exported on the first request (~0.2 s), validated and written into the package
(html_ui/SR20SurfaceWatch/airports + index.json + layout.json), so it is also there at the next sim start without this
server. The instruments cannot see files added to the package after the sim started (layout.json is read at load), so
while this runs they read the maps from here instead (fetch to 127.0.0.1 works from Coherent with a CORS header,
probed 2026-10-26).

    GET /status                  {"ok": 1, "rev": n}   rev changes whenever an airport is added
    GET /airports/index.json     the package index (as coui://html_ui/SR20SurfaceWatch/airports/index.json)
    GET /airports/<ICAO>.json    the map; exported first when missing. 404 {"error": ...} when the sim has none

    pythonw tools\\surfacewatch\\airport_server.py --supervise     (startup shortcut)
    python  tools\\surfacewatch\\airport_server.py                 (foreground)
"""
import argparse
import datetime
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import add_airport as A  # noqa: E402
import osm_names as OSM  # noqa: E402

PORT = 47820
LOGDIR = os.path.join(os.environ["SR20_LOGDIR"], "surfacewatch") if os.environ.get("SR20_LOGDIR") else os.path.join(HERE, "logs")   # SR20_LOGDIR: set by the SR20 Companion (tools/companion)
PACKAGE = A.DEFAULT_PACKAGE
AIRPORTS = os.path.join(PACKAGE, A.AIRPORTS_REL)
ICAO_RE = re.compile(r"^[A-Z0-9]{2,7}$")
LOCK = threading.Lock()          # one SimConnect export at a time
STATE = {"rev": 0, "failed": {}}  # failed: icao -> (time, reason); retried after FAIL_RETRY_S
FAIL_RETRY_S = 300



class _ExclusiveServer(http.server.ThreadingHTTPServer):
    # SO_REUSEADDR (the stdlib default) lets a second program bind the same port on Windows without an error;
    # exclusive, as in efb_server, so a clash fails at start (release review 2026-11-20)
    allow_reuse_address = False
    daemon_threads = True

def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    path = os.path.join(LOGDIR, "airport_server.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def export(icao):
    """Path of the airport's map, exporting it from the sim first if the package has none. Raises A.ExportError."""
    path = os.path.join(AIRPORTS, icao + ".json")
    if os.path.exists(path):
        return path
    with LOCK:
        if os.path.exists(path):
            return path
        f = STATE["failed"].get(icao)
        if f and time.time() - f[0] < FAIL_RETRY_S:
            raise A.ExportError(f[1])
        dll_path = next((p for p in A.DLL_CANDIDATES if os.path.exists(p)), None)
        if not dll_path:
            raise A.ExportError("SimConnect.dll (MSFS 2024) not found")
        t0 = time.time()
        try:
            doc = A.build(icao, A.fetch(A.load_dll(dll_path), icao, timeout=15.0), A.sim_version_string())
            problem = A.validate(doc)
            if problem:
                raise A.ExportError(f"self-check failed: {problem}")
        except A.ExportError as e:
            STATE["failed"][icao] = (time.time(), str(e))
            log(f"{icao}: no map - {e}")
            raise
        OSM.relabel(doc, log=log)   # taxiway names from OpenStreetMap where the sim's are wrong (KCNO); never raises
        A.write_airport(PACKAGE, doc)
        A.write_index(PACKAGE)
        # layout.json too, so the map is in the package at the next sim start even without this server
        A.regen_layout(PACKAGE, quiet=True)
        STATE["rev"] += 1
        log(f"{icao}: exported {doc['name']} in {time.time() - t0:.1f} s ({len(doc['paths'])} paths)")
        return path


class Handler(http.server.BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")   # the instruments' origin is coui://
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            p = self.path.split("?", 1)[0]
            if p == "/status":
                return self.reply(200, {"ok": 1, "rev": STATE["rev"]})
            if p == "/airports/index.json":
                if not os.path.exists(os.path.join(AIRPORTS, "index.json")):
                    A.write_index(PACKAGE)
                return self.reply(200, open(os.path.join(AIRPORTS, "index.json"), "rb").read())
            m = re.fullmatch(r"/airports/([A-Za-z0-9]+)\.json", p)
            if m and ICAO_RE.match(m.group(1).upper()):
                icao = m.group(1).upper()
                try:
                    return self.reply(200, open(export(icao), "rb").read())
                except A.ExportError as e:
                    return self.reply(404, {"error": str(e)})
            self.reply(404, {"error": "not found"})
        except Exception as e:   # noqa: BLE001 - never let one request kill the server
            log(f"request {self.path}: {e.__class__.__name__}: {e}")
            try:
                self.reply(500, {"error": str(e)})
            except Exception:   # noqa: BLE001
                pass

    def log_message(self, fmt, *args):   # quiet: requests every few seconds
        pass


def run():
    srv = _ExclusiveServer(("127.0.0.1", PORT), Handler)
    log(f"serving airport maps on http://127.0.0.1:{PORT}/ ({len([f for f in os.listdir(AIRPORTS) if f != 'index.json'])} in the package)")
    srv.serve_forever()


def supervise():
    quick = 0
    while True:
        t0 = time.time()
        rc = subprocess.call([sys.executable, os.path.abspath(__file__)], cwd=HERE)
        log(f"supervisor: server exited with {rc & 0xFFFFFFFF:#x}")
        quick = quick + 1 if time.time() - t0 < 10 else 0
        if quick >= 5:
            log("supervisor: server keeps exiting at once (port in use?), giving up")
            return
        time.sleep(3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--supervise", action="store_true")
    if ap.parse_args().supervise:
        supervise()
    else:
        run()
