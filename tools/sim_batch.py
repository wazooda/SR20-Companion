"""Batched SimConnect reader: one data definition for many SimVars, fetched with one round trip per poll
(the Python-SimConnect Request path costs ~17 ms per variable per read; 34 variables = 1.7 Hz).

    from probe_electrical import connect_sdk
    from sim_batch import BatchReader
    br = BatchReader(sm, [("kias", "AIRSPEED INDICATED", "knots"), ...])
    br.poll(); time.sleep(0.05); br.latest -> {"kias": 101.2, ...}; br.count -> packets received

Uses RequestDataOnSimObjectType (the library's dispatch only routes SIMOBJECT_DATA_BYTYPE packets; the periodic
RequestDataOnSimObject packets are dropped unhandled) and stores the whole array (the library keeps element [0] only).
"""
import ctypes
from ctypes import POINTER, cast, c_double

from SimConnect.Enum import SIMCONNECT_DATATYPE, SIMCONNECT_UNUSED, SIMCONNECT_SIMOBJECT_TYPE


class BatchReader:
    def __init__(self, sm, cols):
        self.sm = sm
        self.cols = list(cols)
        self.latest = {}
        self.count = 0
        self.def_id = sm.new_def_id()
        self.req_id = sm.new_request_id()
        for _, name, unit in self.cols:
            err = sm.dll.AddToDataDefinition(sm.hSimConnect, self.def_id.value, name.encode(), unit.encode(),
                                             SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64, 0, SIMCONNECT_UNUSED)
            if not sm.IsHR(err, 0):
                raise RuntimeError(f"AddToDataDefinition failed for {name}")
        # chain the library's handler so its own single-variable requests keep working
        prev = sm.handle_simobject_event
        rid = self.req_id.value
        n = len(self.cols)
        keys = [c[0] for c in self.cols]

        def handler(obj_data):
            if obj_data.dwRequestID == rid:
                arr = cast(obj_data.dwData, POINTER(c_double * n)).contents
                self.latest = dict(zip(keys, arr))
                self.count += 1
            else:
                prev(obj_data)
        sm.handle_simobject_event = handler

    def poll(self):
        """Ask for one packet; it lands in .latest a few ms later (the dispatch thread runs every 2 ms)."""
        err = self.sm.dll.RequestDataOnSimObjectType(self.sm.hSimConnect, self.req_id.value, self.def_id.value, 0,
                                                     SIMCONNECT_SIMOBJECT_TYPE.SIMCONNECT_SIMOBJECT_TYPE_USER)
        return self.sm.IsHR(err, 0)

    def read(self, wait=0.03):
        """poll + wait; returns .latest."""
        self.poll()
        import time
        time.sleep(wait)
        return self.latest
