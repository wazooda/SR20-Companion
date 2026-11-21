"""Electrical-system probes for docs/SYSTEMS_PLAN.md stage 1 (sim running, aircraft loaded, on the apron).

Usage (run WITHOUT -I: the SimConnect binding is a --user package):
  python tools/probe_electrical.py inventory <label>   A2: buses, batteries, alternators, circuits, bus-to-bus
                                                        connection matrix -> docs/baseline/<label>_electrical.md
  python tools/probe_electrical.py watch               A0/A5: poll the electrical state 4x/s and print every
                                                        change with a timestamp; flip one switch at a time
  python tools/probe_electrical.py events              A0: print electrical key events as the cockpit fires them
  python tools/probe_electrical.py send <EVENT> [n[,m]] A4: transmit a key event; two comma-separated values go
                                                        through TransmitClientEvent_EX1 (needs the SDK DLL), e.g.
                                                        send ELECTRICAL_CIRCUIT_BREAKER_TOGGLE 1,10  (bus, circuit)
                                                        send ELECTRICAL_BUS_TO_BUS_CONNECTION_TOGGLE 2,5
  python tools/probe_electrical.py snap [EVENT=n[,m] ...] quick state line (masters, bus volts, battery amps)
                                                        before/after each event in the list

All reads are tolerant: a SimVar the sim does not know prints as "-" instead of aborting, so the inventory
doubles as a test of which electrical SimVars exist in this build.
"""
import ctypes
import datetime
import os
import sys
import time
from ctypes.wintypes import DWORD, HANDLE

from SimConnect import SimConnect
from SimConnect.Constants import SIMCONNECT_GROUP_PRIORITY_HIGHEST_MASKABLE
from SimConnect.RequestList import Request

SDK_DLL = os.environ.get("SR20_SIMCONNECT_DLL") or r"C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll"  # the binding's own DLL lacks *_EX1

N_BUS = 12
N_CIRCUIT = 64
N_SRC = 3

KEY_EVENTS = [
    "TOGGLE_MASTER_BATTERY", "TOGGLE_MASTER_BATTERY_1", "TOGGLE_MASTER_BATTERY_2", "MASTER_BATTERY_SET",
    "ELECTRICAL_BATTERY_BUS_CONNECTION_TOGGLE", "ELECTRICAL_BATTERY_BUS_CONNECTION_SET",
    "TOGGLE_MASTER_ALTERNATOR", "TOGGLE_ALTERNATOR1", "TOGGLE_ALTERNATOR2", "ALTERNATOR_SET", "ALTERNATOR_ON", "ALTERNATOR_OFF",
    "ELECTRICAL_ALTERNATOR_BREAKER_TOGGLE", "ELECTRICAL_ALTERNATOR_BUS_CONNECTION_TOGGLE",
    "TOGGLE_AVIONICS_MASTER", "AVIONICS_MASTER_SET", "AVIONICS_MASTER_1_SET", "AVIONICS_MASTER_2_SET",
    "ELECTRICAL_CIRCUIT_TOGGLE", "ELECTRICAL_CIRCUIT_SET", "ELECTRICAL_CIRCUIT_BREAKER_TOGGLE",
    "ELECTRICAL_CIRCUIT_POWER_SETTING_SET", "ELECTRICAL_BUS_TO_BUS_CONNECTION_TOGGLE", "ELECTRICAL_BUS_BREAKER_TOGGLE",
    "ELECTRICAL_EXTERNAL_POWER_TOGGLE", "TOGGLE_EXTERNAL_POWER", "TOGGLE_ELECTRICAL_FAILURE",
    "PITOT_HEAT_TOGGLE", "PITOT_HEAT_SET", "PITOT_HEAT_ON", "PITOT_HEAT_OFF",
    "TOGGLE_NAV_LIGHTS", "TOGGLE_BEACON_LIGHTS", "STROBES_TOGGLE", "LANDING_LIGHTS_TOGGLE", "TOGGLE_TAXI_LIGHTS",
    "PANEL_LIGHTS_TOGGLE", "TOGGLE_CABIN_LIGHTS", "TOGGLE_LOGO_LIGHTS", "TOGGLE_RECOGNITION_LIGHTS", "TOGGLE_WING_LIGHTS",
    "FUEL_PUMP", "TOGGLE_ELECT_FUEL_PUMP", "TOGGLE_ELECT_FUEL_PUMP1", "ELECT_FUEL_PUMP_SET",
    "FLAPS_UP", "FLAPS_DOWN", "FLAPS_INCR", "FLAPS_DECR", "FLAPS_SET", "AXIS_FLAPS_SET",
    "TOGGLE_STARTER1", "SET_STARTER1_HELD", "MAGNETO1_START", "MAGNETO1_SET",
]


