"""SR20 SurfaceWatch - add an airport's taxiway map to the mod (docs/plans/surfacewatch.md §3.1).

Run this while Microsoft Flight Simulator 2024 is running (any aircraft, anywhere). It asks the sim for the
airport's taxiway layout through SimConnect, checks it, and writes one file per airport into the mod:
    <mod>/html_ui/SR20SurfaceWatch/airports/<ICAO>.json
plus airports/index.json, then regenerates the mod's layout.json. The airport is available at the next sim start.
A bad or incomplete answer from the sim never replaces an existing good file.

Usage:  python -I tools/surfacewatch/add_airport.py KSNA [KSQL ...]     add / refresh airports
        python -I tools/surfacewatch/add_airport.py --list              what is installed
        python -I tools/surfacewatch/add_airport.py --remove KSQL       remove one
        python -I tools/surfacewatch/add_airport.py --check             re-validate every installed file
Options: --package <path to the mod folder> (default: this repo's package/paulw-aircraft-sr20)
         --dll <SimConnect.dll> (default: the copy beside this script, else the MSFS 2024 SDK's)
         --no-layout (skip layout.json)
Needs the MSFS 2024 SimConnect.dll (facility-data API); the pip "SimConnect" package's DLL is too old.

Export contract (probed at KSNA and KSQL, 2026-10-23, docs/probes.md "P0 SurfaceWatch"):
  - BIAS_X is metres east, BIAS_Z metres north of the airport reference point (runway spans match at both airports);
  - all indices are 0-based; TAXI_PATH START/END index TAXI_POINT, except a PARKING-type path whose END indexes
    TAXI_PARKING; NAME_INDEX indexes TAXI_NAME, 0 = the empty name (unnamed);
  - named taxiways are TAXI_PATH type 4 (PATH) in both airports; type 1 (TAXI) never appeared; type 2 RUNWAY paths
    carry RUNWAY_NUMBER / DESIGNATOR (meaningless for other types); 3 PARKING = connectors; 6 VEHICLE = roads;
  - curved taxiways are many short segments (median 12-20 m); TAXI_POINT TYPE 2/4/5/6 are hold-short points.
"""
import argparse
import ctypes
import datetime as dt
import json
import os
import struct
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DEFAULT_PACKAGE = os.environ.get("SR20_PACKAGE") or os.path.join(ROOT, "package", "paulw-aircraft-sr20")   # Companion: installed package
PLUGIN_REL = os.path.join("SimObjects", "Airplanes", "asobo_sr22", "presets", "microsoft", "sr20", "panel", "Instruments", "G1000", "Plugins")
AIRPORTS_REL = os.path.join("html_ui", "SR20SurfaceWatch", "airports")   # coui://html_ui/... is fetchable from the PFD; SimObjects paths are not (probed 2026-10-23)
DLL_CANDIDATES = [p for p in (os.environ.get("SR20_SIMCONNECT_DLL"), os.path.join(HERE, "lib", "SimConnect.dll"),
                                 r"C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll") if p]
SCHEMA_VERSION = 1
TOOL = "add_airport 1.0"

RECV_ID_EXCEPTION, RECV_ID_FACILITY_DATA, RECV_ID_FACILITY_DATA_END = 1, 28, 29
FAC_TYPES = {0: "AIRPORT", 1: "RUNWAY", 14: "TAXI_POINT", 15: "TAXI_PARKING", 16: "TAXI_PATH", 17: "TAXI_NAME"}
PATH_TYPES = {0: "NONE", 1: "TAXI", 2: "RUNWAY", 3: "PARKING", 4: "PATH", 5: "CLOSED", 6: "VEHICLE", 7: "ROAD", 8: "PAINTEDLINE"}
PATH_KEEP = {1, 2, 3, 4}            # TAXI, RUNWAY (reference), PARKING (connectors), PATH (the named taxiways)
POINT_TYPES = {0: "NONE", 1: "NORMAL", 2: "HOLD_SHORT", 4: "ILS_HOLD_SHORT", 5: "HOLD_SHORT_NO_DRAW", 6: "ILS_HOLD_SHORT_NO_DRAW"}
PARKING_NAMES = {0: "", 1: "PARKING", 2: "N PARKING", 3: "NE PARKING", 4: "E PARKING", 5: "SE PARKING", 6: "S PARKING", 7: "SW PARKING",
                 8: "W PARKING", 9: "NW PARKING", 10: "GATE", 11: "DOCK"}
