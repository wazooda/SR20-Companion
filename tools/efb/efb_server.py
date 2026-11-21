"""SR20 EFB helper: the iPad side of the FlightStream 510 emulation (docs/plans/flightstream_510.md §12). Own process
under the SR20 Companion (tools/companion), beside the glide ring and the TAWS server.

Moving map (on by default; --no-map or {"map": false} in efb.json turns it off):
    ForeFlight broadcasts {"App":"ForeFlight","GDL90":{"port":4000}} on UDP 63093 every 5 s while it is in the
    foreground. Every iPad heard in the last 10 minutes gets ForeFlight's simulator protocol on UDP 49002 (the "XGPS"
    lines of support.foreflight.com/hc/en-us/articles/204115005): XGPS once a second, XATT five times a second. It
    shows in ForeFlight's Devices as "SR20 Sim". Nothing is sent while the GPS is failed (L:SR20_TAWS_GPS_VALID 0,
    once the PFD has published it at least once) or the sim is not connected.

Flight plan from the iPad, two ways in:
    - "Send to SR20" in ForeFlight's Share FPL File sheet: an iOS Shortcut posts the file over the home Wi-Fi to the
      LAN listener (efb_lan.py, TCP 47823), after a one-time pairing by QR from the setup page
      (docs/plans/fpl_without_tailscale.md);
    - Share FPL File -> Tailscale (or anything else) -> the Downloads folder as ForeFlight*.fpl, scanned every 2 s.
    Either is offered to the MFD, which polls (loopback only):
        GET http://127.0.0.1:47822/pending              -> {"seq": "<boot>-<n>" | "", "plan": {...} | null}
        GET http://127.0.0.1:47822/ack?seq=<seq>&result=activated|ignored|failed&why=...   -> {"ok": bool}
    plan = {"name", "tail", "cruise_ft", "file", "points": [{"ident", "type", "lat", "lon"}], "dropped": {...}}
    or {"name", "file", "error"} when a Downloads file could not be read. A newer plan replaces an unanswered one; an
    unreadable file never replaces a good unanswered plan. seq is unique across helper restarts (a random boot id),
    and an unanswered plan survives a restart for an hour with the same seq (data/efb_state.json).

Setup page (loopback, a browser on this PC): http://127.0.0.1:47822/setup, opened by SR20Companion.exe --ipad.
    QR + code for pairing, status (listener, ForeFlight heard, iPad reached), network category and firewall, and the
    buttons New code / Forget all iPads / Allow through the firewall (efb_setup.py).

Data folder: SR20_DATA\\efb when the Companion is frozen, tools\\efb\\data from the repo (git-ignored):
    efb.json (settings: {"map": true, "lan_upload": true}), efb_state.json, efb_tokens.json, efb_inbox\\.

    python  tools\\efb\\efb_server.py [--no-map] [--dir <folder>]
"""
import argparse
import datetime
import glob
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import time
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))   # tools/ for probe_electrical, sim_batch
LOGDIR = os.path.join(os.environ["SR20_LOGDIR"], "efb") if os.environ.get("SR20_LOGDIR") else os.path.join(HERE, "logs")
DATA = os.path.join(os.environ["SR20_DATA"], "efb") if os.environ.get("SR20_DATA") else os.path.join(HERE, "data")

import fpl  # noqa: E402
import efb_lan  # noqa: E402

PORT = 47822                         # after SurfaceWatch 47820 and TAWS 47821 (47830 is HalAttack's)
DISCOVERY_PORT, XGPS_PORT = 63093, 49002
NAME = "SR20 Sim"
TICK = 0.2                           # XATT 5 Hz, XGPS every 5th tick
IPAD_KEEP_S = 600
SCAN_S = 2.0
RECENT_S = 600                       # a file shared just before the helper started still counts
PENDING_MAX_S = 3600                 # an unanswered plan older than this is dropped (and not restored)
MFD_ALIVE_S = 10                     # the MFD polls every 2 s while the sim runs
SEEN_MAX = 200
DEFAULTS = {"map": True, "lan_upload": True}
COLS = [("lat", "PLANE LATITUDE", "degrees"), ("lon", "PLANE LONGITUDE", "degrees"), ("alt", "PLANE ALTITUDE", "feet"),
        ("track", "GPS GROUND TRUE TRACK", "degrees"), ("gs", "GROUND VELOCITY", "knots"),
        ("hdg", "PLANE HEADING DEGREES TRUE", "degrees"), ("pitch", "PLANE PITCH DEGREES", "degrees"),
        ("bank", "PLANE BANK DEGREES", "degrees"), ("gps_valid", "L:SR20_TAWS_GPS_VALID", "number")]