class Reader:
    """One Request per SimVar; unknown vars read as None instead of raising."""

    def __init__(self, sm: SimConnect):
        self.sm = sm
        self.reqs = {}

    def get(self, name: str, unit: str = "Number", settable: bool = False):
        key = (name, unit)
        req = self.reqs.get(key)
        if req is None:
            try:
                req = Request((name.encode(), unit.encode()), self.sm, _time=0, _settable=settable, _attemps=20)
            except Exception:
                return None
            self.reqs[key] = req
        try:
            v = req.value
        except Exception:
            return None
        if isinstance(v, bytes):
            v = v.decode(errors="replace")
        return v

    def set(self, name: str, unit: str, value):
        key = (name, unit)
        req = self.reqs.get(key)
        if req is None:
            req = Request((name.encode(), unit.encode()), self.sm, _time=0, _settable=True, _attemps=20)
            self.reqs[key] = req
        req.value = value


def fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def state_vector(rd: Reader) -> dict:
    s = {}
    for i in range(1, N_SRC + 1):
        s[f"MASTER BATTERY:{i}"] = rd.get(f"ELECTRICAL MASTER BATTERY:{i}", "Bool")
        s[f"BATTERY CONNECTION ON:{i}"] = rd.get(f"BATTERY CONNECTION ON:{i}", "Bool")
        s[f"BATTERY BREAKER PULLED:{i}"] = rd.get(f"BATTERY BREAKER PULLED:{i}", "Bool")
        s[f"MASTER ALTERNATOR:{i}"] = rd.get(f"GENERAL ENG MASTER ALTERNATOR:{i}", "Bool")
        s[f"ALTERNATOR CONNECTION ON:{i}"] = rd.get(f"ALTERNATOR CONNECTION ON:{i}", "Bool")
        s[f"ALTERNATOR BREAKER PULLED:{i}"] = rd.get(f"ALTERNATOR BREAKER PULLED:{i}", "Bool")
        s[f"EXTERNAL POWER ON:{i}"] = rd.get(f"EXTERNAL POWER ON:{i}", "Bool")
    s["AVIONICS MASTER SWITCH:1"] = rd.get("AVIONICS MASTER SWITCH:1", "Bool")
    s["PITOT HEAT SWITCH:1"] = rd.get("PITOT HEAT SWITCH:1", "Bool")
    for i in range(1, N_BUS + 1):
        s[f"BUS VOLTAGE:{i}"] = rd.get(f"ELECTRICAL MAIN BUS VOLTAGE:{i}", "Volts")
    for i in range(1, N_CIRCUIT + 1):
        s[f"CIRCUIT SWITCH ON:{i}"] = rd.get(f"CIRCUIT SWITCH ON:{i}", "Bool")
        s[f"CIRCUIT ON:{i}"] = rd.get(f"CIRCUIT ON:{i}", "Bool")
        s[f"CIRCUIT BREAKER PULLED:{i}"] = rd.get(f"CIRCUIT BREAKER PULLED:{i}", "Bool")
    for k in ELEC_LVARS:
        s[f"L:SR20_ELEC_{k}"] = rd.get(f"L:SR20_ELEC_{k}", "Number")
    return s


# Published by the PFD plugin's Sr20ElectricalController (docs/ELECTRICAL_DESIGN.md §3.6); logging only
ELEC_LVARS = ["ROW", "L12", "L13", "L23", "L24", "L312", "TGT_L12", "TGT_L13", "TGT_L23", "TGT_L24", "TGT_L312", "ALT1_ALIVE", "ALT2_ALIVE", "DIST1_LIVE", "ALT1_BRK", "ALT2_BRK", "LINK_SKIPS", "HEALS", "TOGGLES",
              "FAULT", "DISABLE"]   # FAULT = runaway latch (clear with `set L:SR20_ELEC_FAULT 0`); DISABLE = 1 silences the controller


