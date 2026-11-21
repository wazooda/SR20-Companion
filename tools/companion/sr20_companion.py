"""SR20 Companion: the aircraft's helpers outside the sim, in one program (2026-10-31, for the community release).

    pythonw tools\\companion\\sr20_companion.py              run all helpers, supervised (Windows log-on shortcut)
    pythonw tools\\companion\\sr20_companion.py --with-sim   the same, and exit when the sim exits (EXE.xml launch)
    python  tools\\companion\\sr20_companion.py --restart yoke   ask the running Companion to restart one helper
    python  tools\\companion\\sr20_companion.py --status     which helpers run (from the Companion's log)
    SR20Companion.exe ...                                    the frozen build (build.py), same arguments

Helpers, each its own child process of this program (a crash in one cannot touch another), restarted when it exits
and given up after 5 exits within 10 s of starting, as their own --supervise loops did:
    glide        tools/glide/glide_ring.py         glide ring (terrain tiles from the internet, cached)
    surfacewatch tools/surfacewatch/airport_server.py   airport maps for SafeTaxi / SurfaceWatch on 127.0.0.1:47820
    taws         tools/taws/flta_server.py         TAWS-B forward-looking terrain alerts (FLTA) for the PFD, same tiles
    efb          tools/efb/efb_server.py           the iPad: ForeFlight moving map (UDP to the iPads it hears) and flight
                                                   plans shared from ForeFlight into Downloads, offered to the MFD
    yoke         tools/yoke/yoke_helper.py         RealSimGear Cirrus yoke feel; started only while that yoke (USB
                                                   32B6:0202) is on the bus, polled, so a yoke that enumerates late
                                                   still gets its helper
A child is stopped through a named event (Local\\SR20Companion_<pid>): it turns that into KeyboardInterrupt in its main
thread, so the helper's own clean-up runs (the yoke helper leaves the yoke loose and closes the port). Only if it is
still alive after 5 s is it terminated. If this program dies, the children see it (they wait on its process handle)
and stop the same way. Not stdin: a thread blocked reading a pipe stdin stalls numpy's import in the glide ring
(Windows serialises the CRT's handle checks behind the pending read; seen 2026-10-31).

The yoke helper must open the yoke's COM port before the sim starts the RealSimGear Device Interface, which only
the log-on launch wins; --with-sim (EXE.xml) suits the glide ring and SurfaceWatch, and a yoke user should use
log-on.

From the repo, the helpers keep their files where they always did (tools/<helper>/logs, yoke_feel.json). Frozen,
everything writable is under %LOCALAPPDATA%\\SR20Companion (logs\\, data\\) and the SR20 package is the installed one in
the Community2024 folder; the environment variables SR20_LOGDIR, SR20_DATA, SR20_PACKAGE and SR20_SIMCONNECT_DLL carry
that to the helpers.
"""
import argparse
import ctypes
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", HERE)
TOOLS = os.path.dirname(HERE)
if not FROZEN:
    for d in ("glide", "surfacewatch", "yoke", "taws", "efb", ""):
        sys.path.insert(0, os.path.join(TOOLS, d) if d else TOOLS)

HELPERS = ("glide", "surfacewatch", "yoke", "taws", "efb")
YOKE_VID, YOKE_PID = 0x32B6, 0x0202
PACKAGE = "paulw-aircraft-sr20"
SIM_EXES = ("FlightSimulator2024.exe", "FlightSimulator.exe")
QUICK_S, QUICK_MAX, STOP_S = 10, 5, 5
NO_WINDOW = 0x08000000
USER_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "SR20Companion")
LOGDIR = os.path.join(USER_DIR, "logs") if FROZEN else os.path.join(HERE, "logs")


SETTINGS = os.path.join(USER_DIR, "settings.json")


def load_settings(path=SETTINGS):
    """User choices written by the installer (release plan B5): {"yoke_feel": bool, "community_dir": the Community2024
    folder chosen in setup}. Missing / unreadable -> defaults (yoke feel on, folder found from UserCfg.opt)."""
    out = {"yoke_feel": True}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("yoke_feel"), bool):
            out["yoke_feel"] = d["yoke_feel"]
        if isinstance(d, dict) and isinstance(d.get("community_dir"), str) and d["community_dir"].strip():
            out["community_dir"] = d["community_dir"].strip()
    except (OSError, ValueError):
        pass
    return out