def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    path = os.path.join(LOGDIR, "efb_server.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- settings and saved state

def load_settings(path):
    """-> (settings, error or None). Missing file: defaults. Malformed: defaults, and the error for the setup page."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        return dict(DEFAULTS), None
    except (OSError, ValueError) as e:
        log(f"settings: {path} is not valid JSON ({e}); using the defaults")
        return dict(DEFAULTS), f"{os.path.basename(path)} could not be read ({e}); using the defaults"
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if isinstance(d.get(k), bool):
            out[k] = d[k]
    url = d.get("shortcut_url")
    if isinstance(url, str) and url.startswith("https://www.icloud.com/shortcuts/"):
        out["shortcut_url"] = url
    return out, None


class State:
    """data/efb_state.json: one lock for every writer, temp file + os.replace, written only when something changed.
    An unreadable file counts as empty."""

    def __init__(self, path):
        self.path, self.lock, self.d, self.dirty = path, threading.Lock(), {}, False
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            self.d = d if isinstance(d, dict) else {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log(f"state: {path} unreadable ({e}); starting empty")

    def get(self, key, default=None):
        with self.lock:
            return self.d.get(key, default)

    def set(self, key, value):
        with self.lock:
            if self.d.get(key) == value and not self.dirty:
                return
            self.d[key] = value
            try:
                efb_lan.atomic_write(self.path, json.dumps(self.d, separators=(",", ":")))
                self.dirty = False
            except OSError as e:
                self.dirty = True            # retried on the next set, even of an unchanged value
                log(f"state: could not save ({e})")


# ---------------------------------------------------------------- moving map

def xgps(v):
    """Longitude, latitude, altitude m MSL, true track, ground speed m/s."""
    return "XGPS%s,%.5f,%.5f,%.1f,%.1f,%.1f" % (NAME, v["lon"], v["lat"], v["alt"] * 0.3048, v["track"] % 360.0, v["gs"] * 0.514444)


def xatt(v):
    """True heading, pitch (up +), roll (right +). The sim's PLANE PITCH DEGREES is nose-down positive and its PLANE BANK
    DEGREES left-wing-down positive, so both are negated."""
    return "XATT%s,%.1f,%.1f,%.1f" % (NAME, v["hdg"] % 360.0, -v["pitch"] + 0.0, -v["bank"] + 0.0)


class Ipads:
    """iPads running ForeFlight in the foreground, from their discovery broadcasts."""
    def __init__(self, state=None):
        self.seen = {}
        self.lock = threading.Lock()
        self.state = state
        self.last = state.get("last_ipad") if state else None

    def listen(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", DISCOVERY_PORT))
        except OSError as e:
            log(f"ForeFlight discovery port {DISCOVERY_PORT} not available ({e}): no moving map")
            return
        while True:
            try:
                data, addr = s.recvfrom(2048)
                msg = json.loads(data.decode("utf-8", "replace"))
            except (OSError, ValueError):
                continue
            if not isinstance(msg, dict) or msg.get("App") != "ForeFlight":
                continue
            with self.lock:
                new = addr[0] not in self.seen
                if new:
                    log(f"ForeFlight heard at {addr[0]}: sending the moving map there")
                self.seen[addr[0]] = time.monotonic()
                self.last = addr[0]
            if new and self.state:           # not every 5 s, nor back and forth between two iPads
                self.state.set("last_ipad", addr[0])

    def targets(self):
        now = time.monotonic()
        with self.lock:
            for ip in [ip for ip, t in self.seen.items() if now - t > IPAD_KEEP_S]:
                del self.seen[ip]
                log(f"ForeFlight at {ip} not heard for {IPAD_KEEP_S // 60} min: stopped sending")
            return list(self.seen)

    def heard(self):
        """-> the iPads heard in the last 10 minutes."""
        return self.targets()


# ---------------------------------------------------------------- flight plans from the iPad

def downloads_dir():
    """The user's Downloads folder (Known Folder, so a moved one is found too)."""
    try:
        import ctypes
        guid = (ctypes.c_byte * 16).from_buffer_copy(bytes.fromhex("90e24d373f126545916439c4925e467b"))   # FOLDERID_Downloads
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(p)) == 0:
            out = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            return out
    except Exception:  # noqa: BLE001
        pass
    return os.path.join(os.path.expanduser("~"), "Downloads")