def cmd_inventory(label: str) -> None:
    sm = SimConnect()
    rd = Reader(sm)
    time.sleep(0.5)
    rows = [f"# Electrical inventory: {label}", "", f"Taken {datetime.datetime.now():%Y-%m-%d %H:%M}",
            f"Aircraft: {fmt(rd.get('TITLE', 'String'))}", ""]
    rows += ["## Sources", "", "| # | battery V | battery load A | battery cap % | master batt | batt conn | genalt V | genalt A | master alt | alt conn | ext power avail | ext power on |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i in range(1, N_SRC + 1):
        rows.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            i, fmt(rd.get(f"ELECTRICAL BATTERY VOLTAGE:{i}", "Volts")), fmt(rd.get(f"ELECTRICAL BATTERY LOAD:{i}", "Amperes")),
            fmt(rd.get(f"ELECTRICAL BATTERY ESTIMATED CAPACITY PCT:{i}", "Percent")), fmt(rd.get(f"ELECTRICAL MASTER BATTERY:{i}", "Bool")),
            fmt(rd.get(f"BATTERY CONNECTION ON:{i}", "Bool")), fmt(rd.get(f"ELECTRICAL GENALT BUS VOLTAGE:{i}", "Volts")),
            fmt(rd.get(f"ELECTRICAL GENALT BUS AMPS:{i}", "Amperes")), fmt(rd.get(f"GENERAL ENG MASTER ALTERNATOR:{i}", "Bool")),
            fmt(rd.get(f"ALTERNATOR CONNECTION ON:{i}", "Bool")), fmt(rd.get(f"EXTERNAL POWER AVAILABLE:{i}", "Bool")),
            fmt(rd.get(f"EXTERNAL POWER ON:{i}", "Bool"))))
    rows += ["", "## Buses", "", "| bus | main bus V | main bus A |", "|---|---|---|"]
    for i in range(1, N_BUS + 1):
        rows.append(f"| {i} | {fmt(rd.get(f'ELECTRICAL MAIN BUS VOLTAGE:{i}', 'Volts'))} | {fmt(rd.get(f'ELECTRICAL MAIN BUS AMPS:{i}', 'Amperes'))} |")
    # bus-to-bus connection matrix: BUS LOOKUP INDEX selects the bus that the indexed vars refer to
    rows += ["", "## Bus-to-bus connections (BUS LOOKUP INDEX = row; BUS CONNECTION ON:col / BUS BREAKER PULLED:col)", "",
             "| lookup \\ bus | " + " | ".join(str(j) for j in range(1, N_BUS + 1)) + " |", "|---|" + "---|" * N_BUS]
    for i in range(1, N_BUS + 1):
        try:
            rd.set("BUS LOOKUP INDEX", "Number", float(i))
            time.sleep(0.15)
            cells = []
            for j in range(1, N_BUS + 1):
                c = rd.get(f"BUS CONNECTION ON:{j}", "Bool")
                b = rd.get(f"BUS BREAKER PULLED:{j}", "Bool")
                cells.append(("on" if c else "off") + (" (brk)" if b else "") if c is not None else "-")
            rows.append(f"| {i} | " + " | ".join(cells) + " |")
        except Exception as exc:
            rows.append(f"| {i} | ERR {exc} |")
    rows += ["", "## Circuits (CIRCUIT SWITCH ON / CIRCUIT ON / CIRCUIT BREAKER PULLED / CIRCUIT POWER SETTING)", "",
             "| circuit | switch on | on | breaker pulled | power setting |", "|---|---|---|---|---|"]
    for i in range(1, N_CIRCUIT + 1):
        rows.append("| %d | %s | %s | %s | %s |" % (
            i, fmt(rd.get(f"CIRCUIT SWITCH ON:{i}", "Bool")),
            fmt(rd.get(f"CIRCUIT ON:{i}", "Bool")), fmt(rd.get(f"CIRCUIT BREAKER PULLED:{i}", "Bool")),
            fmt(rd.get(f"CIRCUIT POWER SETTING:{i}", "Percent"))))
    rows += ["", "## Other", "", "| SimVar | value |", "|---|---|"]
    for name, unit in (("ELECTRICAL TOTAL LOAD AMPS", "Amperes"), ("ELECTRICAL AVIONICS BUS VOLTAGE", "Volts"),
                       ("ELECTRICAL AVIONICS BUS AMPS", "Amperes"), ("CIRCUIT AVIONICS ON", "Bool"), ("CIRCUIT GENERAL PANEL ON", "Bool"),
                       ("CIRCUIT FLAP MOTOR ON", "Bool"), ("CIRCUIT STANDBY VACUUM ON", "Bool"), ("CIRCUIT AUTOPILOT ON", "Bool"),
                       ("STRUCTURAL DEICE SWITCH", "Bool"), ("SUCTION PRESSURE", "inHg"), ("STALL WARNING", "Bool"), ("PITOT HEAT", "Bool")):
        rows.append(f"| {name} | {fmt(rd.get(name, unit))} |")
    sm.exit()
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "baseline")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.abspath(os.path.join(out_dir, f"{label}_electrical.md"))
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(rows) + "\n")
    print(f"wrote {out}")