def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        with open(os.path.join(LOGDIR, "companion.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout:
        try:
            print(line, flush=True)
        except Exception:
            pass


def community_dirs():
    """MSFS 2024 Community2024 folders: the one chosen in setup, UserCfg.opt's InstalledPackagesPath, then the Steam
    and Store defaults."""
    chosen = load_settings().get("community_dir")
    if chosen:
        yield chosen
    appdata, local = os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")
    for cfg in (os.path.join(appdata, "Microsoft Flight Simulator 2024", "UserCfg.opt"),
                os.path.join(local, "Packages", "Microsoft.Limitless_8wekyb3d8bbwe", "LocalCache", "UserCfg.opt")):
        try:
            m = re.search(r'^\s*InstalledPackagesPath\s+"([^"]+)"', open(cfg, encoding="utf-8", errors="replace").read(), re.M)
        except OSError:
            continue
        if m:
            yield os.path.join(m.group(1), "Community2024")
    yield os.path.join(appdata, "Microsoft Flight Simulator 2024", "Packages", "Community2024")
    yield from glob.glob(os.path.join(local, "Packages", "Microsoft.Limitless_*", "LocalCache", "Packages", "Community2024"))


def installed_package():
    for d in community_dirs():
        p = os.path.join(d, PACKAGE)
        if os.path.isdir(p):
            return p
    return None


def setup_env():
    """Frozen: tell the helpers where their writable files, the package and the bundled SimConnect.dll are."""
    if not FROZEN:
        return
    data = os.path.join(USER_DIR, "data")
    os.makedirs(data, exist_ok=True)
    os.environ.setdefault("SR20_LOGDIR", LOGDIR)
    os.environ.setdefault("SR20_DATA", data)
    os.environ.setdefault("SR20_SIMCONNECT_DLL", os.path.join(BUNDLE, "SimConnect.dll"))
    pkg = installed_package()
    if pkg:
        os.environ.setdefault("SR20_PACKAGE", pkg)
    feel = os.path.join(data, "yoke_feel.json")
    if not os.path.exists(feel) and os.path.exists(os.path.join(BUNDLE, "yoke_feel.json")):
        shutil.copyfile(os.path.join(BUNDLE, "yoke_feel.json"), feel)


# ---------------------------------------------------------------- child side

def _k32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.restype = k32.OpenEventW.restype = k32.OpenProcess.restype = ctypes.c_void_p
    k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    k32.OpenEventW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.SetEvent.argtypes = k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.WaitForMultipleObjects.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_ulong]
    k32.WaitForMultipleObjects.restype = ctypes.c_ulong
    return k32


def stop_event_name(pid):
    return f"Local\\SR20Companion_{pid}"


def _stop_watch(parent_pid):
    """Wait for the parent's stop event or the parent's death, then interrupt the main thread so the helper cleans up."""
    import _thread
    try:
        k32 = _k32()
        ev = k32.CreateEventW(None, True, False, stop_event_name(os.getpid()))
        par = k32.OpenProcess(0x00100000, False, parent_pid)   # SYNCHRONIZE
        hs = [h for h in (ev, par) if h]
        if not hs:
            return
        k32.WaitForMultipleObjects(len(hs), (ctypes.c_void_p * len(hs))(*hs), False, 0xFFFFFFFF)
    except Exception:
        return
    _thread.interrupt_main()
    time.sleep(STOP_S + 5)
    os._exit(3)                                     # clean-up hung: the parent would terminate us anyway


def run_child(name, parent_pid):
    threading.Thread(target=_stop_watch, args=(parent_pid,), daemon=True).start()
    try:
        if name == "glide":
            import glide_ring
            glide_ring.run(False)
        elif name == "surfacewatch":
            import airport_server
            airport_server.run()
        elif name == "yoke":
            import yoke_helper
            yoke_helper.run()
        elif name == "taws":
            import flta_server
            flta_server.run(False)
        elif name == "efb":
            import efb_server
            efb_server.run()
        else:
            sys.exit(f"unknown helper {name}")
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- supervisor side

def yoke_present():
    try:
        from serial.tools import list_ports
        return any(p.vid == YOKE_VID and p.pid == YOKE_PID for p in list_ports.comports())
    except Exception:
        return False


def sim_running():
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout.lower()
    return any(f'"{exe.lower()}"' in out for exe in SIM_EXES)


class Child:
    def __init__(self, name):
        self.name, self.p, self.t0, self.quick, self.dead, self.why = name, None, 0.0, 0, False, None
        self.restart_at = None                       # a pending restart after an exit (no sleeping in the loop)

    def start(self):
        cmd = [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)]) + ["--run", self.name,
                                                                                  "--parent", str(os.getpid())]
        self.p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  cwd=HERE, creationflags=NO_WINDOW)
        self.t0 = time.time()
        self.restart_at = None
        log(f"{self.name}: started (pid {self.p.pid})")

    def stop(self):
        self.restart_at = None
        if not self.p or self.p.poll() is not None:
            return
        k32, t_end = _k32(), time.time() + STOP_S
        while time.time() < t_end:                  # the event appears once the child is up; retry until then
            ev = k32.OpenEventW(0x0002, False, stop_event_name(self.p.pid))   # EVENT_MODIFY_STATE
            if ev:
                k32.SetEvent(ev)
                k32.CloseHandle(ev)
                break
            if self.p.poll() is not None:
                break
            time.sleep(0.2)
        try:
            self.p.wait(max(0.5, t_end - time.time()))
            log(f"{self.name}: stopped")
        except subprocess.TimeoutExpired:
            self.p.kill()
            try:
                self.p.wait(3)                       # termination is asynchronous: be sure it's gone (ports, files)
                log(f"{self.name}: did not stop in {STOP_S} s, terminated")
            except subprocess.TimeoutExpired:
                log(f"{self.name}: could not be terminated (pid {self.p.pid})")

    def check(self):
        """Restart after an exit, 3 s later (as the helpers' own --supervise loops did), without blocking the loop."""
        if self.restart_at is not None and self.p is None and not self.dead:
            if time.time() >= self.restart_at:
                self.start()
            return
        if self.dead or self.p is None or self.p.poll() is None:
            return
        rc = self.p.returncode
        log(f"{self.name}: exited with {rc & 0xFFFFFFFF:#x}")
        self.quick = self.quick + 1 if time.time() - self.t0 < QUICK_S else 0
        self.p = None
        if self.quick >= QUICK_MAX:
            self.dead = True
            log(f"{self.name}: keeps exiting at once, giving up (see its own log)")
            return
        self.restart_at = time.time() + 3


