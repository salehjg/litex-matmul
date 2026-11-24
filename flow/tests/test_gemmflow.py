#
# test_gemmflow.py - unit tests for the Python flow (no tools, no card): config rules, names and hashes, size rules,
# result store, campaign expansion, build reports.
#   .venv/bin/python -m unittest discover -s flow/tests -v
#
# The golden build ids pin the hashing: if one changes, every existing build dir stops being reused. Change them
# only on purpose (new config field, new hashing), and expect full rebuilds.
#

import copy
import csv
import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "flow"))

from gemmflow.config import BuildCfg, ConfigError          # noqa: E402
from gemmflow.results import ResultStore, flatten           # noqa: E402
from gemmflow.steps import write_build_reports              # noqa: E402

GOLDEN_BUILD_IDS = {
    "default.json":                     "f32_pe8x8_t128x128-4e52e7f5",
    "pe32x8_ta_t512.json":              "f32_pe32x8_t512x512_200mhz_tA-dc42aa19",
    "pe32x8_ta_t1024x512.json":         "f32_pe32x8_t1024x512_200mhz_tA-a88777ec",
    "pe32x8_ta_t1024x512_225mhz.json":  "f32_pe32x8_t1024x512_225mhz_tA_aggressive-db5adf1c",
}


def load(name):
    return BuildCfg.load(os.path.join(ROOT, "configs", "gemm", name))


def cfg_from(**kernel):
    """default.json with kernel fields replaced (dotted dicts merged one level)."""
    with open(os.path.join(ROOT, "configs", "gemm", "default.json")) as f:
        d = json.load(f)
    for k, v in kernel.items():
        if isinstance(v, dict):
            d["kernel"][k].update(v)
        else:
            d["kernel"][k] = v
    return BuildCfg.from_dict(d)


class TestConfigs(unittest.TestCase):
    def test_golden_build_ids(self):
        for name, bid in GOLDEN_BUILD_IDS.items():
            with self.subTest(config=name):
                self.assertEqual(load(name).build_id(), bid)

    def test_every_config_is_pinned(self):
        have = {f for f in os.listdir(os.path.join(ROOT, "configs", "gemm")) if f.endswith(".json")}
        self.assertEqual(have, set(GOLDEN_BUILD_IDS), "add new configs to GOLDEN_BUILD_IDS")

    def test_campaign_configs_named_like_dse_points(self):
        # gemm_build.py reuses a DSE build only if the config's name is the auto name.
        for name in GOLDEN_BUILD_IDS:
            if name != "default.json":
                cfg = load(name)
                self.assertEqual(cfg.name, cfg.auto_name(), name)

    def test_hash_ignores_nothing_but_is_stable(self):
        a, b = load("default.json"), load("default.json")
        self.assertEqual(a.config_hash(), b.config_hash())
        b.vivado.preset = "explore"
        self.assertNotEqual(a.config_hash(), b.config_hash())
        self.assertEqual(a.hls_hash(), b.hls_hash(), "Vivado knobs must not change the HLS hash")
        b.clocks.accel_mhz = 225
        self.assertNotEqual(a.hls_hash(), b.hls_hash())


class TestNames(unittest.TestCase):
    def test_auto_name(self):
        cfg = load("default.json")
        self.assertEqual(cfg.auto_name(), "f32_pe8x8_t128x128_200mhz")
        cfg.kernel.transposed_a = True
        cfg.vivado.preset = "aggressive"
        cfg.clocks.accel_mhz = 225
        self.assertEqual(cfg.auto_name(), "f32_pe8x8_t128x128_225mhz_tA_aggressive")
        cfg.memory_map.a, cfg.memory_map.b, cfg.memory_map.c = "ch1", "ch0", "ch0"
        self.assertTrue(cfg.auto_name().endswith("_m100"))


class TestRules(unittest.TestCase):
    def test_default_is_valid(self):
        self.assertEqual(load("default.json").validate(), [])

    def test_rejected(self):
        cases = {  # what: (kernel fields, expected message)
            "PM not dividing the M bus word": (dict(parallelism_m=3), r"/ sizeof\) = 16 must be a multiple of parallelism_m"),
            "tile_m not whole M bus words": (dict(memory_tile_size_m=100), r"memory_tile_size_m = 100 must be a multiple of bus"),
            "tile_n not a multiple of PN": (dict(memory_tile_size_n=132), r"memory_tile_size_n = 132 must be a multiple of par"),
            "tile_m/PM < tile_n/PN": (dict(memory_tile_size_n=256, memory_tile_size_m=128), r"double buffering of A"),
            "transposed A, tile_n not whole N bus words":
                (dict(transposed_a=True, parallelism_n=4, memory_tile_size_n=36), r"transposed A: memory_tile_size_n"),
            "bus width not a multiple of the element": (dict(bus_width_bytes={"k": 62}), r"bus_width_bytes.k=62"),
            "unknown data type": (dict(data_type="double"), r"data_type 'double'"),
        }
        for what, (kernel, message) in cases.items():
            with self.subTest(what):
                with self.assertRaisesRegex(ConfigError, message):
                    cfg_from(**kernel).validate()

    def test_rejected_outside_kernel(self):
        for path, value in (("memory_map.a", "ch2"), ("vivado.preset", "fast"), ("hls.export_flow", "bit")):
            with self.subTest(path):
                cfg = load("default.json")
                section, key = path.split(".")
                setattr(getattr(cfg, section), key, value)
                with self.assertRaises(ConfigError):
                    cfg.validate()

    def test_unknown_key(self):
        with self.assertRaises(ConfigError):
            BuildCfg.from_dict({"kernel": {"parallelism_x": 4}})

    def test_size_multiples(self):
        self.assertEqual(load("default.json").size_multiples(), (1, 16, 16))
        self.assertEqual(load("pe32x8_ta_t1024x512.json").size_multiples(), (16, 16, 16))
        self.assertEqual(cfg_from(transpose_width_bytes=128).size_multiples(), (1, 32, 16))

    def test_peak(self):
        self.assertAlmostEqual(load("pe32x8_ta_t1024x512_225mhz.json").peak_gflops(), 115.2)

    def test_vivado_presets(self):
        cfg = load("pe32x8_ta_t1024x512_225mhz.json")
        v = cfg.resolved_vivado()
        self.assertEqual(v["place_directive"], "ExtraTimingOpt")
        self.assertEqual(v["post_route_phys_opt"], "AggressiveExplore")
        self.assertLessEqual(v["max_threads"], 8)
        cfg.vivado.place_directive = "Explore"
        cfg.vivado.max_threads = 4
        v = cfg.resolved_vivado()
        self.assertEqual((v["place_directive"], v["max_threads"]), ("Explore", 4))

    def test_config_h_renders_completely(self):
        text = load("pe32x8_ta_t1024x512.json").render_config_h()
        self.assertNotIn("${", text)
        self.assertIn("kInnerTileSizeN = 32;", text)


