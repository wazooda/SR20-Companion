"""The LAN side of the EFB helper: "Send to SR20" from the iPad without Tailscale (docs/plans/fpl_without_tailscale.md).

TCP 47823 on all IPv4 interfaces, private source addresses only:
    GET  /                     the fallback upload page (Safari: Camera on the setup page's QR)
    GET  /app.js, /app.css     its script and style (served as files so the page's CSP needs no 'unsafe-inline')
    GET  /v1/ping              {"ok": true}; a caller other than this PC counts as "an iPad reached this PC"
    POST /v1/pair?p=<secret>   the setup QR's secret -> text/plain "http://<host>/v1/fpl?t=<token>", which the
                               "SR20 setup" Shortcut (or the upload page) keeps and posts plans to from then on
    POST /v1/fpl?t=<token>     the .fpl as the raw body (or Authorization: Bearer <token>) -> a line of text for the
                               iPad: "Flight plan received: KSNA SLI KLGB. Press ENT on the MFD."

The secret is shown only on the PC (the loopback setup page), lasts 30 minutes and may be used more than once in that
time; "New code" ends it. Tokens are 32 random bytes, kept as SHA-256 hashes; "Forget all iPads" clears them. The home
LAN is trusted: plain HTTP can be sniffed, and the worst a stolen token does is offer a plan the pilot must accept on
the MFD (ENT, ENT, OK). Nothing received is run or written outside the data folder.

Connections: at most SLOTS at once, the rest wait in the listen backlog (a slot is taken before accept()); each one
gets DEADLINE_S for its headers and body together, then its socket is shut and closed. Bodies need one plain
Content-Length (no chunked), at most fpl.MAX_BYTES. A source address that fails the secret or token backs off 1 s,
doubling to 60 s, answered at once with 429 + Retry-After (no slot held while waiting).
"""
import hashlib
import re
import hmac
import ipaddress
import json
import math
import os
import secrets
import socket
import socketserver
import threading
import time
import http.server
from urllib.parse import parse_qs

import fpl

PORT = 47823
SLOTS = 4
DEADLINE_S = 10.0
SECRET_S = 30 * 60
SECRET_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L
SECRET_LEN = 10
KEY_LEN = 8
KEY_RE = re.compile(r"^/([A-Za-z0-9]{8})$")
MAX_TOKENS = 20
PAIR_MAX = 64
BACKOFF_MAX_S = 60
BACKOFF_TABLE = 256
PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                                              "100.64.0.0/10", "127.0.0.0/8")]

MSG_UNPAIRED = "This iPad isn't connected to this PC. On the PC, open SR20 iPad setup and scan code 2 again."
MSG_CODE_OLD = "That code has expired. On the PC, reload SR20 iPad setup and scan the new code."