def single_instance():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.CreateMutexW(None, False, "Local\\SR20Companion")
    if ctypes.get_last_error() == 183:              # ERROR_ALREADY_EXISTS
        k32.CloseHandle(h)                          # don't keep the other instance's mutex alive
        return None
    return h


QUIT_EVENT, SHOW_EVENT, SHOWN_EVENT = ("Local\\SR20Companion_quit", "Local\\SR20Companion_show",
                                      "Local\\SR20Companion_shown")


def _event(name, manual):
    k32 = _k32()
    return k32, k32.CreateEventW(None, manual, False, name)


def _signalled(k32, h):
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k32.WaitForSingleObject.restype = ctypes.c_ulong
    return bool(h) and k32.WaitForSingleObject(h, 0) == 0


def stop_all(kids):
    """All helpers at once (each may take up to STOP_S to clean up), not one after another."""
    ts = [threading.Thread(target=k.stop) for k in kids.values()]
    for t in ts:
        t.start()
    for t in ts:
        t.join(STOP_S + 3)


def helper_states(kids, yoke_present_now, yoke_enabled=True):
    out = {}
    for n, k in kids.items():
        if n == "yoke" and not yoke_enabled:
            out[n] = "off (turned off in setup)"
        elif n == "yoke" and k.p is None and not k.dead:
            out[n] = "on" if yoke_present_now else "no yoke connected"
        elif k.dead:
            out[n] = "stopped: " + (k.why or "keeps failing (see the log)")
        elif k.p is None:
            out[n] = "starting"
        elif k.quick and time.time() - k.t0 < QUICK_S:
            out[n] = "restarting"
        else:
            out[n] = "on"
    return out


def open_ipad_page():
    open_ipad_setup()      # falls back to the "start the Companion" page when the iPad helper isn't answering