class FplWatcher:
    PATTERN = "ForeFlight*.fpl"

    def __init__(self, folder, since, seen=(), on_seen=None):
        self.folder, self.since, self.on_seen = folder, since, on_seen
        seen = seen if isinstance(seen, list) else []
        self.done = {tuple(k) for k in seen if isinstance(k, (list, tuple)) and len(k) == 3
                     and isinstance(k[0], str) and isinstance(k[1], int) and isinstance(k[2], int)}

    def scan(self):
        out, now = [], time.time()
        for p in glob.glob(os.path.join(self.folder, self.PATTERN)):
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = (p, st.st_mtime_ns, st.st_size)
            if key in self.done or st.st_mtime < self.since:
                continue
            if now - st.st_mtime < 0.5:          # still being written: next scan
                continue
            self.done.add(key)
            out.append(p)
        if out and self.on_seen:
            keys = sorted(self.done, key=lambda k: k[1])[-SEEN_MAX:]
            self.done = set(keys)
            self.on_seen([list(k) for k in keys])
        return sorted(out, key=lambda p: os.stat(p).st_mtime)


class Pending:
    """One slot: the newest plan not yet answered on the MFD. seq is "<boot>-<n>", so a plan offered after a helper
    restart never repeats a seq the MFD has already answered."""

    def __init__(self, state=None, clock=time.time):
        self.lock, self.state, self.clock = threading.Lock(), state, clock
        self.boot, self.n = secrets.token_hex(4), 0
        self.seq, self.plan, self.t = "", None, 0.0
        self.last_poll = 0.0
        saved = state.get("pending") if state else None
        if isinstance(saved, dict) and isinstance(saved.get("plan"), dict) and isinstance(saved.get("seq"), str) \
                and isinstance(saved.get("time"), (int, float)) and clock() - saved["time"] < PENDING_MAX_S:
            self.seq, self.plan, self.t = saved["seq"], saved["plan"], float(saved["time"])
            log(f"plan {self.seq} ({self.plan.get('name')}) still unanswered from before the restart: offered again")

    def _save(self):
        if self.state:
            self.state.set("pending", {"seq": self.seq, "plan": self.plan, "time": self.t} if self.plan else None)

    def _expire(self):
        if self.plan and self.clock() - self.t >= PENDING_MAX_S:
            log(f"plan {self.seq} ({self.plan.get('name')}): unanswered for an hour, dropped")
            self.plan = None
            self._save()

    def _put(self, plan, source):
        self.n += 1
        self.seq = f"{self.boot}-{self.n}"
        self.plan = dict(plan, file=source)
        self.t = self.clock()
        self._save()
        return self.seq

    def put(self, plan, source):
        with self.lock:
            return self._put(plan, source)

    def put_error(self, plan, source):
        """An unreadable file: offered (so the pilot sees why) unless a good plan is waiting. Check and replace under
        one lock, so an upload arriving in between is never overwritten."""
        with self.lock:
            self._expire()
            if self.plan and not self.plan.get("error"):
                return None
            return self._put(plan, source)

    def get(self, poll=False):
        with self.lock:
            if poll:
                self.last_poll = time.monotonic()
            self._expire()
            return {"seq": self.seq, "plan": self.plan}

    def ack(self, seq, result, why=""):
        with self.lock:
            if seq != self.seq or self.plan is None:
                return False
            log(f"plan {seq} ({self.plan.get('name')}): {result}{' - ' + why if why else ''}")
            self.plan = None
            self._save()
            return True

    def mfd_alive(self):
        return time.monotonic() - self.last_poll < MFD_ALIVE_S


PENDING = Pending()


def describe(seq, plan, source):
    d = plan["dropped"]
    log(f"plan {seq} from {source}: {plan['name']!r} {' '.join(p['ident'] for p in plan['points'])} "
        f"(left out: {d['hold']} hold points, procedure {d['procedure']}, runway {d['runway']})")


def offer(plan, source):
    describe(PENDING.put(plan, source), plan, source)


def take_file(path):
    name = os.path.basename(path)
    try:
        offer(fpl.load(path), name)
    except (fpl.FplError, OSError) as e:
        seq = PENDING.put_error({"name": name, "error": str(e)}, name)
        log(f"plan {seq or '(not offered: a good plan is waiting)'} from {name} unreadable: {e}")