def is_private(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version != 4:
        mapped = getattr(a, "ipv4_mapped", None)
        if mapped is None:
            return False
        a = mapped
    return any(a in n for n in PRIVATE)


def host_ok(host_header):
    """The Host header must name an IPv4 address or localhost (no DNS name: DNS rebinding)."""
    h = (host_header or "").strip()
    if not h:
        return False
    name, _, port = h.rpartition(":") if h.count(":") == 1 else (h, "", "")
    if port and not port.isdigit():
        return False
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).version == 4
    except ValueError:
        return False


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class Pairing:
    """The setup code (secret) and the paired devices' token hashes. The helper process is the only writer."""

    def __init__(self, path, log=print, clock=time.time):
        self.path, self.log, self.clock = path, log, clock
        self.lock = threading.Lock()
        self.secret, self.secret_until = None, 0.0
        self.key = None              # the send key the setup page shows (plain text in memory only)
        self.hashes = []
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            hs = d.get("hashes") if isinstance(d, dict) else None
            self.hashes = [h for h in hs if isinstance(h, str) and len(h) == 64][-MAX_TOKENS:] if isinstance(hs, list) else []
            k = d.get("key") if isinstance(d, dict) else None
            if isinstance(k, str) and len(k) == KEY_LEN and hashlib.sha256(k.encode()).hexdigest() in self.hashes:
                self.key = k
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as e:
            log(f"pairing: {os.path.basename(path)} unreadable ({e}); no iPads paired")

    def _save(self, hashes, key=None):
        """Raises OSError: a pairing or a Forget all only counts once it is on disk. The current send key is kept in
        plain text too (the setup page must show the same address after a restart); it is no more secret than this
        PC's own files."""
        try:
            atomic_write(self.path, json.dumps({"hashes": hashes, "key": key}))
        except OSError as e:
            self.log(f"pairing: could not save {self.path}: {e}")
            raise

    def current_secret(self, new=False):
        """-> (secret, seconds left). A page load reuses the code until it expires; new=True replaces it."""
        with self.lock:
            now = self.clock()
            if new or not self.secret or now >= self.secret_until:
                self.secret = "".join(secrets.choice(SECRET_ALPHABET) for _ in range(SECRET_LEN))
                self.secret_until = now + SECRET_S
            return self.secret, self.secret_until - now

    def pair(self, given):
        """-> token, or None when the code is wrong ("bad") / expired ("old")."""
        given = (given or "").strip().upper()
        with self.lock:
            if not self.secret or not given:
                return None, "bad"
            if not hmac.compare_digest(given.encode(), self.secret.encode()):
                return None, "bad"
            if self.clock() >= self.secret_until:
                return None, "old"
            token = secrets.token_urlsafe(32)
            hashes = (self.hashes + [hashlib.sha256(token.encode()).hexdigest()])[-MAX_TOKENS:]
            try:
                self._save(hashes, self.key)
            except OSError:
                return None, "save"
            self.hashes = hashes
            return token, "ok"

    def check(self, token):
        if not token:
            return False
        h = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            return any(hmac.compare_digest(h, x) for x in self.hashes)

    def send_key(self, new=False):
        """-> the short send key the setup page shows, for the address http://<pc>:47823/<KEY> typed once into the
        "Send to SR20" Shortcut (no QR scan, no second Shortcut). 8 characters from SECRET_ALPHABET (~40 bits), kept
        only as a hash on disk; the backoff makes guessing it on a home network impractical. new=True shows a new key; earlier keys keep working until Forget all. The key survives a helper restart.
        Raises OSError if it can't be saved."""
        with self.lock:
            if self.key and not new:
                return self.key
            key = "".join(secrets.choice(SECRET_ALPHABET) for _ in range(KEY_LEN))
            hashes = (self.hashes + [hashlib.sha256(key.encode()).hexdigest()])[-MAX_TOKENS:]
            self._save(hashes, key)
            self.hashes, self.key = hashes, key
            return key

    def forget_all(self):
        """-> how many were forgotten. Raises OSError if the empty list couldn't be saved (nothing is forgotten)."""
        with self.lock:
            n = len(self.hashes)
            self._save([])
            self.hashes, self.key = [], None
            return n

    def count(self):
        with self.lock:
            return len(self.hashes)


class Backoff:
    def __init__(self, clock=time.monotonic):
        self.clock, self.lock, self.t = clock, threading.Lock(), {}

    def wait_s(self, ip):
        with self.lock:
            e = self.t.get(ip)
            return max(0.0, e[1] - self.clock()) if e else 0.0

    def fail(self, ip):
        with self.lock:
            n = self.t.get(ip, (0, 0.0))[0] + 1
            self.t[ip] = (n, self.clock() + min(BACKOFF_MAX_S, 2 ** (n - 1)))
            if len(self.t) > BACKOFF_TABLE:
                del self.t[min(self.t, key=lambda k: self.t[k][1])]

    def ok(self, ip):
        with self.lock:
            self.t.pop(ip, None)


