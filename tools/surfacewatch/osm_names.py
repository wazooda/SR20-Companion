"""SR20 SurfaceWatch: taxiway names from OpenStreetMap for an exported airport map.

Why: the simulator's airport data names its taxiways freely at airports Asobo never hand-built (KCNO 2026-10-29: the
sim had A on the east-end crossover that is really P, a G that does not exist, and no L / M / N / P / Q at all; the author
checked against the FAA diagram). The geometry matched the field; only the letters were wrong, and SafeTaxi's
identifier boxes and SurfaceWatch's "TWY x" alerts both read them.

How: fetch OSM ways tagged aeroway=taxiway around the field (Overpass API, mirrors in order, cached per airport in
tools/surfacewatch/osm_cache/), bring them into the map's local metre frame, and give every TAXI / PATH segment of
the sim map the `ref` of the OSM taxiway it lies on (close and parallel). Segments on no OSM taxiway lose their sim
name (unnamed pavement still draws). The sim's name is kept in `simLabel`. The whole relabel is skipped, map left as
exported, when OSM knows fewer than MIN_REFS named taxiways there or matches too little of the sim's named pavement.
`names` (the sim's name table, used by nameIndex) is left alone; index.json lists the labels actually on the paths.

Run by airport_server.py / add_airport.py after each export; also by hand:
    python -I tools/surfacewatch/osm_names.py KCNO            relabel the package's KCNO.json in place
    python -I tools/surfacewatch/osm_names.py --all           every exported airport
    python -I tools/surfacewatch/osm_names.py KCNO --dry-run  report only
    python -I tools/surfacewatch/osm_names.py KCNO --out f    write the result elsewhere
    --refresh refetches OSM (cache otherwise), --no-net uses the cache only.
Data (c) OpenStreetMap contributors, ODbL (www.openstreetmap.org/copyright).
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CACHE_DIR = os.path.join(os.environ["SR20_DATA"], "osm_cache") if os.environ.get("SR20_DATA") else os.path.join(HERE, "osm_cache")   # SR20_DATA: SR20 Companion
AIRPORTS_REL = os.path.join("html_ui", "SR20SurfaceWatch", "airports")
DEFAULT_PACKAGE = os.environ.get("SR20_PACKAGE") or os.path.join(ROOT, "package", "paulw-aircraft-sr20")
OSM_API = "https://api.openstreetmap.org/api/0.6"
MIRRORS = ["https://overpass.private.coffee/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
           "https://overpass-api.de/api/interpreter"]
USER_AGENT = "MSFS-SR20-Mod SafeTaxi (github.com/wazooda)"
TIMEOUT_S = 25
MARGIN_M = 400            # bbox margin around the sim's taxi points
MIN_REFS = 3              # OSM must know at least this many named taxiways, else the sim names stay
MIN_MATCH_FRACTION = 0.4  # ... and match at least this share of the sim's named taxiway length
MAX_ANGLE_DEG = 35        # sim segment vs OSM way direction
SMOOTH_MAX_M = 25         # a piece up to this long takes the label its same-named neighbours agree on (junction fillets)
TOL_MIN_M = 10            # distance tolerance: max(TOL_MIN_M, half width + 5)
KEEP_NEAR_M = 120         # a sim name on no OSM way is kept when OSM has that name within this distance (runway crossings)
CONFLICT_NEAR_M = 100     # ... and even on another named OSM way when its own name is this close (junction fillets: KSNA's
                          # L runs 96 m past the end of OSM's L into B; the author taxied it 2026-10-23)
R_EARTH = 6371000.0


def to_local(ref, lat, lon):
    k = math.pi / 180
    return ((lon - ref["lon"]) * k * R_EARTH * math.cos(ref["lat"] * k), (lat - ref["lat"]) * k * R_EARTH)


def bbox(doc):
    """(south, west, north, east) around the sim's taxi points, MARGIN_M wide."""
    xs = [p["x"] for p in doc["points"]] or [0.0]
    ys = [p["y"] for p in doc["points"]] or [0.0]
    k = math.pi / 180
    ref = doc["ref"]
    dlat = 1 / (k * R_EARTH)
    dlon = 1 / (k * R_EARTH * math.cos(ref["lat"] * k))
    return (ref["lat"] + (min(ys) - MARGIN_M) * dlat, ref["lon"] + (min(xs) - MARGIN_M) * dlon,
            ref["lat"] + (max(ys) + MARGIN_M) * dlat, ref["lon"] + (max(xs) + MARGIN_M) * dlon)


