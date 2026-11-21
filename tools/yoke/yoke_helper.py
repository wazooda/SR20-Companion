"""SR20 yoke helper: owns the RealSimGear Cirrus yoke's serial port and drives its feel from the sim
(docs/plans/yoke_ap_breakout.md §3.1). the author's custom setup.

- Spring centre {C/{D = aileron/elevator trim (what the RealSimGear map did).
- Feel: stiff while AUTOPILOT MASTER = 1, loose otherwise (presets + spring scalings from yoke_feel.json, reloaded when
  the file changes). The PFD plugin's break-out (tools/yoke/sr20_ap_breakout.js) disconnects the autopilot when the
  pilot pushes past the threshold; the helper then goes loose because the autopilot is off.
- No sim / sim lost: loose, centre held where it was. Exit: loose, centre held, port closed.
- Logs to tools/yoke/logs/: helper log plus a 10 Hz CSV (trims, AP, gate, displacements, surfaces) for tuning.

Must start before the sim launches the RealSimGear Device Interface, which otherwise opens the port (it then keeps
retrying while the helper holds it, and runs the other RSG panels as usual). Run without -I.

    python tools\\yoke\\yoke_helper.py --supervise     restart on exit (Windows start-up shortcut)
    python tools\\yoke\\yoke_helper.py                 run once in the console
    python tools\\yoke\\yoke_helper.py --fake-sim      scripted AP/trim sequence, prints the serial lines, no port
"""
import argparse
import datetime
import json
import math
import os
import subprocess
import sys
import collections
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # tools/ for probe_electrical, sim_batch
# SR20_DATA / SR20_LOGDIR: set by the SR20 Companion (tools/companion); from the repo both stay beside this file
CONFIG = os.path.join(os.environ.get("SR20_DATA") or HERE, "yoke_feel.json")
LOGDIR = os.path.join(os.environ["SR20_LOGDIR"], "yoke") if os.environ.get("SR20_LOGDIR") else os.path.join(HERE, "logs")
VID, PID = 0x32B6, 0x0202
SCALE_MIN, SCALE_MAX = 0.8, 1.5          # firmware accepts about this range (measured 2026-10-23)
DEFAULT = {"loose": {"preset": 1, "near": 0.8, "main": 0.8}, "stiff": {"preset": 3, "near": 1.5, "main": 1.5}, "trim_gain": 1.0}



def prune_day_logs(logdir, keep_days):
    """Delete yoke_YYYY-MM-DD*.csv older than keep_days (10 Hz logs grow ~25 MB per flown hour; release review 2026-11-20)."""
    cutoff = time.time() - keep_days * 86400
    try:
        for name in os.listdir(logdir):
            if name.startswith("yoke_2") and name.endswith(".csv"):
                p = os.path.join(logdir, name)
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
    except OSError:
        pass

