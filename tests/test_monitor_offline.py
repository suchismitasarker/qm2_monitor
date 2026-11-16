"""Offline tests of the QM2 monitor (no network): beam-status rules and
the web page. Run from the qm2monitor_apptainer folder or inside the image:
    python -m unittest -v tests/test_monitor_offline.py
"""
import re
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

for d in (Path(__file__).resolve().parents[1] / "ml",   # image / this folder
          Path(__file__).resolve().parents[3] / "ml"):  # repository ml/
    if (d / "beamline_monitor.py").exists():
        sys.path.insert(0, str(d))
        break
import beamline_monitor as bm  # noqa: E402
import monitor_api as m  # noqa: E402

T = pd.date_range("2026-11-15 10:00", periods=60, freq="30s")
END = T[-1].to_pydatetime()


def S(v):
    return pd.Series(np.broadcast_to(v, len(T)).astype(float), index=T)


def state(i1, i2, d, ring=140.0):
    return bm.experiment_state(S(i1), S(i2), S(d), S(ring), END)[0]


class BeamStatus(unittest.TestCase):
    def test_experiment_running(self):
        self.assertEqual(state(1.66, 0.9, 0.78),
                         "Hutch Closed - Experiment Running")

    def test_beam_in_hutch_not_running(self):      # ic1 16,649 ic2 83 d 18
        self.assertEqual(state(1.6649, 0.0083, 0.0018),
                         "Hutch Closed - Beam in Hutch, Experiment NOT "
                         "Running")

    def test_hutch_open(self):                      # ic1 60
        self.assertEqual(state(0.006, 0.0083, 0.0018), "Hutch Open - No Beam")

    def test_no_cesr(self):
        self.assertEqual(state(0, 0, 0, ring=0.0), "No Beam in CESR")

    def test_beam_motion(self):                     # 60 <-> 19,000 counts
        osc = np.where((np.arange(len(T)) // 6) % 2 == 0, 1.9, 0.006)
        self.assertEqual(state(osc, osc * 0.5, osc * 0.4),
                         "Check Beam Motion")

    def test_single_shutter_change_is_not_motion(self):
        one = np.where(np.arange(len(T)) < 40, 0.006, 1.66)
        self.assertNotEqual(state(one, 0.008, 0.002), "Check Beam Motion")

    def test_counts_per_0p1s(self):
        txt = bm.counters_now(S(1.6649), S(0.0083), S(0.0018), S(140.0), END)
        self.assertIn("ic1 16,649", txt)
        self.assertIn("diode 18", txt)


class FluxTable(unittest.TestCase):
    """Light flux check: ml/flux_table.json (no twin, no yaml)."""
    def setUp(self):
        self.t = bm._flux_table()
        if self.t is None:
            self.skipTest("no flux_table.json next to beamline_monitor.py")

    def test_rows_on_grid(self):
        r = np.array(self.t["rows"])
        e, model, per_v = r[len(r) // 2]
        m, pv = bm.table_flux(e, 2e-8)
        self.assertAlmostEqual(m / model, 1, places=9)
        self.assertAlmostEqual(pv / (per_v * 2e-8), 1, places=9)

    def test_known_values(self):         # flux_current, 2026-3, 143 mA
        for e, ref in ((20, 3.832e12), (26, 2.625e12), (45, 9.666e11)):
            self.assertAlmostEqual(bm.table_flux(e, 1.0)[0] / ref, 1, 2)

    def test_outside_table(self):
        self.assertIsNone(bm.table_flux(500.0, 2e-8))

    def test_gain_without_yaml(self):
        saved = sys.modules.get("yaml")
        sys.modules["yaml"] = None       # yaml is not in the image
        try:
            g, src = bm.table_gain(45.0)
        finally:
            if saved is not None:
                sys.modules["yaml"] = saved
            else:
                del sys.modules["yaml"]
        self.assertEqual(g, 2e-8)
        self.assertIn("45 keV", src)


class Page(unittest.TestCase):
    def render(self, value, overall="OK"):
        m.STATE.update(checks=[
            {"check": "experiment status", "value": value, "status": "INFO"},
            {"check": "counters now", "status": "INFO",
             "value": "ic1 16,649 · ic2 83 · diode 18 counts per 0.1 s"}],
            overall=overall, updated="2026-11-15T12:00", window=["a", "b"],
            error=None)
        return m.page()

    def test_banner_colours(self):
        for value, cls in (("Hutch Closed - Experiment Running (x)", "x-run"),
                           ("Hutch Closed - Beam in Hutch, Experiment NOT "
                            "Running (x)", "x-closed"),
                           ("Hutch Open - No Beam (x)", "x-open"),
                           ("No Beam in CESR (x)", "x-nobeam"),
                           ("Check Beam Motion (x)", "x-motion")):
            h = self.render(value)
            self.assertEqual(re.search(r"class='exp (x-\w+)'", h).group(1),
                             cls, value)

    def test_status_first(self):
        h = self.render("Hutch Open - No Beam (x)")
        self.assertLess(h.index("BEAM STATUS"), h.index("BEAMLINE HEALTH"))

    def test_starting_page(self):
        m.STATE.update(checks=[], overall="starting")
        self.assertIn("Reading the CHESS Signals archive", m.page())


if __name__ == "__main__":
    unittest.main()