PARKING_NAMES.update({12 + i: f"GATE {chr(65 + i)}" for i in range(26)})
DESIGNATORS = {0: "", 1: "L", 2: "R", 3: "C", 4: "W", 5: "A", 6: "B"}

DEFINITION = [
    ("AIRPORT", [("LATITUDE", "d"), ("LONGITUDE", "d"), ("ALTITUDE", "d"), ("NAME", "32s"), ("ICAO", "8s"),
                 ("N_RUNWAYS", "i"), ("N_TAXI_POINTS", "i"), ("N_TAXI_PARKINGS", "i"), ("N_TAXI_PATHS", "i"), ("N_TAXI_NAMES", "i")]),
    ("RUNWAY", [("LATITUDE", "d"), ("LONGITUDE", "d"), ("ALTITUDE", "d"), ("HEADING", "f"), ("LENGTH", "f"), ("WIDTH", "f"),
                ("PRIMARY_NUMBER", "i"), ("PRIMARY_DESIGNATOR", "i"), ("SECONDARY_NUMBER", "i"), ("SECONDARY_DESIGNATOR", "i")]),
    ("TAXI_POINT", [("TYPE", "i"), ("ORIENTATION", "i"), ("BIAS_X", "f"), ("BIAS_Z", "f")]),
    ("TAXI_PARKING", [("TYPE", "i"), ("TAXI_POINT_TYPE", "i"), ("NAME", "i"), ("SUFFIX", "i"), ("NUMBER", "I"), ("ORIENTATION", "i"),
                      ("HEADING", "f"), ("RADIUS", "f"), ("BIAS_X", "f"), ("BIAS_Z", "f")]),
    ("TAXI_PATH", [("TYPE", "i"), ("WIDTH", "f"), ("LEFT_HALF_WIDTH", "f"), ("RIGHT_HALF_WIDTH", "f"), ("WEIGHT", "I"),
                   ("RUNWAY_NUMBER", "i"), ("RUNWAY_DESIGNATOR", "i"), ("LEFT_EDGE", "i"), ("RIGHT_EDGE", "i"), ("CENTER_LINE", "i"),
                   ("START", "i"), ("END", "i"), ("NAME_INDEX", "I")]),
    ("TAXI_NAME", [("NAME", "32s")]),
]
FORMATS = {name: ("<" + "".join(f for _, f in fields), [n for n, _ in fields]) for name, fields in DEFINITION}


class Recv(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("dwSize", wintypes.DWORD), ("dwVersion", wintypes.DWORD), ("dwID", wintypes.DWORD)]


class RecvFacilityData(ctypes.Structure):
    _pack_ = 1
    _fields_ = Recv._fields_ + [("UserRequestId", wintypes.DWORD), ("UniqueRequestId", wintypes.DWORD), ("ParentUniqueRequestId", wintypes.DWORD),
                                ("Type", wintypes.DWORD), ("IsListItem", wintypes.DWORD), ("ItemIndex", wintypes.DWORD), ("ListSize", wintypes.DWORD)]


class RecvException(ctypes.Structure):
    _pack_ = 1
    _fields_ = Recv._fields_ + [("dwException", wintypes.DWORD), ("dwSendID", wintypes.DWORD), ("dwIndex", wintypes.DWORD)]


class ExportError(Exception):
    pass


