"""Checks for fpl.py and the efb helper's pure parts:  python -I tools/efb/test_fpl.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tempfile  # noqa: E402
os.environ["SR20_LOGDIR"] = tempfile.mkdtemp()   # not the helper's real log
import fpl  # noqa: E402
import efb_server as S  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
n_ok = n_bad = 0


def ok(cond, what):
    global n_ok, n_bad
    if cond:
        n_ok += 1
    else:
        n_bad += 1
        print("FAIL", what)


def doc(points, wps=None, name="TEST"):
    wps = wps if wps is not None else points
    w = "".join(f"<waypoint><identifier>{i}</identifier><type>{t}</type><lat>{la}</lat><lon>{lo}</lon><altitude-ft>{a}</altitude-ft></waypoint>"
                for i, t, la, lo, a in wps)
    r = "".join(f"<route-point><waypoint-identifier>{i}</waypoint-identifier><waypoint-type>{t}</waypoint-type></route-point>"
                for i, t, *_ in points)
    return ('<?xml version="1.0" encoding="utf-8"?><flight-plan xmlns="http://www8.garmin.com/xmlschemas/FlightPlan/v1">'
            f"<flight-data><altitude-ft>6500</altitude-ft></flight-data><waypoint-table>{w}</waypoint-table>"
            f"<route><route-name>{name}</route-name>{r}</route></flight-plan>")


# ForeFlight's own files
a = fpl.load(os.path.join(FIX, "foreflight_ksna_klgb.fpl"))
ok([p["ident"] for p in a["points"]] == ["KSNA", "SLI", "KLGB"], "UTF-16 plain route: " + str(a["points"]))
ok(a["cruise_ft"] == 8000 and a["tail"] == "N520SR" and a["name"] == "KSNA TO KLGB", "UTF-16 header fields")
ok(a["points"][1]["type"] == "VOR" and abs(a["points"][1]["lat"] - 33.7833) < 1e-6, "SLI typed and positioned")
b = fpl.load(os.path.join(FIX, "foreflight_ksna_kcno_rnav26r_pdz.fpl"))
ok([p["ident"] for p in b["points"]] == ["KSNA", "SLI", "PDZ", "KCNO"], "approach route reduced: " + str([p["ident"] for p in b["points"]]))
ok(b["dropped"]["hold"] == 22 and b["dropped"]["procedure"] == ["CAZBY", "LINDN", "DEWYE"] and b["dropped"]["runway"] == ["RW26R"], "dropped: " + str(b["dropped"]))

# encodings
x = doc([("KSNA", "AIRPORT", 33.67, -117.86, ""), ("KLGB", "AIRPORT", 33.81, -118.15, "")])
for enc, data in (("utf-8", x.encode()), ("utf-8-sig", b"\xef\xbb\xbf" + x.encode()), ("utf-16", x.encode("utf-16"))):
    p = fpl.parse(data)
    ok([q["ident"] for q in p["points"]] == ["KSNA", "KLGB"] and p["cruise_ft"] == 6500, "encoding " + enc)


def raises(data, what):
    try:
        fpl.parse(data)
        ok(False, what + " accepted")
    except fpl.FplError:
        ok(True, what)


raises(b"<!DOCTYPE x [<!ENTITY a 'b'>]>" + x.encode(), "DTD rejected")
raises(b" " * (fpl.MAX_BYTES + 1), "oversized rejected")
raises(b"not xml at all", "garbage rejected")
raises(b"<?xml version='1.0'?><something/>", "wrong root rejected")
raises(doc([("KSNA", "AIRPORT", 1, 1, ""), ("GHOST", "", 1, 1, "")], wps=[("KSNA", "AIRPORT", 1, 1, "")]).encode(), "dangling route point rejected")
raises(doc([("A", "", 1, 1, "")] * (fpl.MAX_POINTS + 1), wps=[("A", "", 1, 1, "")]).encode(), "too many route points rejected")

# reduce rules
p = fpl.parse(doc([("KSNA", "AIRPORT", 33.6, -117.8, ""), ("SLI", "VOR", 33.7, -118.0, ""), ("SLI", "VOR", 33.7, -118.0, ""),
                   ("MYPT", "USER WAYPOINT", 33.9, -117.5, ""), ("FIXA", "", 33.95, -117.4, "5000"), ("H-01", "", 34, -117.3, ""),
                   ("RW26R", "", 33.97, -117.63, "1030"), ("KCNO", "AIRPORT", 33.97, -117.63, "")]).encode())
pts, dr = fpl.reduce(p)
ok([q["ident"] for q in pts] == ["KSNA", "SLI", "MYPT", "KCNO"], "reduce: " + str([q["ident"] for q in pts]))
ok(pts[2]["type"] == "USER WAYPOINT" and pts[2]["lat"] == 33.9, "user waypoint kept with its position")
ok(dr == {"hold": 1, "procedure": ["FIXA"], "runway": ["RW26R"]}, "reduce dropped " + str(dr))
p = fpl.parse(doc([("BAD", "", 95, 200, "")]).encode())
ok(p["points"][0]["lat"] is None and p["points"][0]["lon"] is None, "out-of-range coordinates become unknown")

# helper: XGPS / XATT lines and the sim's sign conventions
v = {"lat": 33.6757, "lon": -117.8682, "alt": 1500.0, "track": 359.6, "gs": 120.0, "hdg": 2.0, "pitch": -3.0, "bank": 10.0}
g, t = S.xgps(v), S.xatt(v)
ok(g == "XGPSSR20 Sim,-117.86820,33.67570,457.2,359.6,61.7", "XGPS line: " + g)
ok(t == "XATTSR20 Sim,2.0,3.0,-10.0", "XATT: pitch up / right bank positive (sim's are the other way): " + t)

# helper: Downloads watcher picks only new ForeFlight files
import tempfile, time  # noqa: E401,E402
d = tempfile.mkdtemp()
old = os.path.join(d, "ForeFlight.fpl")
open(old, "wb").write(x.encode())
os.utime(old, (time.time() - 3600, time.time() - 3600))
w = S.FplWatcher(d, since=time.time() - 600)
ok(w.scan() == [], "a file from an hour ago is not offered")
new = os.path.join(d, "ForeFlight (3).fpl")
open(new, "wb").write(x.encode())
ok(w.scan() == [], "a file written this instant waits (it may still be arriving)")
os.utime(new, (time.time() - 2, time.time() - 2))
open(os.path.join(d, "other.txt"), "wb").write(b"x")
got = w.scan()
ok(got == [new], "a new ForeFlight file is offered once: " + str(got))
ok(w.scan() == [], "and not again")

# helper: pending slot (seq "<boot>-<n>", unique across restarts)
q = S.Pending()
ok(q.get() == {"seq": "", "plan": None}, "empty pending")
s1 = q.put({"name": "A", "points": []}, "f1")
s2 = q.put({"name": "B", "points": []}, "f2")
g1 = q.get()
ok(g1["seq"] == s2 and s2 == q.boot + "-2" and g1["plan"]["name"] == "B", "the newest file replaces an unanswered one")
ok(not q.ack(s1, "ignored") and q.get()["plan"] is not None, "an ack for an old seq does nothing")
ok(q.ack(s2, "activated") and q.get()["plan"] is None and q.get()["seq"] == s2, "ack clears it, seq kept")
ok(S.Pending().boot != q.boot, "a restarted helper has a new boot id, so its first seq is new to the MFD")
ok(q.put_error({"name": "bad", "error": "x"}, "f3") is not None, "an unreadable file is offered when nothing waits")
q.put({"name": "C", "points": []}, "f4")
ok(q.put_error({"name": "bad", "error": "x"}, "f5") is None and q.get()["plan"]["name"] == "C",
   "an unreadable file never displaces a good unanswered plan")

# fpl: load_bytes and broken UTF-16
try:
    fpl.decode(b"\xff\xfe" + b"\x00\xd8")   # a lone surrogate
    ok(False, "malformed UTF-16 accepted")
except fpl.FplError:
    ok(True, "malformed UTF-16 -> FplError")
try:
    fpl.load_bytes(doc([("KSNA", "AIRPORT", 33.6, -117.8, "")]).encode())
    ok(False, "one-point route accepted")
except fpl.FplError:
    ok(True, "a route of fewer than two points is refused")

print(f"{n_ok} ok, {n_bad} failed")
sys.exit(1 if n_bad else 0)