class TestResults(unittest.TestCase):
    def test_flatten(self):
        self.assertEqual(flatten({"a": 1, "b": {"c": 2, "d": {"e": 3}}}), {"a": 1, "b.c": 2, "b.d.e": 3})

    def test_store_csv_union_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            st = ResultStore(os.path.join(d, "runs.jsonl"))
            st.append({"zeta": 1, "build_id": "x", "perf": {"a": 2}})
            st.append({"build_id": "y", "extra": None})
            st.write_csv()
            self.assertEqual([r["build_id"] for r in st.rows()], ["x", "y"])
            with open(os.path.join(d, "runs.csv")) as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(list(rows[0])[:2], ["schema_version", "build_id"])
            self.assertEqual(rows[1]["perf.a"], "")
            self.assertEqual(rows[0]["perf.a"], "2")


class TestDse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("gemm_dse", os.path.join(ROOT, "scripts", "gemm_dse.py"))
        cls.dse = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.dse)

    def test_expand_names_points_after_parameters(self):
        with open(os.path.join(ROOT, "configs", "dse", "memory_maps.json")) as f:
            camp = json.load(f)
        names = [d["name"] for d in self.dse.expand(camp)]
        self.assertEqual(names, ["f32_pe8x8_t128x128_200mhz_m000", "f32_pe8x8_t128x128_200mhz_m100"])

    def test_expand_shares_builds_across_campaigns(self):
        # The 200 MHz point of clock_pe32_ta must be the build of configs/gemm/pe32x8_ta_t1024x512.json.
        with open(os.path.join(ROOT, "configs", "dse", "clock_pe32_ta.json")) as f:
            camp = json.load(f)
        ids = [BuildCfg.from_dict(d).build_id() for d in self.dse.expand(camp)]
        self.assertEqual(ids[0], GOLDEN_BUILD_IDS["pe32x8_ta_t1024x512.json"])
        self.assertEqual(len(set(ids)), 3)

    def test_sizes_of(self):
        self.assertEqual(self.dse.sizes_of({"triples": [[1, 2, 3]]}), [(1, 2, 3)])
        self.assertEqual(len(self.dse.sizes_of({"n": [1, 2], "k": [3], "m": [4, 5, 6]})), 6)

    def test_every_campaign_expands(self):
        folder = os.path.join(ROOT, "configs", "dse")
        for fn in sorted(os.listdir(folder)):
            with self.subTest(campaign=fn):
                with open(os.path.join(folder, fn)) as f:
                    camp = json.load(f)
                points = self.dse.expand(copy.deepcopy(camp))
                self.assertGreater(len(points), 0)


class TestVivadoReport(unittest.TestCase):
    HIER = """\
| Instance | Module | Total LUTs | Logic LUTs | LUTRAMs | SRLs | FFs | RAMB36 | RAMB18 | DSP Blocks |
+----------+--------+------------+------------+---------+------+-----+--------+--------+------------+
| dupcb | (top) | 151282 | 136505 | 3734 | 11043 | 235872 | 580 | 149 | 1320 |
|   gemm_kernel_ip | gemm_kernel_ip | 122535 | 111556 | 6 | 10973 | 206873 | 509 | 69 | 1320 |
"""

    def test_kernel_columns_by_name(self):
        from gemmflow.reports import VivadoReport
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "dupcb_utilization_hierarchical_place.rpt"), "w") as f:
                f.write(self.HIER)
            k = VivadoReport(d).kernel_utilization()
        self.assertEqual((k["kernel_lut"], k["kernel_lutram"], k["kernel_srl"], k["kernel_ff"]),
                         (122535, 6, 10973, 206873))
        self.assertEqual((k["kernel_ramb36"], k["kernel_ramb18"], k["kernel_dsp"]), (509, 69, 1320))


class TestBuildReports(unittest.TestCase):
    def test_manifest_only_when_a_step_ran(self):
        cfg = load("default.json")
        with tempfile.TemporaryDirectory() as d:
            write_build_reports(cfg, d, {"hls": {"seconds": 0.0, "skipped": True}})
            self.assertTrue(os.path.exists(os.path.join(d, "reports", "summary.json")))
            self.assertFalse(os.path.exists(os.path.join(d, "manifest.json")))
            s = write_build_reports(cfg, d, {"soc": {"seconds": 12.0, "skipped": False}})
            self.assertTrue(os.path.exists(os.path.join(d, "manifest.json")))
            self.assertEqual(s["build_id"], cfg.build_id())


if __name__ == "__main__":
    unittest.main()