def load_dll(path):
    dll = ctypes.WinDLL(path)
    sigs = {
        "SimConnect_Open": [ctypes.POINTER(wintypes.HANDLE), ctypes.c_char_p, wintypes.HWND, wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD],
        "SimConnect_Close": [wintypes.HANDLE],
        "SimConnect_AddToFacilityDefinition": [wintypes.HANDLE, wintypes.DWORD, ctypes.c_char_p],
        "SimConnect_RequestFacilityData": [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_char_p, ctypes.c_char_p],
        "SimConnect_GetNextDispatch": [wintypes.HANDLE, ctypes.POINTER(ctypes.POINTER(Recv)), ctypes.POINTER(wintypes.DWORD)],
    }
    for fn, args in sigs.items():
        f = getattr(dll, fn)
        f.argtypes = args
        f.restype = ctypes.c_long
    return dll


def fetch(dll, icao, timeout=30.0):
    """Returns {typeName: [fields, ...]} for one airport, or raises ExportError."""
    h = wintypes.HANDLE()
    if dll.SimConnect_Open(ctypes.byref(h), b"SR20 SurfaceWatch add_airport", None, 0, None, 0) < 0:
        raise ExportError("could not connect to the simulator (is MSFS 2024 running?)")
    try:
        define_id, request_id = 7, 77
        for name, fields in DEFINITION:
            for text in [f"OPEN {name}"] + [f for f, _ in fields] + ([] if name == "AIRPORT" else [f"CLOSE {name}"]):
                if dll.SimConnect_AddToFacilityDefinition(h, define_id, text.encode()) < 0:
                    raise ExportError(f"the simulator rejected the definition at '{text}'")
        if dll.SimConnect_AddToFacilityDefinition(h, define_id, b"CLOSE AIRPORT") < 0:
            raise ExportError("the simulator rejected the definition at 'CLOSE AIRPORT'")
        if dll.SimConnect_RequestFacilityData(h, define_id, request_id, icao.encode(), b"") < 0:
            raise ExportError("the facility request was refused")
        out, p, n, t0 = {}, ctypes.POINTER(Recv)(), wintypes.DWORD(), time.time()
        while time.time() - t0 < timeout:
            if dll.SimConnect_GetNextDispatch(h, ctypes.byref(p), ctypes.byref(n)) < 0:
                time.sleep(0.005)
                continue
            rid = p.contents.dwID
            if rid == RECV_ID_EXCEPTION:
                e = ctypes.cast(p, ctypes.POINTER(RecvException)).contents
                raise ExportError(f"the simulator reported exception {e.dwException} (unknown airport '{icao}', or no facility data)")
            if rid == RECV_ID_FACILITY_DATA:
                f = ctypes.cast(p, ctypes.POINTER(RecvFacilityData)).contents
                tname = FAC_TYPES.get(f.Type)
                if tname is None:
                    continue
                raw = ctypes.string_at(ctypes.addressof(p.contents) + ctypes.sizeof(RecvFacilityData), n.value - ctypes.sizeof(RecvFacilityData))
                fmt, names = FORMATS[tname]
                if len(raw) < struct.calcsize(fmt):
                    raise ExportError(f"short {tname} record ({len(raw)} bytes)")
                vals = struct.unpack(fmt, raw[:struct.calcsize(fmt)])
                rec = {k: (v.split(b"\0", 1)[0].decode("latin-1") if isinstance(v, bytes) else v) for k, v in zip(names, vals)}
                lst = out.setdefault(tname, [])
                if f.IsListItem and f.ItemIndex != len(lst):
                    raise ExportError(f"{tname} list out of order (item {f.ItemIndex} after {len(lst)})")
                lst.append(rec)
            elif rid == RECV_ID_FACILITY_DATA_END:
                return out
        raise ExportError("timed out waiting for the simulator")
    finally:
        dll.SimConnect_Close(h)