def _get(url, data=None, accept="*/*"):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read()


def fetch_api(s, w, n, e, depth=0):
    """Taxiway ways from the plain OSM map call (fast, no queue; 2 s and 4.5 MB for KCNO). The call refuses boxes with
    more than 50 000 nodes (HTTP 400), so a box that is refused is split in four. Returns [(ref, [(lat, lon), ...])]."""
    import xml.etree.ElementTree as ET
    try:
        body = _get(f"{OSM_API}/map?bbox={w:.5f},{s:.5f},{e:.5f},{n:.5f}")
    except urllib.error.HTTPError as ex:
        if ex.code == 400 and depth < 3:
            ms, me = (s + n) / 2, (w + e) / 2
            return (fetch_api(s, w, ms, me, depth + 1) + fetch_api(s, me, ms, e, depth + 1)
                    + fetch_api(ms, w, n, me, depth + 1) + fetch_api(ms, me, n, e, depth + 1))
        raise
    root = ET.fromstring(body)
    nodes = {nd.get("id"): (float(nd.get("lat")), float(nd.get("lon"))) for nd in root.iter("node")}
    out = []
    for way in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        if tags.get("aeroway") != "taxiway":
            continue
        pts = [nodes[nd.get("ref")] for nd in way.findall("nd") if nd.get("ref") in nodes]
        if len(pts) >= 2:
            out.append((tags.get("ref", ""), pts))
    return out


def fetch_overpass(s, w, n, e, log=print):
    """The same from an Overpass mirror (queued, often slow or at capacity): fallback only."""
    q = f'[out:json][timeout:{TIMEOUT_S - 3}];way["aeroway"="taxiway"]({s:.5f},{w:.5f},{n:.5f},{e:.5f});out geom;'
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for url in MIRRORS:
        try:
            reply = json.loads(_get(url, data, "application/json"))
            return [(el.get("tags", {}).get("ref", ""), [(g["lat"], g["lon"]) for g in el["geometry"]])
                    for el in reply.get("elements", []) if el.get("type") == "way" and el.get("geometry")
                    and el.get("tags", {}).get("aeroway") == "taxiway"]
        except Exception as ex:   # noqa: BLE001 - try the next mirror
            last = ex
            log(f"osm: {url.split('/')[2]}: {type(ex).__name__}: {str(ex)[:80]}")
    raise OSError(f"Overpass unavailable: {last}")


def fetch(doc, log=print):
    """{'ways': [{'ref', 'pts': [[lat, lon], ...]}], '_fetchedUtc', '_source'} for the field. Raises OSError when
    both the OSM API and every Overpass mirror fail."""
    s, w, n, e = bbox(doc)
    try:
        found, source = fetch_api(s, w, n, e), OSM_API
    except Exception as ex:   # noqa: BLE001
        log(f"osm: api.openstreetmap.org: {type(ex).__name__}: {str(ex)[:80]}; trying Overpass")
        found, source = fetch_overpass(s, w, n, e, log), "overpass"
    return {"ways": [{"ref": r, "pts": [[round(la, 7), round(lo, 7)] for la, lo in pts]} for r, pts in found],
            "_fetchedUtc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "_source": source}


def cached(doc, refresh=False, no_net=False, log=print):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, doc["icao"] + ".json")
    if not refresh and os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    if no_net:
        raise OSError("no OSM cache and --no-net")
    out = fetch(doc, log)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, separators=(",", ":"))
    return out


def ways(osm, ref):
    """[(ref or '', [(x, y), ...])] in the map's local frame."""
    out = []
    for w in osm.get("ways", []):
        pts = [to_local(ref, la, lo) for la, lo in w["pts"]]
        if len(pts) >= 2:
            out.append(((w.get("ref") or "").strip().upper(), pts))
    return out