def supervise(with_sim, background=False):
    mutex = single_instance()
    if mutex is None:
        if background or with_sim:
            log("another SR20 Companion is already running, exiting")
        else:   # started by hand from the Start menu: show the running one's page instead of a second Companion
            if hand_off():
                log("SR20 Companion already running: it showed the iPad setup page")
                return
            # no answer: it is shutting down. Wait for it to go, then be the Companion.
            t_end = time.time() + 20
            while mutex is None and time.time() < t_end:
                time.sleep(0.5)
                mutex = single_instance()
            if mutex is None:
                log("another SR20 Companion is running and not answering, exiting")
                return
            log("the previous SR20 Companion had just quit: starting")
    if mutex is None:
        return
    k32, quit_ev = _event(QUIT_EVENT, True)              # right after the mutex: --quit looks for these
    _, show_ev = _event(SHOW_EVENT, False)
    _, shown_ev = _event(SHOWN_EVENT, False)
    import queue
    commands = queue.Queue()
    log(f"SR20 Companion {'frozen' if FROZEN else 'from source'}, {'with the sim' if with_sim else 'log-on'} mode, "
        f"package {os.environ.get('SR20_PACKAGE') or installed_package() or 'not found'}")
    kids = {n: Child(n) for n in HELPERS}
    yoke_enabled = load_settings()["yoke_feel"]
    if not yoke_enabled:
        log("yoke: force feel turned off in setup (settings.json), not started")
    if FROZEN and not os.environ.get("SR20_PACKAGE"):
        kids["surfacewatch"].dead = True
        kids["surfacewatch"].why = "the SR20 aircraft isn't in the Community2024 folder"
        log("surfacewatch: SR20 package not found in the Community2024 folder, not started")
    for n in ("glide", "surfacewatch", "taws", "efb"):
        if not kids[n].dead:
            kids[n].start()
    yoke_seen, next_yoke, next_sim, t_start, sim_seen = False, 0.0, 0.0, time.time(), False
    state = {"yoke": False, "sim": False}
    tray = None
    try:
        import tray as tray_mod
        tray = tray_mod.Tray(commands, lambda: helper_states(kids, state["yoke"], yoke_enabled), lambda: state["sim"], FROZEN,
                             sys.executable, os.path.abspath(__file__), LOGDIR, open_ipad_page)
        if not tray.start():
            log("tray icon not available (pystray / Pillow missing); running without it")
            tray = None
    except Exception as e:  # noqa: BLE001
        log(f"tray icon failed: {e}")
        tray = None
    dead_seen = {n for n, k in kids.items() if k.dead}
    last_states = None
    try:
        while True:
            if _signalled(k32, quit_ev):               # shutdown before anything else (log-off has seconds)
                log("quit requested")
                return
            try:
                first = commands.get_nowait()
            except queue.Empty:
                first = None
            if first == "quit":
                log("quit from the tray")
                return
            if first is not None:
                commands.put(first)
            now = time.time()
            if yoke_enabled and kids["yoke"].p is None and not kids["yoke"].dead and now >= next_yoke:
                next_yoke = now + 5
                state["yoke"] = yoke_present()
                if state["yoke"]:
                    kids["yoke"].start()
                elif not yoke_seen:
                    yoke_seen = True
                    log("yoke: RealSimGear Cirrus yoke not on the bus, watching for it")
            for n in HELPERS:
                if n != "yoke" or kids["yoke"].p is not None:
                    kids[n].check()
            for n in HELPERS:
                req = os.path.join(LOGDIR, f"restart_{n}")
                if os.path.exists(req):
                    os.remove(req)
                    log(f"{n}: restart requested")
                    kids[n].stop()
                    kids[n].p, kids[n].quick, kids[n].dead = None, 0, False
                    if n != "yoke":
                        kids[n].start()
                    next_yoke = 0.0
            if now >= next_sim:
                next_sim = now + (5 if with_sim else 15)
                try:
                    state["sim"] = sim_running()
                except (OSError, subprocess.SubprocessError):
                    pass                                # keep the last value
                if with_sim:
                    if state["sim"]:
                        sim_seen = True
                    elif sim_seen or now - t_start > 120:
                        log("sim not running, stopping")
                        return
            for n, k in kids.items():                  # tell the pilot once when a feature gives up
                if k.dead and n not in dead_seen:
                    dead_seen.add(n)
                    if tray:
                        import tray as tray_mod
                        tray.notify(f"{tray_mod.LABEL[n]} stopped working. Right-click the SR20 icon > Restart all.")
            if tray:
                st = (helper_states(kids, state["yoke"], yoke_enabled), state["sim"])
                if st != last_states:
                    last_states = st
                    tray.refresh()
            if _signalled(k32, quit_ev):
                log("quit requested")
                return
            if _signalled(k32, show_ev):
                k32.SetEvent(shown_ev)                 # the launcher only exits once we've answered
                open_ipad_page()
            try:
                cmd = commands.get(timeout=1)
            except queue.Empty:
                continue
            if cmd == "quit":
                log("quit from the tray")
                return
            if cmd == "restart":
                log("restart all from the tray")
                stop_all(kids)
                for n, k in kids.items():
                    if n == "surfacewatch" and k.why:
                        continue                       # package missing: restarting won't help
                    k.p, k.quick, k.dead = None, 0, False
                    if n != "yoke":
                        k.start()
                dead_seen = {n for n, k in kids.items() if k.dead}
                next_yoke = 0.0
    except KeyboardInterrupt:
        pass
    finally:
        stop_all(kids)
        if tray:
            tray.stop()           # also releases a pending log-off (WM_ENDSESSION waits for this)
        log("SR20 Companion stopped")