# ---------------------------------------------------------------- loopback service: the MFD and the setup page

class Ctx:
    """What the setup page shows and acts on (filled in by run())."""
    pairing = None
    lan = None
    ipads = None
    settings_error = None
    nonce = secrets.token_urlsafe(16)
    origins_logged = set()


CTX = Ctx()
SETUP_ORIGINS = (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 5

    def _send(self, code, data, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200, cors=False):
        extra = []
        origin = self.headers.get("Origin", "")
        if cors and (origin.startswith("coui://") or origin == "null"):   # the instruments only, never /setup
            extra.append(("Access-Control-Allow-Origin", origin))
        self._send(code, json.dumps(obj, separators=(",", ":")).encode("utf-8"), "application/json", extra)

    def _host_ok(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")   # DNS rebinding

    def _mfd_ok(self):
        """The MFD's routes. Probe P0 (2026-11-16, the author's sim): the MFD's Coherent fetch sends Origin coui://html_ui and
        no Sec-Fetch-* headers (User-Agent ... CoherentGT/2.0). Allowed: a coui:// Origin, or a local tool with neither
        an Origin nor Sec-Fetch-Site (the smoke tests, curl). Anything a browser page sends (an http(s) or null Origin,
        Sec-Fetch-Site) is refused. Header sets other than the MFD's are logged, up to 10."""
        h = self.headers
        origin = h.get("Origin")
        if origin is not None and origin.startswith("coui://"):
            return True
        seen = (origin, h.get("Sec-Fetch-Site"), h.get("Sec-Fetch-Mode"), h.get("Referer"), h.get("User-Agent"))
        if seen not in CTX.origins_logged and len(CTX.origins_logged) < 10:
            CTX.origins_logged.add(seen)
            log("loopback request without a coui:// origin: Origin=%r Sec-Fetch-Site=%r Sec-Fetch-Mode=%r Referer=%r User-Agent=%r" % seen)
        return origin is None and not h.get("Sec-Fetch-Site")

    def _page_ok(self, navigate_ok=False):
        """Setup routes: same-origin only. navigate_ok: the page itself may also be opened from a link elsewhere (a
        top-level navigation); another site can't read it, and its buttons still need Origin + nonce."""
        site = self.headers.get("Sec-Fetch-Site")
        if site in (None, "none", "same-origin"):
            return True
        return navigate_ok and self.headers.get("Sec-Fetch-Mode") == "navigate" and self.headers.get("Sec-Fetch-Dest") == "document"

    def do_GET(self):
        if not self._host_ok():
            self._json({"ok": False, "why": "host"}, 403)
            return
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in parse_qs(query).items()}
        if path in ("/pending", "/ack"):
            if not self._mfd_ok():
                self._json({"ok": False, "why": "origin"}, 403)
                return
            if path == "/pending":
                self._json(PENDING.get(poll=True), cors=True)
                return
            result = q.get("result", "")[:20]
            if result not in ("activated", "ignored", "failed"):
                self._json({"ok": False, "why": "result"}, 400, cors=True)
                return
            self._json({"ok": PENDING.ack(q.get("seq", "")[:40], result, q.get("why", "")[:200])}, cors=True)
        elif path == "/v1/alive":
            self._json({"ok": True})
        elif path.startswith("/setup"):
            if not self._page_ok(navigate_ok=path == "/setup"):
                self._json({"ok": False, "why": "origin"}, 403)
                return
            import efb_setup
            code, data, ctype, extra = efb_setup.get(path, q, CTX, PENDING)
            self._send(code, data, ctype, extra)
        else:
            self._json({"ok": False}, 404)

    def do_POST(self):
        if not self._host_ok():
            self._json({"ok": False, "why": "host"}, 403)
            return
        path = self.path.partition("?")[0]
        if not path.startswith("/setup/") or self.headers.get("Origin") not in SETUP_ORIGINS or not self._page_ok():
            self._json({"ok": False, "why": "origin"}, 403)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if not 0 <= n <= 512:
            self._json({"ok": False, "why": "length"}, 400)
            return
        body = self.rfile.read(n).decode("utf-8", "replace")
        nonce = {k: v[0] for k, v in parse_qs(body).items()}.get("nonce", "")
        import hmac
        if not hmac.compare_digest(nonce.encode(), CTX.nonce.encode()):
            self._json({"ok": False, "why": "nonce"}, 403)
            return
        import efb_setup
        self._json(efb_setup.post(path, CTX))

    def log_message(self, *a):
        pass


class _Server(http.server.ThreadingHTTPServer):
    # Python's HTTP server sets SO_REUSEADDR, which on Windows lets a second program bind a port already in use without
    # an error (HalAttack's 47830 was shared that way, 2026-11-02): exclusive instead, so a clash fails at start.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def start_http(port=PORT):
    srv = _Server(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"flight plan service and setup page on 127.0.0.1:{port}")
    return srv


# ---------------------------------------------------------------- main loop

def run(map_on=None, folder=None):
    global PENDING
    settings, CTX.settings_error = load_settings(os.path.join(DATA, "efb.json"))
    CTX.shortcut_url = settings.get("shortcut_url")
    if map_on is None:
        map_on = settings["map"]
    state = State(os.path.join(DATA, "efb_state.json"))
    PENDING = Pending(state)
    folder = folder or os.environ.get("SR20_FPL_DIR") or downloads_dir()
    watcher = FplWatcher(folder, since=time.time() - RECENT_S, seen=state.get("seen", []),
                         on_seen=lambda keys: state.set("seen", keys))
    log(f"EFB helper: watching {folder} for {FplWatcher.PATTERN}; moving map {'on' if map_on else 'off'}; "
        f"LAN upload {'on' if settings['lan_upload'] else 'off'}; data in {DATA}")
    start_http()
    ipads = Ipads(state)
    CTX.ipads = ipads
    CTX.pairing = efb_lan.Pairing(os.path.join(DATA, "efb_tokens.json"), log=log)
    CTX.lan = efb_lan.Lan(CTX.pairing, offer, PENDING.mfd_alive, log=log, inbox=os.path.join(DATA, "efb_inbox"))
    if settings["lan_upload"]:
        efb_lan.start(CTX.lan)
    else:
        CTX.lan.state = "off in efb.json"
    threading.Thread(target=ipads.listen, daemon=True).start()   # also tells the setup page which address reaches the iPad
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sm = br = None
    last_count, last_change, next_scan, tick, gps_seen, gps_was = 0, 0.0, 0.0, 0, False, True
    while True:
        t0 = time.monotonic()
        if t0 >= next_scan:
            next_scan = t0 + SCAN_S
            for p in watcher.scan():
                take_file(p)
        if not map_on:
            time.sleep(SCAN_S)
            continue
        targets = ipads.targets()
        if not targets:
            time.sleep(1.0)
            continue
        if sm is None:
            try:
                from probe_electrical import connect_sdk
                from sim_batch import BatchReader
                sm = connect_sdk()
                br = BatchReader(sm, COLS)
                last_count, last_change = 0, time.monotonic()
                log("SimConnect connected")
            except Exception:  # noqa: BLE001
                sm = None
                time.sleep(5)
                continue
        try:
            br.poll()
            if br.count != last_count:
                last_count, last_change = br.count, time.monotonic()
            elif time.monotonic() - last_change > 5:
                raise ConnectionError("sim stopped answering")
            v = br.latest
            if v:
                if v.get("gps_valid", 0) >= 1:
                    gps_seen = True
                gps_ok = not gps_seen or v.get("gps_valid", 0) >= 1
                if gps_ok != gps_was:
                    gps_was = gps_ok
                    log("GPS " + ("back: position sent again" if gps_ok else "failed: no position sent to the iPad"))
                lines = [xatt(v)] + ([xgps(v)] if tick % 5 == 0 and gps_ok else [])
                for ip in targets:
                    for line in lines:
                        try:
                            out.sendto(line.encode("ascii"), (ip, XGPS_PORT))
                        except OSError:
                            pass
                tick += 1
        except Exception as e:  # noqa: BLE001
            log(f"sim: {e.__class__.__name__}: {e}")
            try:
                sm.exit()
            except Exception:  # noqa: BLE001
                pass
            sm = br = None
            time.sleep(2)
            continue
        time.sleep(max(0.02, TICK - (time.monotonic() - t0)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-map", action="store_true", help="no moving map to the iPad")
    ap.add_argument("--dir", help="folder to watch for ForeFlight*.fpl (default: Downloads)")
    a = ap.parse_args()
    try:
        run(False if a.no_map else None, a.dir)
    except KeyboardInterrupt:
        pass