def norm_name(s):
    return "".join(ch for ch in s.strip().upper() if 32 <= ord(ch) < 127)


def build(icao, data, sim_version):
    """Raw records -> the airport JSON (schema 1), with every index validated. Raises ExportError on inconsistency."""
    apt = (data.get("AIRPORT") or [None])[0]
    if not apt:
        raise ExportError("no airport record")
    if norm_name(apt["ICAO"]) != icao:
        raise ExportError(f"the simulator answered for '{apt['ICAO']}', not '{icao}'")
    pts, prk, paths, names = data.get("TAXI_POINT", []), data.get("TAXI_PARKING", []), data.get("TAXI_PATH", []), data.get("TAXI_NAME", [])
    for key, lst in (("N_TAXI_POINTS", pts), ("N_TAXI_PARKINGS", prk), ("N_TAXI_PATHS", paths), ("N_TAXI_NAMES", names)):
        if apt[key] != len(lst):
            raise ExportError(f"{key} = {apt[key]} but {len(lst)} records arrived")
    if not names and not paths:
        raise ExportError("no taxiways in the simulator's data (heliport or small strip)")
    if not names or norm_name(names[0]["NAME"]) != "":
        raise ExportError("name index 0 is expected to be the empty name")
    rejected = []
    out_paths, kept_by_type, dropped_by_type = [], {}, {}
    for i, p in enumerate(paths):
        t = p["TYPE"]
        if t not in PATH_KEEP:
            dropped_by_type[PATH_TYPES.get(t, str(t))] = dropped_by_type.get(PATH_TYPES.get(t, str(t)), 0) + 1
            continue
        a_kind, b_kind = "point", ("parking" if t == 3 else "point")
        ok = 0 <= p["START"] < len(pts) and (0 <= p["END"] < (len(prk) if b_kind == "parking" else len(pts)))
        if not ok:
            rejected.append({"path": i, "why": f"START {p['START']} / END {p['END']} out of range for type {PATH_TYPES.get(t)}"})
            continue
        if not (0 <= p["NAME_INDEX"] < len(names)):
            rejected.append({"path": i, "why": f"NAME_INDEX {p['NAME_INDEX']} out of range"})
            continue
        w = max(0.0, float(p["WIDTH"]))
        hl, hr = float(p["LEFT_HALF_WIDTH"]), float(p["RIGHT_HALF_WIDTH"])
        if not (0 < hl + hr <= 200):
            hl = hr = w / 2
        entry = {"i": i, "type": t, "typeName": PATH_TYPES[t], "a": {"kind": a_kind, "i": p["START"]}, "b": {"kind": b_kind, "i": p["END"]},
                 "width": round(w, 2), "halfL": round(hl, 2), "halfR": round(hr, 2), "nameIndex": p["NAME_INDEX"],
                 "label": norm_name(names[p["NAME_INDEX"]]["NAME"])}
        if t == 2:
            entry["rwy"] = {"number": p["RUNWAY_NUMBER"], "designator": DESIGNATORS.get(p["RUNWAY_DESIGNATOR"], "")}
        out_paths.append(entry)
        kept_by_type[PATH_TYPES[t]] = kept_by_type.get(PATH_TYPES[t], 0) + 1
    if len(rejected) > len(paths) * 0.02:
        raise ExportError(f"{len(rejected)} of {len(paths)} paths rejected - the export contract does not hold for this airport")
    runways_seen = {}
    for e in out_paths:
        if e["type"] == 2:
            k = f"{e['rwy']['number']:02d}{e['rwy']['designator']}"
            runways_seen[k] = runways_seen.get(k, 0) + 1
    return {
        "schemaVersion": SCHEMA_VERSION,
        "icao": icao,
        "name": apt["NAME"].strip(),
        "ref": {"lat": apt["LATITUDE"], "lon": apt["LONGITUDE"], "elevFt": round(apt["ALTITUDE"] / 0.3048, 1)},
        "source": {"sim": sim_version, "exportedUtc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tool": TOOL,
                   "frame": "x = BIAS_X metres east of ref, y = BIAS_Z metres north of ref; indices 0-based; PARKING paths end on a parking",
                   "counts": {"points": len(pts), "parkings": len(prk), "paths": len(paths), "names": len(names), "runways": apt["N_RUNWAYS"]},
                   "kept": kept_by_type, "dropped": dropped_by_type, "rejected": rejected},
        "names": [{"index": i, "raw": n["NAME"], "label": norm_name(n["NAME"])} for i, n in enumerate(names)],
        "points": [{"i": i, "x": round(p["BIAS_X"], 1), "y": round(p["BIAS_Z"], 1), "type": p["TYPE"], "orient": p["ORIENTATION"]} for i, p in enumerate(pts)],
        "parkings": [{"i": i, "x": round(q["BIAS_X"], 1), "y": round(q["BIAS_Z"], 1), "radius": round(q["RADIUS"], 1), "type": q["TYPE"],
                      "name": q["NAME"], "suffix": q["SUFFIX"], "number": q["NUMBER"], "heading": round(q["HEADING"], 1),
                      "label": (PARKING_NAMES.get(q["NAME"], "") + (" " + PARKING_NAMES.get(q["SUFFIX"], "") if q["SUFFIX"] else "")
                                + (f" {q['NUMBER']}" if q["NUMBER"] else "")).strip()} for i, q in enumerate(prk)],
        "paths": out_paths,
        "runwaysSeen": [{"ident": k, "paths": v} for k, v in sorted(runways_seen.items())],
        "runways": [{"lat": r["LATITUDE"], "lon": r["LONGITUDE"], "elevFt": round(r["ALTITUDE"] / 0.3048, 1), "headingTrue": round(r["HEADING"], 2),
                     "lengthM": round(r["LENGTH"], 1), "widthM": round(r["WIDTH"], 1),
                     "primary": f"{r['PRIMARY_NUMBER']:02d}{DESIGNATORS.get(r['PRIMARY_DESIGNATOR'], '')}",
                     "secondary": f"{r['SECONDARY_NUMBER']:02d}{DESIGNATORS.get(r['SECONDARY_DESIGNATOR'], '')}"} for r in data.get("RUNWAY", [])],
    }