def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    path = os.path.join(LOGDIR, "yoke_helper.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    try:
        c = json.load(open(CONFIG, encoding="utf-8"))
    except (OSError, ValueError) as e:
        log(f"config unreadable ({e}), using defaults")
        return DEFAULT
    out = {"trim_gain": DEFAULT["trim_gain"]}
    for k in ("loose", "stiff"):
        d = dict(DEFAULT[k])
        d.update({kk: v for kk, v in (c.get(k) or {}).items() if kk in d})
        d["preset"] = int(d["preset"]) if int(d["preset"]) in (1, 2, 3) else DEFAULT[k]["preset"]
        for kk in ("near", "main"):
            v = float(d[kk])
            d[kk] = min(max(v, SCALE_MIN), SCALE_MAX) if math.isfinite(v) else DEFAULT[k][kk]
        out[k] = d
    g = c.get("trim_gain", 1.0)
    out["trim_gain"] = float(g) if isinstance(g, (int, float)) and 0 < g <= 1.5 else 1.0
    # ESP push: centre offset = esp_gain x the ESP axis force (L:SR20_ESP_*_FORCE, about 0..1), capped at esp_max
    for k, dflt, lo, hi in (("esp_gain", 1.5, 0.0, 4.0), ("esp_max", 0.6, 0.0, 0.9), ("esp_rate_on", 1.0, 0.05, 10.0), ("esp_rate_off", 0.4, 0.05, 10.0), ("shaker", 0.5, 0.0, 1.0), ("trim_smooth_s", 1.5, 0.0, 10.0), ("trim_deadband", 0.005, 0.001, 1.0)):
        x = c.get(k, dflt)
        out[k] = float(x) if isinstance(x, (int, float)) and lo <= x <= hi else dflt
    out["clear_disengage_latch"] = bool(c.get("clear_disengage_latch", True))
    return out


class Yoke:
    """The yoke's CDC serial port: channel lines ({A..{D) and the service console (!e...)."""

    def __init__(self, port=None, fake=False):
        self.port_name, self.fake, self.s = port, fake, None
        self.feel = None          # name of the feel last applied
        self.centre = [None, None]
        self.shake = None
        self.rx_total, self.rx_logged, self.next_check = 0, 0.0, 0.0
        self.last_rx, self.silent_reported = time.time(), False
        self.sent = collections.deque(maxlen=40)   # (time, line): dumped to the log when the firmware goes silent

    def drain(self):
        """Read whatever the yoke has sent. Unread replies fill the CDC buffers and the firmware then blocks on its
        serial writes, slowing and finally freezing its HID reports (2026-10-24: axes dead in Windows until a power
        cycle, after ~5 h with the port open and only read during a feel change)."""
        if self.fake or not self.s:
            return
        n = self.s.in_waiting
        if n:
            data = self.s.read(n)
            self.last_rx = time.time()
            if self.silent_reported:
                log(f"yoke talking again (after {getattr(self, 'reopens', 0)} port reopen(s))")
                self.silent_reported = False
            self.rx_total += len(data)
            self.parse_temp(data)
            if time.time() - self.rx_logged > 600:
                log(f"yoke sent {self.rx_total} bytes since the port opened (drained), latest {data[-60:]!r}")
                self.rx_logged = time.time()

    def console(self, cmd, wait=0.8):
        """Send one service-console line and return what the yoke prints within `wait` s."""
        if self.fake or not self.s:
            return ""
        self.drain()
        self.send(cmd)
        buf, end = b"", time.time() + wait
        while time.time() < end:
            buf += self.s.read(1024)
        return buf.decode("ascii", "replace")

    def parse_temp(self, data):
        """`!t1` telemetry: lines `TEMP_MAIN=30.8` (deg C; firmware derates from 60, zeroes the axis at 70)."""
        self._rx_tail = (getattr(self, "_rx_tail", b"") + data)[-400:]
        *lines, self._rx_tail = self._rx_tail.split(b"\n")
        for ln in lines:
            if ln.startswith(b"TEMP_MAIN="):
                try:
                    t = float(ln[10:].strip())
                except ValueError:
                    continue
                self.temp = t
                now = time.time()
                if now - getattr(self, "_temp_logged", 0) >= 5:
                    self._temp_logged = now
                    tpath = os.path.join(LOGDIR, "yoke_temp.csv")
                    try:   # capped: one 2 MB generation (release review 2026-11-20)
                        if os.path.getsize(tpath) > 2_000_000:
                            os.replace(tpath, tpath + ".1")
                    except OSError:
                        pass
                    with open(tpath, "a", encoding="utf-8") as f:
                        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')},{t:.1f},{self.feel}\n")
                band = 2 if t >= 60 else 1 if t >= 50 else 0
                if band != getattr(self, "_temp_band", 0):
                    log(f"yoke motor temperature {t:.1f} C" + (" - firmware derate range (60-70 C)" if band == 2 else " - warm" if band == 1 else " - back to normal"))
                    self._temp_band = band

    def check_present(self):
        """Raise when the yoke has left the USB bus (power cycle / unplug), so the port is reopened."""
        if self.fake or not self.s or time.time() < self.next_check:
            return
        self.next_check = time.time() + 5
        from serial.tools import list_ports
        if self.s.port not in [p.device for p in list_ports.comports()]:
            raise OSError(f"{self.s.port} gone from the USB bus")

    def find_port(self):
        if self.port_name:
            return self.port_name
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.vid == VID and p.pid == PID:
                return p.device
        return None

    def open(self):
        if self.fake:
            return True
        import serial
        name = self.find_port()
        if not name:
            if not getattr(self, "_absent_logged", False):
                log(f"yoke (USB {VID:04X}:{PID:04X}) not found, retrying every 5 s")
                self._absent_logged = True
            return False
        self._absent_logged = False
        try:
            self.s = serial.Serial(name, 115200, timeout=0.05)
        except serial.SerialException as e:
            if not getattr(self, "_busy_logged", False):
                log(f"{name} busy ({e.__class__.__name__}); is the RealSimGear Device Interface holding the yoke? retrying")
                self._busy_logged = True
            return False
        self._busy_logged = False
        self.rx_total, self.rx_logged = 0, 0.0
        self.last_rx = time.time() + 5   # grace for the first telemetry (silent_reported survives a wake-up reopen)
        time.sleep(0.3)
        self.s.reset_input_buffer()
        self.feel, self.centre, self.shake = None, [None, None], None   # the yoke may have been power-cycled: send everything again
        self.send("!t1")   # temperature telemetry stream (TEMP_MAIN=...), parsed in drain()
        log(f"yoke port {name} open")
        return True

    def close(self):
        if self.s:
            try:
                self.s.close()
            finally:
                self.s = None

    def send(self, line):
        if self.fake:
            print("  >", line)
            return
        self.sent.append((time.time(), line))
        self.s.write((line + "\r\n").encode("ascii"))
        time.sleep(0.02)

    def readback(self):
        """`!e` -> {'pitch': (near, main, x), 'roll': (...)} or None."""
        if self.fake:
            return None
        for wait in (1.0, 1.5):   # the first reply after opening the port sometimes comes late
            vals = self._readback(wait)
            if vals:
                return vals
        return None

    def _readback(self, wait):
        self.s.reset_input_buffer()
        self.send("!e")
        buf, end = b"", time.time() + wait
        while time.time() < end:
            buf += self.s.read(512)
            roll = buf.find(b"* Roll:")
            if roll >= 0 and buf.find(b"Crossover Point (x):", roll) >= 0:
                break
        time.sleep(0.05)
        buf += self.s.read(512)
        txt = buf.decode("ascii", "replace")
        vals = {}
        for axis in ("Pitch", "Roll"):
            i = txt.find(f"* {axis}:")
            if i < 0:
                return None
            nums = []
            for tag in ("Near Scaling (1):", "Main Scaling (2):", "Crossover Point (x):"):
                j = txt.find(tag, i)
                if j < 0:
                    return None
                nums.append(float(txt[j + len(tag):].split()[0]))
            vals[axis.lower()] = tuple(nums)
        return vals

    def apply_feel(self, name, f):
        p = f["preset"]
        self.send(f"{{A{p}")
        self.send(f"{{B{p}")
        for ax in ("r", "p"):
            self.send(f"!e{ax}1{f['near']:g}")
            self.send(f"!e{ax}2{f['main']:g}")
        rb = self.readback()
        if not self.fake:
            want = (f["near"], f["main"])
            good = rb is not None and all(abs(rb[a][0] - want[0]) < 0.01 and abs(rb[a][1] - want[1]) < 0.01 for a in ("pitch", "roll"))
            if not good:
                log(f"feel {name}: read-back mismatch {rb}")
                return False
        self.feel = name
        log(f"feel -> {name} (preset {p}, near {f['near']}, main {f['main']})")
        return True

    def set_shaker(self, amp):
        """{H 0..1: the firmware's vibration channel (unused by RealSimGear; docs/rsg_yoke_reverse_engineering.md)."""
        amp = round(min(max(amp, 0.0), 1.0), 2)
        if amp != self.shake:
            self.send(f"{{H{amp:g}")
            self.shake = amp

    def set_centre(self, roll, pitch, deadband=0.002):
        """Each new centre is a small step the hand feels, so only send moves of at least `deadband`."""
        for i, (ch, v) in enumerate((("C", roll), ("D", pitch))):
            v = round(min(max(v, -1.0), 1.0), 3)
            if self.centre[i] is None or abs(v - self.centre[i]) >= deadband:
                self.send(f"{{{ch}{v:g}")
                self.centre[i] = v


COLS = [("ail_trim", "AILERON TRIM PCT", "percent over 100"), ("elev_trim", "ELEVATOR TRIM PCT", "percent over 100"),
        ("ap", "AUTOPILOT MASTER", "Bool"), ("latch", "AUTOPILOT DISENGAGED", "Bool"), ("gate", "L:SR20_YOKE_GATE", "Number"),
        ("disp_p", "L:SR20_YOKE_DISP_PITCH", "Number"), ("disp_r", "L:SR20_YOKE_DISP_ROLL", "Number"),
        ("elev_pos", "ELEVATOR POSITION", "Position"), ("ail_pos", "AILERON POSITION", "Position"),
        ("bank", "PLANE BANK DEGREES", "degrees"), ("pitch", "PLANE PITCH DEGREES", "degrees"),
        ("esp_eng", "L:SR20_ESP_ENGAGED", "Number"), ("esp_r", "L:SR20_ESP_ROLL_FORCE", "Number"),
        ("esp_p", "L:SR20_ESP_PITCH_FORCE", "Number"), ("stall", "STALL WARNING", "Bool"), ("on_ground", "SIM ON GROUND", "Bool")]


class Sim:
    def __init__(self):
        self.sm, self.br, self.last_count, self.last_change = None, None, 0, 0.0

    def connect(self):
        try:
            from probe_electrical import connect_sdk
            from sim_batch import BatchReader
            self.sm = connect_sdk()
            self.br = BatchReader(self.sm, COLS)
            self.last_count, self.last_change = 0, time.time()
            log("SimConnect connected")
            return True
        except Exception as e:   # sim not running
            self.sm = self.br = None
            if not getattr(self, "_down_logged", False):
                log(f"no sim ({e.__class__.__name__}), waiting")
                self._down_logged = True
            return False

    def read(self):
        """Latest values, or None when the sim has stopped answering for 3 s."""
        self._down_logged = False
        try:
            self.br.poll()
        except Exception:
            return None
        time.sleep(0.03)
        if self.br.count != self.last_count:
            self.last_count, self.last_change = self.br.count, time.time()
        elif time.time() - self.last_change > 3:
            return None
        return self.br.latest or None

    def close(self):
        try:
            if self.sm:
                self.sm.exit()
        except Exception:
            pass
        self.sm = self.br = None


class EspPush:
    """ESP on the yoke: move the spring centre the way the ESP servo pushes (2026-10-24, the author: "strong strength to get it
    back"). Signs: ESP roll force + = leftward roll, pitch force + = nose down (sr20_esp curves); yoke {C + = right
    (like AILERON TRIM PCT), {D + = aft (like ELEVATOR TRIM PCT). The sim input bias stays as well.
    Fail-safe: no sim data -> 0; ESP not engaged -> 0; a force held non-zero for 60 s (stale L-var after a PFD crash;
    auto-LVL takes over after 20 s anyway) -> 0 until it drops. logs/push_test.txt "roll 0.3" / "pitch -0.3" pushes
    the yoke for 2 s (sign check on the ground)."""
    TEST_FILE = os.path.join(LOGDIR, "push_test.txt")

    def __init__(self):
        self.active, self.last_active, self.since, self.test = False, 0.0, None, None
        self.cur, self.t_last = [0.0, 0.0], 0.0

    def offsets(self, v, cfg):
        now = time.time()
        self._read_test(now)
        if self.test and now < self.test[2]:
            self.active = False
            self.cur = [self.test[0], self.test[1]]
            return self.test[0], self.test[1]
        fr = v.get("esp_r", 0.0) if v else 0.0
        fp = v.get("esp_p", 0.0) if v else 0.0
        if not v or v.get("esp_eng", 0) < 0.5:
            fr = fp = 0.0
        on = abs(fr) > 0.01 or abs(fp) > 0.01
        self.since = (self.since or now) if on else None
        if on and now - self.since > 60:
            fr = fp = 0.0
            on = False
        if on:
            self.last_active = now
        was = self.active
        self.active = now - self.last_active < 3   # keep the stiff spring 3 s after the push ends
        if self.active != was:
            log(f"ESP push {'on' if self.active else 'off'}")
        lim, k = cfg["esp_max"], cfg["esp_gain"]
        clip = lambda x: min(max(x, -lim), lim)
        # slew-limited (the author 2026-10-24: a loud click when the push slammed in at full offset, another when it
        # dropped out): build up at esp_rate_on, let go at esp_rate_off (offset units per second)
        dt = min(now - self.t_last, 0.2) if self.t_last else 0.0
        self.t_last = now
        out = []
        for i, target in enumerate((clip(-k * fr), clip(-k * fp))):
            cur = self.cur[i]
            rate = cfg["esp_rate_on"] if abs(target) > abs(cur) else cfg["esp_rate_off"]
            step = rate * dt
            self.cur[i] = cur + max(-step, min(step, target - cur))
            out.append(self.cur[i])
        return out[0], out[1]

    def _read_test(self, now):
        if not os.path.exists(self.TEST_FILE):
            return
        try:
            axis, val = open(self.TEST_FILE, encoding="ascii").read().split()[:2]
            os.remove(self.TEST_FILE)
            x = min(max(float(val), -0.6), 0.6)
        except (OSError, ValueError):
            return
        self.test = (x, 0.0, now + 2) if axis == "roll" else (0.0, x, now + 2)
        log(f"push test {axis} {x:+.2f} for 2 s")


class CsvLog:
    def __init__(self):
        self.f, self.day, self.next_t = None, None, 0.0

    def row(self, v, feel):
        now = time.time()
        if now < self.next_t:
            return
        self.next_t = now + 0.1
        day = datetime.date.today().isoformat()
        if day != self.day:
            if self.f:
                self.f.close()
            os.makedirs(LOGDIR, exist_ok=True)
            prune_day_logs(LOGDIR, 14)
            header = "t," + ",".join(k for k, _, _ in COLS) + ",feel\n"
            path, n = os.path.join(LOGDIR, f"yoke_{day}.csv"), 1
            while os.path.exists(path) and open(path, encoding="utf-8").readline() != header:   # columns changed
                n += 1
                path = os.path.join(LOGDIR, f"yoke_{day}_{n}.csv")
            new = not os.path.exists(path)
            self.f = open(path, "a", encoding="utf-8")
            if new:
                self.f.write(header)
            self.day = day
        self.f.write(f"{now:.2f}," + ",".join(f"{v.get(k, 0):.4f}" for k, _, _ in COLS) + f",{feel}\n")
        self.f.flush()


_TICK = [time.time()]
CMD_FILE, REPLY_FILE = os.path.join(LOGDIR, "console_cmd.txt"), os.path.join(LOGDIR, "console_reply.txt")


def console_request(yoke):
    """One-shot console access while the helper owns the port: write a line (e.g. `!D`) to logs/console_cmd.txt, the
    reply lands in logs/console_reply.txt (appended, timestamped)."""
    if not os.path.exists(CMD_FILE):
        return
    try:
        cmds = [c.strip() for c in open(CMD_FILE, encoding="ascii", errors="replace").read().splitlines() if c.strip()]
        os.remove(CMD_FILE)
    except OSError:
        return
    with open(REPLY_FILE, "a", encoding="utf-8") as f:
        for c in cmds:
            f.write(f"=== {datetime.datetime.now().isoformat(timespec='seconds')} {c}\n{yoke.console(c)}\n")


def watchdog():
    """Log the main loop's stack when it stops ticking for 30 s (2026-10-24: the helper sat silent with COM9 free)."""
    main, reported = threading.main_thread().ident, False
    while True:
        time.sleep(5)
        stuck = time.time() - _TICK[0] > 30
        if stuck and not reported:
            frame = sys._current_frames().get(main)
            log("main loop stuck for 30 s at:\n" + "".join(traceback.format_stack(frame)) if frame else "main loop stuck")
        reported = stuck


def yoke_silence(yoke, sim, v, cfg):
    """The firmware streams TEMP_MAIN a few times a second; silence for SILENT_S with the port still on the bus means
    it has hung (2026-10-25 13:03:04: telemetry stopped, 5 s later the HID axes froze mid go-around, AP off, and the
    aircraft rolled into the ground; power cycle needed, as on 2026-10-24). Log the last commands sent (was it us?),
    pause the sim when airborne (cfg pause_on_freeze, default on) and put up an alert."""
    if yoke.s is None or time.time() - yoke.last_rx < SILENT_S:
        return
    if yoke.silent_reported:
        # 2026-10-25: the hung yoke answered again right after the helper closed and reopened the port (13:07:22; one
        # earlier reopen had not helped), with no power cycle - so keep trying that every REOPEN_S
        if time.time() - yoke.reopen_at >= REOPEN_S:
            yoke.reopens += 1
            yoke.reopen_at = time.time()
            log(f"yoke still silent: closing and reopening the port to wake it (try {yoke.reopens})")
            yoke.close()
        return
    yoke.silent_reported = True
    yoke.reopens, yoke.reopen_at = 0, time.time()
    t0 = yoke.last_rx
    sent = "\n".join(f"  {t - t0:+7.2f} s  {ln}" for t, ln in yoke.sent)
    log(f"YOKE FIRMWARE SILENT for {SILENT_S} s (port still present) - hung. Last commands sent, "
        f"relative to the last byte received:\n{sent or '  (none)'}")
    airborne = bool(v) and v.get("on_ground", 1) < 0.5
    paused = False
    if airborne and cfg.get("pause_on_freeze", True) and sim.sm is not None:
        try:
            from probe_electrical import send_event
            send_event(sim.sm, "PAUSE_SET", "1")
            paused = True
            log("sim paused (yoke frozen in the air)")
        except BaseException as e:   # send_event sys.exits on a mapping failure
            log(f"pause failed: {e!r}")
    msg = ("The RealSimGear yoke has stopped responding (its firmware went silent).\n\n"
           + ("The sim has been PAUSED.\n\n" if paused else "")
           + "The helper is trying to wake it (reopening its port every few seconds). If the controls do not come "
           "back within about 30 s, power-cycle the yoke (unplug its power / USB for a few seconds). Then unpause.")

    def alert():
        try:
            import ctypes
            ctypes.windll.user32.MessageBeep(0x30)
            ctypes.windll.user32.MessageBoxW(0, msg, "SR20 yoke helper", 0x30 | 0x1000 | 0x10000)   # warning, system modal, foreground
        except Exception:
            pass
    threading.Thread(target=alert, daemon=True).start()


SILENT_S = 6
REOPEN_S = 8


def run(port=None, fake=False):
    threading.Thread(target=watchdog, daemon=True).start()
    yoke, sim, csv, esp = Yoke(port, fake), Sim(), CsvLog(), EspPush()
    cfg, cfg_mtime = load_config(), 0.0
    next_try, port_try, latch_t = 0.0, 0.0, 0.0
    trim, trim_t = None, 0.0     # smoothed (aileron, elevator) trim for the spring centre
    try:
        while True:
            _TICK[0] = time.time()
            try:
                m = os.path.getmtime(CONFIG)
                if m != cfg_mtime:
                    cfg, cfg_mtime = load_config(), m
                    yoke.feel = None    # re-apply with the new numbers
            except OSError:
                pass
            if not fake and yoke.s is None:
                # its own retry time: pushing next_try out on every wait pass (before 2026-10-29) meant one failed open
                # (yoke not on the bus yet at Windows start-up) was never retried, and the sim's RealSimGear took the port
                if time.time() < port_try or not yoke.open():
                    if time.time() >= port_try:
                        port_try = time.time() + 5
                    time.sleep(0.5)
                    continue
            try:
                if fake:
                    v = fake_values()
                    if v is None:
                        break
                else:
                    if sim.sm is None and time.time() >= next_try:
                        if not sim.connect():
                            next_try = time.time() + 5
                    v = sim.read() if sim.sm else None
                    if v is None and sim.sm is not None:
                        log("sim stopped answering")
                        sim.close()
                # AP DISC bound to TOGGLE AUTOPILOT DISENGAGE latches AUTOPILOT DISENGAGED and the AP button is then
                # ignored until AP DISC is pressed again (2026-10-26, docs/probes.md): treat it as a plain disconnect
                if v and v.get("latch", 0) >= 0.5 and cfg["clear_disengage_latch"] and sim.sm is not None and time.time() - latch_t > 1:
                    latch_t = time.time()
                    log("AUTOPILOT DISENGAGED latch set: autopilot off, latch released")
                    try:
                        from probe_electrical import send_event
                        send_event(sim.sm, "AUTOPILOT_OFF", "0")
                        send_event(sim.sm, "AUTOPILOT_DISENGAGE_SET", "0")
                    except BaseException as e:   # send_event sys.exits on a mapping failure
                        log(f"latch release failed: {e!r}")
                push_r, push_p = esp.offsets(v, cfg)
                want = "stiff" if v and (v.get("ap", 0) >= 0.5 or esp.active) else "loose"
                if yoke.feel != want and not yoke.silent_reported:   # no retries into a hung yoke
                    if not yoke.apply_feel(want, cfg[want]) and want == "stiff":
                        yoke.apply_feel("loose", cfg["loose"])   # unverified stiff is not kept
                if v:
                    # trim smoothed (the autopilot dithers it ~+-0.007 every second; each centre step sent for that
                    # was felt as a tremble, 2026-10-26); the ESP push is added unsmoothed, it has its own slew
                    g, now = cfg["trim_gain"], time.time()
                    raw = (g * v.get("ail_trim", 0.0), g * v.get("elev_trim", 0.0))
                    k = 1.0 if trim is None or cfg["trim_smooth_s"] <= 0 else min(1.0, (now - trim_t) / cfg["trim_smooth_s"])
                    trim = raw if trim is None else tuple(t + (r - t) * k for t, r in zip(trim, raw))
                    trim_t = now
                    yoke.set_centre(trim[0] + push_r, trim[1] + push_p, deadband=0.002 if fake else cfg["trim_deadband"])
                    if not fake:
                        csv.row(v, yoke.feel)
                # stall warning shaker (the author 2026-10-24; a sim cue - the real SR20 has a horn, no shaker)
                yoke.set_shaker(cfg["shaker"] if v and v.get("stall", 0) >= 0.5 and v.get("on_ground", 1) < 0.5 else 0.0)
                yoke.drain()
                if not fake:
                    yoke_silence(yoke, sim, v, cfg)
                yoke.check_present()
                console_request(yoke)
                time.sleep(0.02 if v else 0.5)
            except Exception as e:   # serial unplugged etc.
                log(f"error: {e.__class__.__name__}: {e}")
                yoke.close()
                if fake:
                    raise
                time.sleep(1)
    finally:
        if fake or yoke.s:
            try:
                yoke.set_shaker(0.0)
                yoke.apply_feel("loose", cfg["loose"])
            except Exception:
                pass
        yoke.close()
        sim.close()
        log("helper stopped (yoke left loose, centre held)")


_FAKE = None


def fake_values():
    """Scripted sequence for --fake-sim: ground, AP on, autotrim, AP off."""
    global _FAKE
    seq = [(0, 0.0, 0.0), (0, 0.0, 0.10), (1, 0.0, 0.10), (1, 0.0, 0.12), (1, 0.02, 0.12), (0, 0.02, 0.12), (0, 0.02, 0.12)]
    _FAKE = 0 if _FAKE is None else _FAKE + 1
    if _FAKE >= len(seq):
        return None
    ap, ail, elev = seq[_FAKE]
    print(f"[fake] ap={ap} ail_trim={ail} elev_trim={elev}")
    return {"ap": ap, "ail_trim": ail, "elev_trim": elev}


def supervise():
    quick = 0
    while True:
        t0 = time.time()
        rc = subprocess.call([sys.executable, os.path.abspath(__file__)], cwd=HERE)
        log(f"supervisor: helper exited with {rc & 0xFFFFFFFF:#x}")
        quick = quick + 1 if time.time() - t0 < 10 else 0
        if quick >= 5:
            log("supervisor: helper keeps exiting at once, giving up")
            return
        time.sleep(3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--fake-sim", action="store_true")
    ap.add_argument("--supervise", action="store_true")
    a = ap.parse_args()
    if a.supervise:
        supervise()
    else:
        try:
            run(a.port, a.fake_sim)
        except KeyboardInterrupt:
            pass