def hand_off(timeout=3):
    """Ask the running Companion to show its page. -> True once it has answered."""
    k32 = _k32()
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k32.WaitForSingleObject.restype = ctypes.c_ulong
    ack = k32.CreateEventW(None, False, False, SHOWN_EVENT)
    try:
        for _ in range(20):                           # the other one may still be creating its event
            ev = k32.OpenEventW(0x0002, False, SHOW_EVENT)
            if ev:
                k32.SetEvent(ev)
                k32.CloseHandle(ev)
                return bool(ack) and k32.WaitForSingleObject(ack, int(timeout * 1000)) == 0
            time.sleep(0.1)
        return False
    finally:
        if ack:
            k32.CloseHandle(ack)


def request_quit(timeout=30):
    """--quit (the installer, an upgrade): ask the running Companion to stop its helpers and exit; wait for it."""
    k32 = _k32()
    k32.OpenMutexW.restype = ctypes.c_void_p
    k32.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]

    def running():
        m = k32.OpenMutexW(0x00100000, False, "Local\\SR20Companion")
        if m:
            k32.CloseHandle(m)
        return bool(m)

    ev = None
    for _ in range(20):                       # a Companion that is just starting may not have its event yet
        ev = k32.OpenEventW(0x0002, False, QUIT_EVENT)
        if ev or not running():
            break
        time.sleep(0.1)
    if not ev:
        if running():                         # an older build without the quit event: the installer must stop it
            print("SR20 Companion is running but can't be asked to quit (older version)")
            return 2
        print("SR20 Companion isn't running")
        return 0
    k32.SetEvent(ev)
    k32.CloseHandle(ev)
    t_end = time.time() + timeout
    while time.time() < t_end:
        m = k32.OpenMutexW(0x00100000, False, "Local\\SR20Companion")
        if not m:
            print("SR20 Companion stopped")
            return 0
        k32.CloseHandle(m)
        time.sleep(0.3)
    print("SR20 Companion did not stop in time")
    return 1


def status():
    try:
        lines = open(os.path.join(LOGDIR, "companion.log"), encoding="utf-8").read().splitlines()[-40:]
    except OSError:
        print("no Companion log yet")
        return
    print("\n".join(lines))


def open_ipad_setup():
    """The setup page is served by the running EFB helper on loopback; without it, a page that says to start the
    Companion. (Not the mutex: single_instance() creates it.)"""
    import pathlib
    import urllib.request
    import webbrowser
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a system proxy
    try:
        with opener.open("http://127.0.0.1:47822/v1/alive", timeout=2) as r:
            alive = r.status == 200
    except OSError:
        alive = False
    if alive:
        webbrowser.open("http://127.0.0.1:47822/setup")
    else:
        page = os.path.join(BUNDLE if FROZEN else os.path.join(TOOLS, "efb"), "web", "not_running.html")
        webbrowser.open(pathlib.Path(page).as_uri())


def main(argv=None):
    ap = argparse.ArgumentParser(description="SR20 Companion: glide ring, SurfaceWatch airport maps, yoke feel")
    ap.add_argument("--with-sim", action="store_true", help="exit when the sim exits (EXE.xml launch)")
    ap.add_argument("--run", choices=HELPERS, help=argparse.SUPPRESS)
    ap.add_argument("--parent", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--restart", choices=HELPERS, help="ask the running Companion to restart one helper")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--ipad", action="store_true", help="open the iPad setup page (pairing QR, firewall check)")
    ap.add_argument("--background", action="store_true", help="started by Windows at log-on: never open a page")
    ap.add_argument("--quit", action="store_true", help="stop the running Companion and its helpers (installer)")
    ap.add_argument("--fix-firewall", action="store_true", help=argparse.SUPPRESS)   # run elevated by the setup page
    a = ap.parse_args(argv)
    if a.fix_firewall:      # elevated, possibly as another account: before setup_env() touches any profile folder
        import efb_setup
        sys.exit(efb_setup.fix_firewall(efb_setup.program(), frozen=FROZEN))
    if a.quit:
        sys.exit(request_quit())
    setup_env()
    if a.ipad:
        open_ipad_setup()
    elif a.run:
        run_child(a.run, a.parent)
    elif a.restart:
        os.makedirs(LOGDIR, exist_ok=True)
        open(os.path.join(LOGDIR, f"restart_{a.restart}"), "w").close()
        print(f"restart of {a.restart} requested")
    elif a.status:
        status()
    else:
        supervise(a.with_sim, a.background)


if __name__ == "__main__":
    main()
