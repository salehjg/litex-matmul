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
from gemmflow.numeric import NumericError, parse_spec, resolve  # noqa: E402
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
            "PM not dividing the M bus word": (dict(parallelism_m=3), r"elements per M bus word = 16 must be a multiple of parallelism_m"),
            "tile_m not whole M bus words": (dict(memory_tile_size_m=100), r"memory_tile_size_m = 100 must be a multiple of the elements"),
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


def int_cfg(**kernel):
    """The best float config turned into another element format (name from the parameters)."""
    with open(os.path.join(ROOT, "configs", "gemm", "pe32x8_ta_t1024x512.json")) as f:
        d = json.load(f)
    d["kernel"].update(kernel)
    d["name"] = BuildCfg.from_dict(d).auto_name()
    return BuildCfg.from_dict(d)


class TestNumeric(unittest.TestCase):
    def test_formats(self):
        cases = [  # (family, bits, frac) -> (spec, c_type, container bytes, elements per word, short, tier)
            (("float", None, None), ("float32", "float", 4, 16, "f32", "T0")),
            (("int", 8, None), ("int8", "int8_t", 1, 64, "i8", "T1")),
            (("uint", 16, None), ("uint16", "uint16_t", 2, 32, "u16", "T1")),
            (("int", 64, None), ("int64", "int64_t", 8, 8, "i64", "T1")),
            (("fixed", 16, 8), ("fixed16.8", "ap_fixed<16, 8>", 2, 32, "fx16f8", "T2")),
            (("ufixed", 8, 8), ("ufixed8.8", "ap_ufixed<8, 0>", 1, 64, "ufx8f8", "T2")),
            (("int", 4, None), ("int4", "ap_int<4>", 1, 128, "i4", "T3")),
            (("uint", 2, None), ("uint2", "ap_uint<2>", 1, 256, "u2", "T3")),
        ]
        for args, (spec, ctype, cont, epw, short, tier) in cases:
            with self.subTest(args):
                nf = resolve(*args)
                self.assertEqual((nf.spec, nf.c_type, nf.container_bytes, nf.elems_per_word, nf.short, nf.tier),
                                 (spec, ctype, cont, epw, short, tier))
                self.assertEqual(parse_spec(spec), nf)

    def test_rejected(self):
        for args, msg in ((("int", 12), r"kernel to count elements by bits"), (("pow2", 3), r"DenseShift"),
                          (("int32_t",), r"use data_type 'int' with data_bits 32"), (("fixed", 16), r"needs frac_bits"),
                          (("fixed", 16, 16), r"frac_bits must be in 0..15"), (("int",), r"needs data_bits"),
                          (("int", 8, None, 32), r"wider accumulator"), (("float", 16), r"is 32 bits"),
                          (("int", 8, 3), r"only for fixed")):
            with self.subTest(args):
                with self.assertRaisesRegex(NumericError, msg):
                    resolve(*args)

    def test_logical_bus_width_for_packed_types(self):
        self.assertEqual(resolve("int", 4).logical_bytes(64), 128)
        self.assertEqual(resolve("int", 8).logical_bytes(64), 64)
        self.assertEqual(resolve("fixed", 16, 8).logical_bytes(64), 64)

    def test_columns(self):
        c = resolve("fixed", 16, 8).columns()
        self.assertEqual((c["dtype"], c["dtype_kind"], c["dtype_bits"], c["dtype_signed"], c["dtype_frac_bits"]),
                         ("fixed16.8", "fixed", 16, True, 8))
        self.assertEqual((c["a_dtype"], c["b_dtype"], c["c_dtype"], c["acc_bits"]), ("fixed16.8",) * 3 + (16,))
        self.assertIsNone(c["pow2_exp_bits"])


class TestIntConfigs(unittest.TestCase):
    def test_float_hash_ignores_unset_new_fields(self):
        cfg = load("pe32x8_ta_t1024x512.json")
        self.assertIsNone(cfg.kernel.data_bits)
        self.assertEqual(cfg.build_id(), GOLDEN_BUILD_IDS["pe32x8_ta_t1024x512.json"])
        self.assertIn("data_bits", cfg.to_dict()["kernel"])  # kept in the records

    def test_int8(self):
        cfg = int_cfg(data_type="int", data_bits=8)
        cfg.validate()
        self.assertEqual(cfg.name, "i8_pe32x8_t1024x512_200mhz_tA")
        self.assertEqual(cfg.size_multiples(), (64, 64, 64))
        self.assertAlmostEqual(cfg.peak_gops(), 102.4)
        h = cfg.render_config_h()
        self.assertIn("using Data_t = int8_t;", h)
        self.assertNotIn("${", h)
        self.assertNotIn("ap_fixed.h", h)

    def test_fixed_and_packed_render(self):
        h = int_cfg(data_type="fixed", data_bits=16, frac_bits=8).render_config_h()
        self.assertIn("#include <ap_fixed.h>", h)
        self.assertIn("using Data_t = ap_fixed<16, 8>;", h)
        self.assertRegex(h, r"\(2 != 64\)\)")  # sizeof(Data_t) vs bus width in the MM_CONVERT_A test
        cfg = int_cfg(data_type="int", data_bits=4, memory_tile_size_m=512)
        cfg.validate()
        h = cfg.render_config_h()
        self.assertIn("using Data_t = ap_int<4>;", h)
        self.assertIn("kMemoryWidthBytesM = 128;", h)
        self.assertEqual(cfg.size_multiples(), (128, 128, 128))

    def test_int_rules_in_elements(self):
        with self.assertRaisesRegex(ConfigError, r"elements per M bus word = 64 must be a multiple of parallelism_m = 128"):
            int_cfg(data_type="int", data_bits=8, parallelism_m=128).validate()
        with self.assertRaisesRegex(ConfigError, r"must be one of \['all', 'dsp', 'fabric'\]"):
            int_cfg(data_type="int", data_bits=8, mult_resource="fulldsp").validate()

    def test_op_bindings(self):
        from gemmflow.hls import HlsRun
        self.assertEqual(HlsRun(load("default.json")).op_bindings(), [])
        self.assertEqual(HlsRun(int_cfg(data_type="int", data_bits=8, mult_resource="fabric", add_resource="dsp"))
                         .op_bindings(), [("mul", "fabric"), ("add", "dsp")])
        self.assertEqual(HlsRun(int_cfg(mult_resource="fulldsp")).op_bindings(), [("fmul", "fulldsp")])


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
