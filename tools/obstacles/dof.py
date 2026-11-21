"""FAA Digital Obstacle File (DOF) parser and the shared obstacle snapshot (docs/plans/obstacles.md §2, §9 R3 / R6 / R12).

Fixed-width DOF.DAT records (FAA DOF / DDOF readme): 0-based slices below. One normalized snapshot feeds both the package
display tiles and the FLTA server cache, so they always carry the same schema, currency and dataset hash.
"""
import hashlib
import math
import re

SCHEMA = 1
MIN_AGL_FT = 200            # PG Appendix B: obstacles "200 feet and higher" (txt 29454); PG p.361
SKIP_TYPES = {'CATENARY'}   # wire span markers: wires are out of scope (plan §6)

# horizontal accuracy code -> ft (DOF readme); 9 / blank unknown -> the plan's ±1000 ft (§9 R3), never zero
H_ACC_FT = {'1': 20, '2': 50, '3': 100, '4': 250, '5': 500, '6': 1000, '7': 3038, '8': 6076}
H_UNKNOWN_FT = 1000
# vertical accuracy code -> ft; I / blank unknown -> 125 ft (between E and F; added to the top for the FLTA only)
V_ACC_FT = {'A': 3, 'B': 10, 'C': 20, 'D': 50, 'E': 125, 'F': 250, 'G': 500, 'H': 1000}
V_UNKNOWN_FT = 125
LIT = set('RDHMSFCWL')     # N none, U unknown -> unlit symbol

F_LIT, F_TURBINE, F_UNVERIFIED, F_GROUP = 1, 2, 4, 8

_OAS = re.compile(r'^[0-9A-Z]{2}-[0-9A-Z]{6}$')


def _dms(deg, mins, sec, hemi, neg):
    v = int(deg) + int(mins) / 60 + float(sec) / 3600
    return -v if hemi == neg else v


def parse_line(line):
    """One DOF record -> dict, or (None, reason). Header and malformed lines are rejected with a reason."""
    l = line.rstrip('\r\n')
    if len(l) < 100:
        return None, 'short'
    oas = l[0:9]
    if not _OAS.match(oas):
        return None, 'not a record'
    try:
        lat_h, lon_h = l[46], l[60]
        if lat_h not in 'NS' or lon_h not in 'EW':
            return None, 'bad hemisphere'
        lat = _dms(l[35:37], l[38:40], l[41:46], lat_h, 'S')
        lon = _dms(l[48:51], l[52:54], l[55:60], lon_h, 'W')
        agl = int(l[83:88])
        amsl = int(l[89:94])
    except ValueError:
        return None, 'bad number'
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, 'bad coordinates'
    typ = l[62:80].strip()
    hc, vc = l[97], l[99]
    q = l[81].strip()
    return {
        'id': oas, 'verified': l[10] == 'O', 'country': l[12:14].strip(), 'lat': lat, 'lon': lon, 'type': typ,
        'qty': int(q) if q.isdigit() else 1, 'agl': agl, 'amsl': amsl, 'light': l[95],
        'hacc_ft': H_ACC_FT.get(hc, H_UNKNOWN_FT), 'vacc_ft': V_ACC_FT.get(vc, V_UNKNOWN_FT), 'hcode': hc, 'vcode': vc,
    }, None


def keep(rec):
    """None if the obstacle belongs in the snapshot, else the filter reason."""
    if rec['type'] in SKIP_TYPES:
        return 'catenary'
    if rec['agl'] < MIN_AGL_FT:
        return 'below 200 ft AGL'
    return None


def flags(rec):
    f = 0
    if rec['light'] in LIT:
        f |= F_LIT
    if rec['type'] == 'WINDMILL':
        f |= F_TURBINE
    if not rec['verified']:
        f |= F_UNVERIFIED
    if rec['qty'] > 1:
        f |= F_GROUP
    return f


def e5(v):
    """Degrees -> integer 1e-5 degrees (about 1.1 m); tile membership is computed from this integer."""
    return int(round(v * 1e5))


def tile_key(lat_e5, lon_e5):
    """1 x 1 degree tile by signed floor (Python // floors toward -inf like Math.floor in JS): N33W118 = 33..34N, 118..117W."""
    la, lo = lat_e5 // 100000, lon_e5 // 100000
    return '%s%02d%s%03d' % ('N' if la >= 0 else 'S', abs(la), 'E' if lo >= 0 else 'W', abs(lo))


def snapshot(lines):
    """All DOF lines -> (records sorted by id, stats). Records carry lat_e5 / lon_e5 / flags / tile."""
    out, rejected, filtered, seen = [], {}, {}, set()
    for line in lines:
        rec, why = parse_line(line)
        if rec is None:
            if line.strip():    # header lines land in 'not a record' / 'short' too: 4 per file
                rejected[why] = rejected.get(why, 0) + 1
            continue
        why = keep(rec)
        if why:
            filtered[why] = filtered.get(why, 0) + 1
            continue
        if rec['id'] in seen:
            rejected['duplicate id'] = rejected.get('duplicate id', 0) + 1
            continue
        seen.add(rec['id'])
        rec['lat_e5'], rec['lon_e5'] = e5(rec['lat']), e5(rec['lon'])
        rec['flags'] = flags(rec)
        rec['tile'] = tile_key(rec['lat_e5'], rec['lon_e5'])
        out.append(rec)
    out.sort(key=lambda r: r['id'])
    h = hashlib.sha256()
    for r in out:
        h.update(('%s %d %d %d %d %d %d %d\n' % (r['id'], r['lat_e5'], r['lon_e5'], r['amsl'], r['agl'], r['flags'],
                                                r['hacc_ft'], r['vacc_ft'])).encode())
    return out, {'kept': len(out), 'rejected': rejected, 'filtered': filtered, 'hash': h.hexdigest()[:16]}


def currency(lines):
    for line in lines[:5]:
        m = re.search(r'CURRENCY DATE\s*=\s*(\S+)', line)
        if m:
            return m.group(1)
    return None


def tile_payload(recs, key):
    """Compact columnar tile for the instruments: positions relative to the tile's south-west corner in 1e-5 deg."""
    la0 = (recs[0]['lat_e5'] // 100000) * 100000
    lo0 = (recs[0]['lon_e5'] // 100000) * 100000
    return {
        'schema': SCHEMA, 'tile': key, 'n': len(recs),
        'lat': [r['lat_e5'] - la0 for r in recs], 'lon': [r['lon_e5'] - lo0 for r in recs],
        'amsl': [r['amsl'] for r in recs], 'agl': [r['agl'] for r in recs], 'f': [r['flags'] for r in recs],
        'id': [r['id'] for r in recs],
    }


def tile_origin(key):
    la = int(key[1:3]) * (1 if key[0] == 'N' else -1)
    lo = int(key[4:7]) * (1 if key[3] == 'E' else -1)
    return la, lo


def dist_nm(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 2 * 3440.065 * math.asin(min(1, math.sqrt(a)))