class Lan:
    """Everything the LAN handler needs from the helper, in one place (and replaceable in the tests)."""

    def __init__(self, pairing, offer, mfd_alive, log=print, deadline_s=DEADLINE_S, inbox=None):
        self.pairing, self.offer, self.mfd_alive, self.log = pairing, offer, mfd_alive, log
        self.deadline_s, self.inbox = deadline_s, inbox
        self.backoff = Backoff()
        self.reached = None          # (ip, time) of the last caller other than this PC
        self.state = "not started"
        self.own = local_addresses()
        self.last_plan = None        # (ip, time, "KSNA SLI KLGB") for the setup page

    def keep(self, data):
        if not self.inbox:
            return
        try:
            os.makedirs(self.inbox, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + f"-{secrets.token_hex(2)}.fpl"
            with open(os.path.join(self.inbox, name), "wb") as fh:
                fh.write(data)
            old = sorted(f for f in os.listdir(self.inbox) if f.endswith(".fpl"))[:-20]
            for f in old:
                os.remove(os.path.join(self.inbox, f))
        except OSError:
            pass


PAGES = {"/": ("upload.html", "text/html; charset=utf-8"), "/app.js": ("upload.js", "text/javascript; charset=utf-8"),
         "/app.css": ("upload.css", "text/css; charset=utf-8"), "/connect": ("connect.html", "text/html; charset=utf-8"),
         "/connect.js": ("connect.js", "text/javascript; charset=utf-8")}


def _page_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class LanHandler(http.server.BaseHTTPRequestHandler):
    server_version = "SR20"
    sys_version = ""
    protocol_version = "HTTP/1.0"     # one request per connection

    def setup(self):
        self.lan = self.server.lan
        self._body_read = False
        self.request.settimeout(self.lan.deadline_s)
        self._timer = threading.Timer(self.lan.deadline_s, self._kill)
        self._timer.daemon = True
        self._timer.start()
        super().setup()

    def _kill(self):
        for f in (lambda: self.request.shutdown(socket.SHUT_RDWR), self.request.close):
            try:
                f()
            except OSError:
                pass

    def finish(self):
        self._timer.cancel()
        try:
            super().finish()
        except OSError:
            pass

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=()):
        if code >= 400:
            self._drain()
        data = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass
        self.close_connection = True

    def _gate(self):
        """Source, Host and backoff checks; -> the source ip, or None when a reply has been sent."""
        ip = self.client_address[0]
        path = self.path.partition("?")[0]
        shown = "/<key>" if KEY_RE.match(path) else path        # never the key, the code or a token
        self.lan.log(f"LAN: {ip} {self.command} {shown}")
        if not is_private(ip):
            self._send(403, "Only devices on your home network can send to the SR20.")
            return None
        if not host_ok(self.headers.get("Host")):
            self._send(403, "Use the PC's address, as the setup page shows it.")
            return None
        w = self.lan.backoff.wait_s(ip)
        if w > 0:
            s = math.ceil(w)
            self._send(429, f"Too many tries. Wait {s} seconds and try again.", extra=[("Retry-After", str(s))])
            return None
        return ip

    def _reached(self, ip):
        """An iPad (not this PC itself) got through: the setup page's proof that the network and firewall allow it."""
        if ip not in self.lan.own:
            self.lan.reached = (ip, time.time())

    def _length(self):
        """-> the one plain ASCII Content-Length, None when absent, -1 when unreadable."""
        lens = self.headers.get_all("Content-Length") or []
        if not lens:
            return None
        v = lens[0].strip()
        if len(set(x.strip() for x in lens)) != 1 or not (v.isascii() and v.isdigit()):
            return -1
        return int(v)

    def _drain(self):
        """Before an early error reply to a POST: read (and drop) the body the client is still sending, or Windows
        resets the connection and the iPad shows a network error instead of our text. The deadline bounds it."""
        if self._body_read or self.command != "POST" or self.headers is None or self.headers.get("Transfer-Encoding"):
            return
        self._body_read = True
        n = self._length()
        if not n or n < 0 or n > fpl.MAX_BYTES:     # nothing to drain, or too big to be worth it (413 at once)
            return
        left = n
        try:
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass

    def _query(self):
        path, _, q = self.path.partition("?")
        return path, {k: v[0] for k, v in parse_qs(q).items()}

    def _body(self, limit, empty_ok=False):
        """-> bytes, or None when a reply has been sent. empty_ok: no Content-Length means an empty body."""
        if self.headers.get("Transfer-Encoding"):
            self._send(501, "Send the file with a plain length (no chunked upload).")
            return None
        n = self._length()
        if n is None:
            if empty_ok:
                self._body_read = True
                return b""
            self._send(411, "The file arrived without a length.")
            return None
        if n < 0:
            self._send(400, "The file's length was not readable.")
            return None
        if n > limit:
            self._send(413, "That file is too large to be a flight plan.")
            return None
        self._body_read = True
        data, left = b"", n
        while left > 0:
            try:
                chunk = self.rfile.read(left)
            except OSError:            # the deadline shut the socket, or the client went away
                self.close_connection = True
                return None
            if not chunk:
                self._send(400, "The file arrived incomplete.")
                return None
            data += chunk
            left -= len(chunk)
        return data

    def do_GET(self):
        if self._gate() is None:
            return
        path, _ = self._query()
        if path == "/v1/ping":
            self._reached(self.client_address[0])
            self._send(200, json.dumps({"ok": True}), "application/json")
        elif path in PAGES:
            name, ctype = PAGES[path]
            try:
                with open(os.path.join(_page_dir(), name), "rb") as fh:
                    data = fh.read()
            except OSError:
                self._send(404, "not found")
                return
            self._send(200, data, ctype, extra=[
                ("Content-Security-Policy", "default-src 'none'; script-src 'self'; connect-src 'self'; "
                                            "style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
                ("Referrer-Policy", "no-referrer")])
        else:
            self._send(404, "not found")

    def do_POST(self):
        ip = self._gate()
        if ip is None:
            return
        path, q = self._query()
        if path == "/v1/pair":
            body = self._body(PAIR_MAX, empty_ok="p" in q)
            if body is None:
                return
            given = q.get("p") or body.decode("ascii", "replace").strip()
            if given.startswith("p="):
                given = given[2:]
            token, why = self.lan.pairing.pair(given)
            if why == "save":
                self._send(500, "This PC couldn't save the setup. Try again; if it keeps failing, restart the SR20 Companion.")
                return
            if token is None:
                self.lan.backoff.fail(ip)
                self.lan.log(f"LAN: pairing refused for {ip} ({why})")
                self._send(401, MSG_CODE_OLD if why == "old" else MSG_UNPAIRED)
                return
            self.lan.backoff.ok(ip)
            self._reached(ip)
            host = self.headers.get("Host").strip()
            self.lan.log(f"LAN: {ip} paired ({self.lan.pairing.count()} devices)")
            self._send(200, f"http://{host}/v1/fpl?t={token}")
        elif path == "/v1/fpl" or KEY_RE.match(path):
            auth = self.headers.get("Authorization", "")
            m = KEY_RE.match(path)
            token = m.group(1).upper() if m else (q.get("t") or (auth[7:].strip() if auth.lower().startswith("bearer ") else ""))
            if not self.lan.pairing.check(token):     # before the body is read
                self.lan.backoff.fail(ip)
                self._send(401, MSG_UNPAIRED)
                return
            self.lan.backoff.ok(ip)
            body = self._body(fpl.MAX_BYTES)
            if body is None:
                return
            try:
                plan = fpl.load_bytes(body)
            except fpl.FplError as e:
                self.lan.log(f"LAN: plan from {ip} unreadable: {e}")
                self._send(400, f"That file couldn't be read as a flight plan ({e}).")
                return
            self.lan.keep(body)
            self._reached(ip)
            self.lan.offer(plan, f"iPad {ip}")
            route = " ".join(p["ident"] for p in plan["points"])
            self.lan.last_plan = (ip, time.time(), route)
            if self.lan.mfd_alive():
                self._send(200, f"Flight plan received: {route}. Press ENT on the MFD.")
            else:
                self._send(200, f"Flight plan received: {route}. It will appear on the MFD once the sim is running.")
        else:
            self._send(404, "not found")


class LanServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """A slot is taken before accept(), so a busy server leaves new connections in the backlog."""
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 32

    def __init__(self, addr, lan, slots=SLOTS):
        self.lan = lan
        self.slots = threading.BoundedSemaphore(slots)
        self._held = set()
        self._held_lock = threading.Lock()
        super().__init__(addr, LanHandler)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        """A connection cut by its deadline or by the client: nothing to report, nothing to print."""
        import sys
        e = sys.exc_info()[1]
        if not isinstance(e, OSError):
            self.lan.log(f"LAN: request from {client_address[0]} failed: {e.__class__.__name__}: {e}")

    def verify_request(self, request, client_address):
        """Not from the home network: closed before any header is read, so it never holds a slot for long."""
        return is_private(client_address[0])

    def get_request(self):
        self.slots.acquire()
        try:
            req, addr = self.socket.accept()
        except BaseException:
            self.slots.release()
            raise
        with self._held_lock:
            self._held.add(id(req))
        return req, addr

    def close_request(self, request):
        try:
            super().close_request(request)
        finally:
            with self._held_lock:
                held = id(request) in self._held
                self._held.discard(id(request))
            if held:
                self.slots.release()


def start(lan, host="0.0.0.0", port=PORT):
    """-> the server, or None when the port could not be bound (the helper carries on without it)."""
    try:
        srv = LanServer((host, port), lan)
    except OSError as e:
        lan.state = f"failed: port {port} is in use or blocked ({e.strerror or e})"
        lan.log(f"LAN upload listener not started: {e}")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    lan.state = "ready"
    lan.log(f"LAN upload listener on {host}:{port}")
    return srv


def local_addresses():
    out = {"127.0.0.1"}
    try:
        out.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return out


def address_for(peer_ip):
    """The local IPv4 address this PC uses to reach peer_ip (no packet is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((peer_ip, 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def candidates(last_ipad=None):
    """-> [best, others...]. Home-network addresses (192.168/16, 10/8, 172.16/12) come before VPN ones (Tailscale's
    100.64/10): someone without Tailscale on the iPad can only use the home-network address, and a PC whose home traffic
    is routed through Tailscale reaches even a home-network iPad through 100.x (the author's PC, 2026-11-16). Among home
    addresses, the one that reaches the iPad last heard comes first, then the default route's."""
    seen, order = [], []
    first = address_for(last_ipad) if last_ipad else None
    d = address_for("192.0.2.1")
    try:
        allv4 = [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        allv4 = []
    for a in ([first] if first else []) + ([d] if d else []) + sorted(allv4, key=lambda x: not x.startswith("192.168.")):
        if a and a not in seen and not a.startswith("127.") and is_private(a):
            seen.append(a)

    def vpn(a):
        return ipaddress.ip_address(a) in ipaddress.ip_network("100.64.0.0/10")
    # stable sort: home before VPN; the address that reaches the iPad first; then 192.168/16 (the usual home LAN)
    return sorted(seen, key=lambda a: (vpn(a), a != first, not a.startswith("192.168.")))
