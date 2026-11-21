"""Build the obstacle database from the FAA Digital Obstacle File (docs/plans/obstacles.md O1, §2, §9 R3 / R6 / R12).

    python -I tools/obstacles/build_obstacles.py [--zip DAILY_DOF_DAT.ZIP]

Without --zip it downloads https://aeronav.faa.gov/Obst_Data/DAILY_DOF_DAT.ZIP into tools/obstacles/cache/ (git-ignored).
One snapshot writes both outputs, each stamped with the same schema / currency / hash:
  - package/paulw-aircraft-sr20/html_ui/SR20Obstacles/index.json + tiles/<N33W118>.json (instrument display; the old
    tiles are removed first, a new cycle needs a sim restart: coui:// does not see new files mid-session)
  - tools/obstacles/cache/obstacles.npz + obstacles_meta.json (the FLTA server)
Then run make_layout.py. Run each 56-day cycle (the file itself is daily).
"""
import argparse
import datetime
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dof  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(HERE))
URL = 'https://aeronav.faa.gov/Obst_Data/DAILY_DOF_DAT.ZIP'
PKG = os.path.join(ROOT, 'package', 'paulw-aircraft-sr20', 'html_ui', 'SR20Obstacles')
CACHE = os.path.join(HERE, 'cache')


def read_dof(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.upper().endswith('DOF.DAT'))
        return io.TextIOWrapper(z.open(name), encoding='latin-1').read().splitlines()


def write_package(recs, meta):
    tiles = {}
    for r in recs:
        tiles.setdefault(r['tile'], []).append(r)
    if os.path.isdir(PKG):
        shutil.rmtree(PKG)
    os.makedirs(os.path.join(PKG, 'tiles'))
    sizes = 0
    for key, rs in sorted(tiles.items()):
        p = os.path.join(PKG, 'tiles', key + '.json')
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(dof.tile_payload(rs, key), f, separators=(',', ':'))
        sizes += os.path.getsize(p)
    index = dict(meta, tiles={k: len(v) for k, v in sorted(tiles.items())})
    with open(os.path.join(PKG, 'index.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(index, f, separators=(',', ':'))
    return len(tiles), sizes


def write_cache(recs, meta):
    os.makedirs(CACHE, exist_ok=True)
    np.savez_compressed(
        os.path.join(CACHE, 'obstacles.npz'),
        lat=np.array([r['lat_e5'] for r in recs], np.int32), lon=np.array([r['lon_e5'] for r in recs], np.int32),
        amsl=np.array([r['amsl'] for r in recs], np.int32), agl=np.array([r['agl'] for r in recs], np.int32),
        hacc=np.array([r['hacc_ft'] for r in recs], np.int32), vacc=np.array([r['vacc_ft'] for r in recs], np.int32),
        flags=np.array([r['flags'] for r in recs], np.int16), id=np.array([r['id'] for r in recs]))
    with open(os.path.join(CACHE, 'obstacles_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--zip', help='a downloaded DAILY_DOF_DAT.ZIP (default: download)')
    a = ap.parse_args()
    zp = a.zip
    if not zp:
        os.makedirs(CACHE, exist_ok=True)
        zp = os.path.join(CACHE, 'DAILY_DOF_DAT.ZIP')
        print('downloading', URL)
        urllib.request.urlretrieve(URL, zp)
    lines = read_dof(zp)
    recs, st = dof.snapshot(lines)
    meta = {'schema': dof.SCHEMA, 'currency': dof.currency(lines), 'hash': st['hash'], 'source': URL,
            'built': datetime.date.today().isoformat(), 'min_agl_ft': dof.MIN_AGL_FT, 'kept': st['kept'],
            'rejected': st['rejected'], 'filtered': st['filtered']}
    n, size = write_package(recs, meta)
    write_cache(recs, meta)
    print(f"currency {meta['currency']} hash {meta['hash']}: kept {st['kept']} in {n} tiles, {size / 1e6:.1f} MB")
    print('filtered', st['filtered'])
    print('rejected', st['rejected'])


if __name__ == '__main__':
    main()