def seg_dist(p, a, b):
    """Distance from p to segment ab, and the segment's direction angle (deg)."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - ax, py - ay), 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), math.degrees(math.atan2(dy, dx))


def angle_diff(a, b):
    d = abs((a - b + 180) % 360 - 180)
    return min(d, 180 - d)   # undirected


def nearest(p, way):
    best = (float("inf"), 0.0)
    for i in range(len(way) - 1):
        d, ang = seg_dist(p, way[i], way[i + 1])
        if d < best[0]:
            best = (d, ang)
    return best


def candidates(seg_a, seg_b, half, osm_ways, tol, max_angle):
    """OSM ways the sim segment lies on: [(ref, mean distance)] nearest first. Every sample point of the segment must be
    within tol of the way and, for segments of 8 m or more, the directions within max_angle."""
    ax, ay = seg_a
    bx, by = seg_b
    length = math.hypot(bx - ax, by - ay)
    my_ang = math.degrees(math.atan2(by - ay, bx - ax))
    samples = [((ax + bx) / 2, (ay + by) / 2)] if length < 8 else [(ax + (bx - ax) * f, ay + (by - ay) * f) for f in (0.2, 0.5, 0.8)]
    out = []
    for ref, way in osm_ways:
        dists, ok = [], True
        for s in samples:
            d, ang = nearest(s, way)
            if d > tol or (length >= 8 and angle_diff(ang, my_ang) > max_angle):
                ok = False
                break
            dists.append(d)
        if ok:
            out.append((ref, sum(dists) / len(dists)))
    out.sort(key=lambda c: c[1])
    return out


def decide(seg_a, seg_b, half, sim_label, osm_ways, known_refs):
    """The label a sim segment gets: ('label', why). Rules, in order:
    1. the sim's own name is on an OSM way the segment roughly lies on (loose tolerance): keep it;
    2. the segment lies (strict tolerance) on a named OSM taxiway: that name (KCNO: the sim's "A" crossover is P);
    3. it lies only on OSM pavement without a name: keep the sim's name (KSNA: OSM's B stops short, the sim's goes on);
    4. it lies on no OSM way: keep the sim's name when OSM has no taxiway of that name at the field at all (KSNA has
       no A in OSM) or has it within KEEP_NEAR_M (the sim's K crossing the runways where OSM's K stops at the edges),
       drop it when OSM puts that name elsewhere (the sim is contradicted)."""
    tol = max(TOL_MIN_M, half + 5)
    loose = candidates(seg_a, seg_b, half, osm_ways, max(2 * tol, 25), 60)
    if sim_label and any(r == sim_label for r, _ in loose):
        return sim_label, "agree"
    strict = [c for c in loose if c[1] <= tol]
    strict = candidates(seg_a, seg_b, half, osm_ways, tol, MAX_ANGLE_DEG) if strict else []
    named = [c for c in strict if c[0]]
    mid = ((seg_a[0] + seg_b[0]) / 2, (seg_a[1] + seg_b[1]) / 2)
    own = min((nearest(mid, w)[0] for r, w in osm_ways if r == sim_label), default=float("inf")) if sim_label else float("inf")
    if named and own <= CONFLICT_NEAR_M:
        return sim_label, "near"   # junction fillets: the sim's L into B at KSNA where OSM's L stops short of B
    if named:
        return named[0][0], "osm"
    if strict:
        return sim_label, "unnamed"
    if sim_label and sim_label not in known_refs:
        return sim_label, "silent"
    if sim_label:
        if own <= KEEP_NEAR_M:
            return sim_label, "near"   # KSNA: the sim's K crosses the runways, OSM's K stops at their edges
        return "", "dropped"
    return "", "none"


def match(seg_a, seg_b, half, osm_ways):
    """Nearest OSM taxiway the segment lies on (strict tolerance): (ref, mean distance) or None."""
    c = candidates(seg_a, seg_b, half, osm_ways, max(TOL_MIN_M, half + 5), MAX_ANGLE_DEG)
    return c[0] if c else None


def smooth(results):
    """Junction fillets: a piece up to SMOOTH_MAX_M whose neighbours (same sim name, shared endpoint) all carry one other
    label takes it. KSNA 2026-10-29: the 8 m end of L at the K / B junction lay on OSM's K and became K, which hid L in
    the "approaching" text the author checked on 2026-10-23. Two passes, so a chain of two short pieces follows too."""
    key = lambda p: (round(p[0] * 10), round(p[1] * 10))   # noqa: E731
    by_end = {}
    for idx, r in enumerate(results):
        for p in r[4]:
            by_end.setdefault((r[1], key(p)), []).append(idx)
    for _ in range(2):
        for idx, r in enumerate(results):
            if r[5] > SMOOTH_MAX_M:
                continue
            labels = {results[j][2] for p in r[4] for j in by_end.get((r[1], key(p)), []) if j != idx}
            if len(labels) == 1:
                lbl = labels.pop()
                if lbl != r[2]:
                    r[2], r[3] = lbl, "smoothed"


def apply(doc, osm, log=print):
    """Relabels doc in place. Returns the report dict stored under doc['osm']."""
    ref = doc["ref"]
    osm_ways = ways(osm, ref)
    refs = sorted({r for r, _ in osm_ways if r})
    report = {"attribution": "(c) OpenStreetMap contributors, ODbL", "fetchedUtc": osm.get("_fetchedUtc", ""), "source": osm.get("_source", ""),
              "ways": len(osm_ways), "refs": refs, "applied": False, "why": ""}
    if len(refs) < MIN_REFS:
        report["why"] = f"only {len(refs)} named OSM taxiways"
        doc["osm"] = report
        return report
    pts = {("point", p["i"]): (p["x"], p["y"]) for p in doc["points"]}
    pts.update({("parking", q["i"]): (q["x"], q["y"]) for q in doc["parkings"]})
    known = set(refs)
    results = []
    named_len = confirmed_len = 0.0
    for e in doc["paths"]:
        if e["typeName"] not in ("TAXI", "PATH"):
            continue
        a, b = pts.get((e["a"]["kind"], e["a"]["i"])), pts.get((e["b"]["kind"], e["b"]["i"]))
        if not a or not b:
            continue
        half = max(float(e.get("halfL", 0)), float(e.get("halfR", 0)), float(e.get("width", 0)) / 2)
        sim_label = e.get("simLabel", e.get("label", ""))
        new, why = decide(a, b, half, sim_label, osm_ways, known)
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if sim_label:
            named_len += length
            if why in ("agree", "osm", "near"):
                confirmed_len += length
        results.append([e, sim_label, new, why, (a, b), length])
    smooth(results)
    frac = confirmed_len / named_len if named_len else 1.0   # a map the sim left unnamed (KONT) has nothing to contradict
    whys = {}
    for r in results:
        whys[r[3]] = whys.get(r[3], 0) + 1
    report.update({"segments": len(results), "rules": whys, "namedLengthConfirmed": round(frac, 2)})
    if frac < MIN_MATCH_FRACTION:
        report["why"] = f"OSM confirms only {frac:.0%} of the sim's named taxiway length"
        doc["osm"] = report
        return report
    changed = 0
    for e, sim_label, new, _, _, _ in results:
        e["simLabel"] = sim_label
        if e.get("label", "") != new:
            changed += 1
        e["label"] = new
    report.update({"applied": True, "changed": changed, "labels": sorted({e["label"] for e in doc["paths"] if e.get("label")})})
    doc["osm"] = report
    log(f"osm: {doc['icao']}: {len(results)} segments, rules {whys}, {frac:.0%} of named length confirmed, "
        f"{changed} labels changed; now {' '.join(report['labels'])}")
    return report


def relabel(doc, refresh=False, no_net=False, log=print):
    """Fetch (or cache) + apply. Never raises: on any failure the doc is left as it was and the reason logged."""
    try:
        return apply(doc, cached(doc, refresh, no_net, log), log)
    except Exception as ex:   # noqa: BLE001
        log(f"osm: {doc.get('icao', '?')}: names left as exported ({type(ex).__name__}: {str(ex)[:100]})")
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("icaos", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--package", default=DEFAULT_PACKAGE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", help="write the single result here instead of in place")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-net", action="store_true")
    a = ap.parse_args()
    d = os.path.join(a.package, AIRPORTS_REL)
    icaos = [s.upper() for s in a.icaos]
    if a.all:
        icaos = sorted(fn[:-5] for fn in os.listdir(d) if fn.endswith(".json") and fn != "index.json")
    if not icaos:
        ap.error("give ICAO(s) or --all")
    sys.path.insert(0, HERE)
    import add_airport as A  # noqa: E402
    for icao in icaos:
        path = os.path.join(d, icao + ".json")
        doc = json.load(open(path, encoding="utf-8"))
        before = {e["i"]: e.get("label", "") for e in doc["paths"]}
        rep = relabel(doc, a.refresh, a.no_net)
        if not rep:
            continue
        if not rep["applied"]:
            print(f"  {icao}: not applied - {rep['why']} (OSM refs: {' '.join(rep['refs']) or 'none'})")
            continue
        diff = sum(1 for e in doc["paths"] if before[e["i"]] != e.get("label", ""))
        print(f"  {icao}: {diff} labels differ from the file")
        if a.dry_run:
            continue
        if a.out:
            with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(doc, fh, separators=(",", ":"))
            print(f"  wrote {a.out}")
        else:
            problem = A.validate(doc)
            if problem:
                print(f"  {icao}: NOT written, self-check failed: {problem}")
                continue
            A.write_airport(a.package, doc)
            A.write_index(a.package)
            print(f"  wrote {path}")


if __name__ == "__main__":
    main()