def cmd_watch() -> None:
    sm = SimConnect()
    rd = Reader(sm)
    time.sleep(0.5)
    prev = state_vector(rd)
    print(time.strftime("%H:%M:%S"), "watching (Ctrl+C to stop); flip one switch at a time")
    try:
        while True:
            time.sleep(0.25)
            cur = state_vector(rd)
            diffs = [f"{k}: {fmt(prev[k])} -> {fmt(v)}" for k, v in cur.items()
                     if v != prev[k] and not (k.startswith("BUS VOLTAGE") and prev[k] is not None and v is not None and abs(v - prev[k]) < 0.3)]
            if diffs:
                print(time.strftime("%H:%M:%S"), "; ".join(diffs), flush=True)
            prev = cur
    except KeyboardInterrupt:
        pass
    finally:
        sm.exit()


def cmd_events() -> None:
    names = {}

    class Sm(SimConnect):
        def handle_id_event(self, event):  # noqa: N802 (binding API)
            n = names.get(event.uEventID)
            if n is None:
                return super().handle_id_event(event)
            print(time.strftime("%H:%M:%S"), f"{n} data={event.dwData}", flush=True)

    sm = Sm()
    group = 1
    for ev in KEY_EVENTS:
        evnt = sm.map_to_sim_event(ev.encode())
        if evnt is None:
            print("not mappable:", ev)
            continue
        names[evnt.value] = ev
        sm.dll.AddClientEventToNotificationGroup(sm.hSimConnect, group, evnt.value, False)
    sm.dll.SetNotificationGroupPriority(sm.hSimConnect, group, SIMCONNECT_GROUP_PRIORITY_HIGHEST_MASKABLE)
    print(time.strftime("%H:%M:%S"), f"listening for {len(names)} key events (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        sm.exit()


def connect_sdk(timeout: float = 15.0) -> SimConnect:
    """SimConnect through the SDK DLL, which exports TransmitClientEvent_EX1 (multi-parameter key events).
    The library's connect() spins forever waiting for the sim's Open reply when the sim is starting up or shutting down
    (2026-10-24: the yoke helper hung there with the sim gone); here it gets `timeout` s, then ConnectionError."""
    import threading
    sm = SimConnect(auto_connect=False, library_path=SDK_DLL)
    err = []

    def _connect():
        try:
            sm.connect()
        except Exception as e:   # sim not running
            err.append(e)

    t = threading.Thread(target=_connect, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() or err:
        if t.is_alive():
            sm.ok = True                          # release the library's spin loop
            t.join(1.0)
            try:
                sm.exit()                         # stop its dispatch thread, close the handle
            except Exception:
                pass
        raise ConnectionError(f"SimConnect open failed ({err[0] if err else f'no reply in {timeout:.0f} s'})")
    ex1 = sm.dll.SimConnect.SimConnect_TransmitClientEvent_EX1
    ex1.restype = ctypes.HRESULT
    ex1.argtypes = [HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD]
    sm.ex1 = ex1
    return sm


def send_event(sm: SimConnect, event: str, value: str) -> None:
    evnt = sm.map_to_sim_event(event.encode())
    if evnt is None:
        sys.exit(f"cannot map {event}")
    vals = [int(x) for x in str(value).split(",")]
    if len(vals) == 1:
        sm.send_event(evnt, DWORD(vals[0]))
    else:
        vals += [0] * (5 - len(vals))
        hr = sm.ex1(sm.hSimConnect, 0, evnt.value, 0, 16, *vals)  # object USER, group 0, flag GROUPID_IS_PRIORITY
        if hr != 0:
            print(f"EX1 failed hr={hr:#x}")


def cmd_send(event: str, value: str) -> None:
    sm = connect_sdk()
    send_event(sm, event, value)
    time.sleep(0.3)
    sm.exit()
    print(f"sent {event} {value}")


def cmd_cb() -> None:
    """Stage 3 breaker panel: request (L:SR20_CB_<KEY>) / tracked (L:SR20_ELEC_CB_<bus>_<circuit>) / CIRCUIT ON per sim breaker."""
    table = [("STARTER", [(1, 3)]), ("NAV_LIGHTS", [(10, 8)]), ("STROBE_LIGHTS", [(10, 12), (10, 9)]), ("PITOT_HEAT", [(10, 7)]),
             ("FLAPS", [(10, 5)]), ("LANDING_LIGHTS", [(1, 10), (1, 11), (1, 44), (1, 45)]), ("USB_POWER", [(6, 46)]), ("MFD_A", [(6, 40)]),
             ("GIA2", [(9, 29), (9, 42)]), ("COM2", [(9, 30)]), ("FUEL_PUMP", [(9, 4)]), ("PFD_B", [(9, 39)]), ("GIA1", [(12, 27), (12, 41)]),
             ("COM1", [(12, 28)]), ("ADAHRS1", [(12, 33)]), ("STDBY_ATTD_A", [(12, 38)]), ("PFD_A", [(12, 39)]), ("ICE_LIGHTS", [(8, 15)]),
             ("STDBY_ATTD_B", [(8, 38)]), ("MFD_B", [(8, 40)]), ("CABIN_LIGHTS", [(8, 13), (8, 14), (8, 17), (8, 18), (8, 19), (8, 20), (8, 21), (8, 43)]),
             ("AP_SERVOS", [(8, 37)]), ("AUDIO_PANEL", [(4, 36), (4, 26)]), ("DATALINK", [(4, 34)]), ("XPONDER", [(4, 25)])]
    sm = connect_sdk()
    rd = Reader(sm)
    time.sleep(0.3)
    print("key              req | bus:circuit tracked on sw (per sim breaker)")
    for key, pairs in table:
        req = fmt(rd.get(f"L:SR20_CB_{key}", "Number"))
        cells = [f"{b}:{c} t={fmt(rd.get(f'L:SR20_ELEC_CB_{b}_{c}', 'Number'))} on={fmt(rd.get(f'CIRCUIT ON:{c}', 'Bool'))} sw={fmt(rd.get(f'CIRCUIT SWITCH ON:{c}', 'Bool'))}" for b, c in pairs]
        print(f"{key:16s} {req:>3} | " + "  ".join(cells))
    for name in ("AVIONICS", "ESS_POWER", "ALT1", "ALT2", "BAT2", "AC_COND", "AC_COMPR", "CABIN_FAN", "AV_FAN1", "AV_FAN2", "ENGINE_INSTR", "FUEL_QTY"):
        print(f"{name:16s} {fmt(rd.get(f'L:SR20_CB_{name}', 'Number')):>3}")
    print("tracked ALT1/ALT2/BAT2:", fmt(rd.get("L:SR20_ELEC_ALT1_BRK", "Number")), fmt(rd.get("L:SR20_ELEC_ALT2_BRK", "Number")), fmt(rd.get("L:SR20_ELEC_BAT2_BRK", "Number")),
          "queue:", fmt(rd.get("L:SR20_ELEC_CB_QUEUE", "Number")), "heals:", fmt(rd.get("L:SR20_ELEC_HEALS", "Number")), "fault:", fmt(rd.get("L:SR20_ELEC_FAULT", "Number")),
          "loads 47/48/49/50 sw:", [fmt(rd.get(f"CIRCUIT SWITCH ON:{c}", "Bool")) for c in (47, 48, 49, 50)])
    sm.exit()


def cmd_snap(steps) -> None:
    sm = connect_sdk()
    rd = Reader(sm)

    def snap(tag):
        bat = [fmt(rd.get(f"ELECTRICAL MASTER BATTERY:{i}", "Bool")) for i in (1, 2)]
        alt = [fmt(rd.get(f"GENERAL ENG MASTER ALTERNATOR:{i}", "Bool")) for i in (1, 2)]
        bus = [fmt(rd.get(f"ELECTRICAL MAIN BUS VOLTAGE:{i}", "Volts")) for i in range(1, N_BUS + 1)]
        load = [fmt(rd.get(f"ELECTRICAL BATTERY LOAD:{i}", "Amperes")) for i in (1, 2)]
        gen = [fmt(rd.get(f"ELECTRICAL GENALT BUS AMPS:{i}", "Amperes")) for i in (1, 2)]
        ctl = "/".join(fmt(rd.get(f"L:SR20_ELEC_{k}", "Number")) for k in ("ROW", "L12", "L13", "L23", "TOGGLES"))
        print(time.strftime("%H:%M:%S"), f"{tag:44s} bat={bat} alt={alt} busV={bus} batA={load} altA={gen} ctl(row/L12/L13/L23/n)={ctl}", flush=True)

    snap("now")
    for step in steps:
        ev, _, val = step.partition("=")
        send_event(sm, ev, val or "0")
        time.sleep(1.0)
        snap(f"after {ev} {val}")
    sm.exit()


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "inventory" and len(a) == 2:
        cmd_inventory(a[1])
    elif a[0] == "watch":
        cmd_watch()
    elif a[0] == "events":
        cmd_events()
    elif a[0] == "send" and len(a) >= 2:
        cmd_send(a[1], a[2] if len(a) > 2 else "0")
    elif a[0] == "snap":
        cmd_snap(a[1:])
    elif a[0] == "cb":
        cmd_cb()
    else:
        sys.exit(__doc__)