def validate(doc):
    """Structural check of a file as the plugin will read it (used by --check and before writing)."""
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        return "schemaVersion"
    np_, nk = len(doc["points"]), len(doc["parkings"])
    for e in doc["paths"]:
        for end in ("a", "b"):
            k, i = e[end]["kind"], e[end]["i"]
            if not (0 <= i < (nk if k == "parking" else np_)):
                return f"path {e['i']} {end} out of range"
        if not (0 <= e["nameIndex"] < len(doc["names"])):
            return f"path {e['i']} nameIndex"
    return None


def sim_version_string():
    return "MSFS 2024"


def write_airport(package, doc):
    d = os.path.join(package, AIRPORTS_REL)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{doc['icao']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def write_index(package):
    d = os.path.join(package, AIRPORTS_REL)
    os.makedirs(d, exist_ok=True)
    entries = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".json") and fn != "index.json":
            try:
                doc = json.load(open(os.path.join(d, fn), encoding="utf-8"))
                entries.append({"icao": doc["icao"], "name": doc.get("name", ""), "lat": doc["ref"]["lat"], "lon": doc["ref"]["lon"], "exportedUtc": doc["source"]["exportedUtc"],
                                "paths": len(doc["paths"]), "names": sorted({e.get("label", "") for e in doc["paths"] if e.get("label")}),
                                "osm": bool(doc.get("osm", {}).get("applied"))})
            except Exception as e:   # noqa: BLE001
                entries.append({"icao": fn[:-5], "error": str(e)})
    with open(os.path.join(d, "index.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"schemaVersion": SCHEMA_VERSION, "airports": entries}, fh, indent=1)
    return entries


def regen_layout(package, quiet=False):
    """layout.json, in-process (tools/make_layout.py; also inside the frozen SR20 Companion, where no python.exe runs)."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import io
    import contextlib
    import make_layout
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        make_layout.main(package)
    if not quiet:
        print("  " + out.getvalue().strip())


def main():
    ap = argparse.ArgumentParser(description="SR20 SurfaceWatch: add an airport's taxiways to the mod")
    ap.add_argument("icaos", nargs="*")
    ap.add_argument("--package", default=DEFAULT_PACKAGE)
    ap.add_argument("--dll")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--remove")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-layout", action="store_true")
    ap.add_argument("--no-osm", action="store_true", help="keep the simulator's taxiway names (no OpenStreetMap lookup)")
    a = ap.parse_args()
    airports_dir = os.path.join(a.package, AIRPORTS_REL)
    if a.list or a.check:
        entries = write_index(a.package)
        for e in entries:
            if "error" in e:
                print(f"  {e['icao']}: BROKEN ({e['error']})")
                continue
            problem = validate(json.load(open(os.path.join(airports_dir, e["icao"] + ".json"), encoding="utf-8"))) if a.check else None
            print(f"  {e['icao']}: {e['name']}  ({e['paths']} paths, taxiways {' '.join(e['names'])}; exported {e['exportedUtc']})" + (f"  INVALID: {problem}" if problem else ""))
        if not entries:
            print("  no airports installed")
        return
    if a.remove:
        p = os.path.join(airports_dir, a.remove.upper() + ".json")
        if os.path.exists(p):
            os.remove(p)
            print(f"removed {a.remove.upper()}")
        else:
            print(f"{a.remove.upper()} is not installed")
        write_index(a.package)
        if not a.no_layout:
            regen_layout(a.package)
        return
    if not a.icaos:
        try:
            a.icaos = input("Airport ICAO(s) to add (e.g. KSNA KSQL): ").split()
        except EOFError:
            a.icaos = []
    if not a.icaos:
        ap.error("give at least one ICAO")
    dll_path = a.dll or next((p for p in DLL_CANDIDATES if os.path.exists(p)), None)
    if not dll_path:
        raise SystemExit("SimConnect.dll not found: install the MSFS 2024 SDK or put the 2024 SimConnect.dll in tools/surfacewatch/lib/")
    dll = load_dll(dll_path)
    added = 0
    for icao in [s.upper() for s in a.icaos]:
        try:
            data = fetch(dll, icao)
            doc = build(icao, data, sim_version_string())
            problem = validate(doc)
            if problem:
                raise ExportError(f"self-check failed: {problem}")
            if not a.no_osm:
                sys.path.insert(0, HERE)
                import osm_names   # noqa: E402 - taxiway names from OpenStreetMap where the sim's are wrong (KCNO 2026-10-29)
                osm_names.relabel(doc)
            path = write_airport(a.package, doc)
            k = doc["source"]["kept"]
            print(f"{icao}: {doc['name']} - {doc['source']['counts']['points']} points, {len(doc['paths'])} paths kept "
                  f"({', '.join(f'{v} {t}' for t, v in k.items())}; dropped {doc['source']['dropped'] or 'none'}; rejected {len(doc['source']['rejected'])}), "
                  f"taxiways {' '.join(n['label'] for n in doc['names'] if n['label']) or '(none named)'}, runways {', '.join(r['primary'] + '/' + r['secondary'] for r in doc['runways'])}")
            print(f"  wrote {path}")
            added += 1
        except ExportError as e:
            print(f"{icao}: NOT added - {e}")
    write_index(a.package)
    if added and not a.no_layout:
        regen_layout(a.package)
    if added:
        print("Done. The airport(s) are available the next time the simulator starts.")


if __name__ == "__main__":
    main()
