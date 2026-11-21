"""Garmin .fpl (FlightPlan v1 XML) as ForeFlight's "Share FPL File" writes it, reduced to what the G1000 import takes
(docs/plans/flightstream_510.md §11-12).

Seen in ForeFlight's files (2026-11-02, fixtures in tools/efb/fixtures):
  - encoding: UTF-16 with BOM for a plain route, UTF-8 without BOM for one with an approach; both are taken here;
  - <type> is AIRPORT / VOR / NDB / INT / USER WAYPOINT, and empty for most fixes;
  - a procedure is written out fix by fix: each procedure fix carries its <altitude-ft> (an enroute point never does),
    a runway threshold is a point "RW26R", and a hold or course reversal is drawn as made-up points H-01 .. H-nn;
  - <flight-data><altitude-ft> is the cruise altitude, <aircraft-tailnumber> the tail.

reduce() keeps the enroute route: the procedure points (with an altitude), runway points and H-points go, consecutive
repeats are merged. The pilot loads procedures on the PROC page, as in the airplane.

    python -I tools/efb/fpl.py <file.fpl>      print the parsed and reduced route
"""
import re
import sys
import xml.etree.ElementTree as ET

MAX_BYTES = 256 * 1024
MAX_POINTS = 300
NS = "{http://www8.garmin.com/xmlschemas/FlightPlan/v1}"
HOLD_RE = re.compile(r"^H-\d{2,3}$")
RWY_RE = re.compile(r"^RW\d{2}[LRC]?$")


class FplError(ValueError):
    pass


def decode(data):
    """Bytes of an .fpl to text: UTF-16 (BOM) or UTF-8 (BOM optional)."""
    if len(data) > MAX_BYTES:
        raise FplError("file too large")
    try:
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = data.decode("utf-16")
        elif data[:3] == b"\xef\xbb\xbf":
            text = data[3:].decode("utf-8")
        else:
            text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FplError("not UTF-8 / UTF-16")
    # no DTDs, so no entity expansion of any kind (the files never carry one)
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
        raise FplError("DTD / entity declarations are not accepted")
    return text


def _t(el, tag):
    x = el.find(NS + tag) if el is not None else None
    return (x.text or "").strip() if x is not None and x.text else ""


def _f(s):
    try:
        v = float(s)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def parse(data):
    """-> {"name", "tail", "cruise_ft", "points": [{"ident", "type", "lat", "lon", "alt_ft"}]} in route order."""
    text = decode(data) if isinstance(data, (bytes, bytearray)) else data
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)   # the declaration names utf-8 even in the UTF-16 file
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise FplError(f"not XML: {e}")
    if root.tag != NS + "flight-plan":
        raise FplError("not a Garmin flight plan")
    table = {}
    wps = root.findall(f"{NS}waypoint-table/{NS}waypoint")
    if len(wps) > MAX_POINTS:
        raise FplError("too many waypoints")
    for w in wps:
        ident = _t(w, "identifier").upper()
        typ = _t(w, "type").upper()
        lat, lon = _f(_t(w, "lat")), _f(_t(w, "lon"))
        if not ident:
            continue
        if lat is not None and not (-90 <= lat <= 90):
            lat = None
        if lon is not None and not (-180 <= lon <= 180):
            lon = None
        # a repeated identifier (CAZBY twice in the hold) carries the same data; the first one wins
        table.setdefault((ident, typ), {"ident": ident, "type": typ, "lat": lat, "lon": lon, "alt_ft": _f(_t(w, "altitude-ft"))})
    routes = root.findall(f"{NS}route")
    if not routes:
        raise FplError("no route in the file")
    route = routes[0]
    points = []
    for rp in route.findall(f"{NS}route-point"):
        ident, typ = _t(rp, "waypoint-identifier").upper(), _t(rp, "waypoint-type").upper()
        w = table.get((ident, typ))
        if w is None:
            raise FplError(f"route point {ident} not in the waypoint table")
        points.append(dict(w))
    if len(points) > MAX_POINTS:
        raise FplError("too many route points")
    fd = root.find(NS + "flight-data")
    return {"name": _t(route, "route-name"), "tail": _t(root.find(NS + "aircraft"), "aircraft-tailnumber"),
            "cruise_ft": _f(_t(fd, "altitude-ft")), "points": points}


def reduce(plan):
    """The enroute route for the G1000: -> (points, dropped) where dropped lists what was left out and why."""
    keep, dropped = [], {"hold": 0, "procedure": [], "runway": []}
    for p in plan["points"]:
        if HOLD_RE.match(p["ident"]):
            dropped["hold"] += 1
            continue
        if RWY_RE.match(p["ident"]) and p["type"] != "AIRPORT":
            dropped["runway"].append(p["ident"])
            continue
        if p["alt_ft"] is not None and p["type"] != "AIRPORT":
            if p["ident"] not in dropped["procedure"]:
                dropped["procedure"].append(p["ident"])
            continue
        if keep and keep[-1]["ident"] == p["ident"] and keep[-1]["type"] == p["type"]:
            continue
        keep.append({"ident": p["ident"], "type": p["type"], "lat": p["lat"], "lon": p["lon"]})
    return keep, dropped


def load_bytes(data):
    """An .fpl's bytes -> the plan the MFD is offered: {"name", "tail", "cruise_ft", "points", "dropped"}. Raises
    FplError for anything the G1000 import can't take, including a route of fewer than two points."""
    plan = parse(data)
    pts, dropped = reduce(plan)
    if len(pts) < 2:
        raise FplError("fewer than two route points")
    return {"name": plan["name"], "tail": plan["tail"], "cruise_ft": plan["cruise_ft"], "points": pts, "dropped": dropped}


def load(path):
    with open(path, "rb") as fh:
        data = fh.read(MAX_BYTES + 1)
    return load_bytes(data)


if __name__ == "__main__":
    import json
    print(json.dumps(load(sys.argv[1]), indent=1))
