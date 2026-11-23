#
# config.py - the GEMM build configuration (JSON) as dataclasses: load with defaults, reject unknown keys, apply the
# Vivado presets, check the gemm_hls rules before any tool runs, render kernels/gemm/include/Config.h.in, and compute
# stable hashes for traceability. Schema and rules: docs/notes1/gemm-hls-plan.md sections 4.1, 4.4, 5, 6.
#

import dataclasses
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field, fields
from typing import Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KERNEL_DIR = os.path.join(ROOT, "kernels", "gemm")

# Element types the kernel supports: size in bytes, DSP48E1 per multiply-accumulate (rough, for the budget hint).
DATA_TYPES = {
    "float":    {"bytes": 4, "dsp_per_mac": 5},  # fmul 3 + fadd 2 (full DSP)
    "half":     {"bytes": 2, "dsp_per_mac": 3},
    "int32_t":  {"bytes": 4, "dsp_per_mac": 4},
    "int16_t":  {"bytes": 2, "dsp_per_mac": 1},
    "uint8_t":  {"bytes": 1, "dsp_per_mac": 1},
}
DEVICE = {"part": "xc7k480tiffg1156-2L", "dsp": 1920, "bram36": 955}

VIVADO_PRESETS = {
    "default":    dict(synth_directive="default", opt_directive="default", place_directive="default",
                       post_place_phys_opt=None, route_directive="default", post_route_phys_opt="default"),
    "explore":    dict(synth_directive="default", opt_directive="Explore", place_directive="Explore",
                       post_place_phys_opt="Explore", route_directive="Explore", post_route_phys_opt="Explore"),
    "aggressive": dict(synth_directive="PerformanceOptimized", opt_directive="ExploreWithRemap",
                       place_directive="ExtraTimingOpt", post_place_phys_opt="AggressiveExplore",
                       route_directive="AggressiveExplore", post_route_phys_opt="AggressiveExplore"),
}


class ConfigError(ValueError):
    pass


# Dataclasses ------------------------------------------------------------------------------------------------------

@dataclass
class BusWidths:
    n: int = 64
    k: int = 64
    m: int = 64


@dataclass
class FifoDepths:
    pipe:     int = 4               # PE-to-PE pipes (upstream kPipeDepth)
    a_split:  Optional[int] = None  # None = upstream default 2 * memory_tile_size_n
    b_memory: Optional[int] = None  # None = upstream default 2 * memory tile M in bus words
    c_memory: Optional[int] = None  # None = upstream default 2 * memory tile M in bus words


@dataclass
class MAxi:
    max_burst_len:   int = 64
    num_outstanding: int = 16
    latency:         int = 64


@dataclass
class KernelCfg:
    data_type:             str = "float"
    parallelism_n:         int = 8
    parallelism_m:         int = 8
    memory_tile_size_n:    int = 128
    memory_tile_size_m:    int = 128
    bus_width_bytes:       BusWidths = field(default_factory=BusWidths)
    transpose_width_bytes: int = 64
    transposed_a:          bool = False
    fifo_depths:           FifoDepths = field(default_factory=FifoDepths)
    add_resource:          Optional[str] = None
    mult_resource:         Optional[str] = None
    m_axi:                 MAxi = field(default_factory=MAxi)


@dataclass
class MemoryMap:
    a: str = "ch0"
    b: str = "ch1"
    c: str = "ch1"


@dataclass
class ClockCfg:
    accel_mhz:           float = 200.0
    hls_uncertainty_pct: float = 27.0


@dataclass
class HlsCfg:
    run_csim:    bool = False
    export_flow: str = "none"  # none | syn | impl (export_design -flow)


@dataclass
class VivadoCfg:
    preset:              str = "default"
    max_threads:         object = "auto"  # "auto" or an int
    synth_directive:     Optional[str] = None
    opt_directive:       Optional[str] = None
    place_directive:     Optional[str] = None
    post_place_phys_opt: Optional[str] = None
    route_directive:     Optional[str] = None
    post_route_phys_opt: Optional[str] = None
    floorplan:           Optional[str] = None


@dataclass
class SocCfg:
    sys_clk_mhz: float = 125.0


@dataclass
class BuildCfg:
    name:       str = "default"
    kernel:     KernelCfg = field(default_factory=KernelCfg)
    memory_map: MemoryMap = field(default_factory=MemoryMap)
    clocks:     ClockCfg = field(default_factory=ClockCfg)
    hls:        HlsCfg = field(default_factory=HlsCfg)
    vivado:     VivadoCfg = field(default_factory=VivadoCfg)
    soc:        SocCfg = field(default_factory=SocCfg)

    # Loading ------------------------------------------------------------------------------------------------------

    @classmethod
    def from_dict(cls, d):
        return _from_dict(cls, d, "config")

    @classmethod
    def load(cls, path):
        with open(path) as f:
            cfg = cls.from_dict(json.load(f))
        cfg.validate()
        return cfg

    def to_dict(self):
        return dataclasses.asdict(self)

    # Derived values -----------------------------------------------------------------------------------------------

    @property
    def elem_bytes(self):
        return DATA_TYPES[self.kernel.data_type]["bytes"]

    def resolved_vivado(self):
        """Directives after the preset, explicit fields win. max_threads resolved (Vivado caps it at 8 on Linux)."""
        v = dict(VIVADO_PRESETS[self.vivado.preset])
        for k in v:
            if getattr(self.vivado, k) is not None:
                v[k] = getattr(self.vivado, k)
        mt = self.vivado.max_threads
        v["max_threads"] = min(8, os.cpu_count() or 1) if mt == "auto" else int(mt)
        v["floorplan"] = self.vivado.floorplan
        return v

    def size_multiples(self):
        """(N, K, M) multiples the run-time sizes need: the kernel reads whole bus words, and K also whole transpose
        words when A is not transposed (gemm_hls host/RunHardware.cpp, MatrixMultiplication.h). Passed to gemm_runner."""
        k, e = self.kernel, self.elem_bytes
        mult_k = k.bus_width_bytes.k // e
        if not k.transposed_a:
            mult_k = math.lcm(mult_k, k.transpose_width_bytes // e)
        mult_n = k.bus_width_bytes.n // e if k.transposed_a else 1
        return mult_n, mult_k, k.bus_width_bytes.m // e

    def peak_gflops(self):
        k = self.kernel
        return 2 * k.parallelism_n * k.parallelism_m * self.clocks.accel_mhz * 1e6 / 1e9

    # Validation ---------------------------------------------------------------------------------------------------

    def validate(self):
        """Raise ConfigError on any rule gemm_hls enforces (CMake FATAL_ERROR or static_assert); return warnings."""
        k, e = self.kernel, None
        errors, warnings = [], []
        if k.data_type not in DATA_TYPES:
            raise ConfigError(f"kernel.data_type {k.data_type!r} not in {sorted(DATA_TYPES)}")
        e = self.elem_bytes
        bw = k.bus_width_bytes
        for name in ("n", "k", "m"):
            w = getattr(bw, name)
            if w <= 0 or w % e:
                errors.append(f"bus_width_bytes.{name}={w} must be a positive multiple of sizeof({k.data_type})={e}")
        for name in ("parallelism_n", "parallelism_m", "memory_tile_size_n", "memory_tile_size_m"):
            if getattr(k, name) <= 0:
                errors.append(f"kernel.{name} must be positive")
        if errors:
            raise ConfigError("; ".join(errors))

        width_m = bw.m // e
        if width_m % k.parallelism_m:
            errors.append(f"(bus_width_bytes.m / sizeof) = {width_m} must be a multiple of parallelism_m = {k.parallelism_m}")
        if k.memory_tile_size_m % width_m:
            errors.append(f"memory_tile_size_m = {k.memory_tile_size_m} must be a multiple of bus_width_bytes.m / sizeof = {width_m}")
        if k.memory_tile_size_n % k.parallelism_n:
            errors.append(f"memory_tile_size_n = {k.memory_tile_size_n} must be a multiple of parallelism_n = {k.parallelism_n}")
        if k.memory_tile_size_m % k.parallelism_m:
            errors.append(f"memory_tile_size_m = {k.memory_tile_size_m} must be a multiple of parallelism_m = {k.parallelism_m}")
        elif k.memory_tile_size_n % k.parallelism_n == 0 and \
                k.memory_tile_size_m // k.parallelism_m < k.memory_tile_size_n // k.parallelism_n:
            # src/Compute.cpp static_assert(kInnerTilesM >= kInnerTilesN): double buffering of A.
            errors.append(f"tile_m/PM = {k.memory_tile_size_m // k.parallelism_m} must be >= tile_n/PN = "
                          f"{k.memory_tile_size_n // k.parallelism_n} (double buffering of A, Compute.cpp)")
        if k.transposed_a:
            if k.memory_tile_size_n % (bw.n // e):
                errors.append(f"transposed A: memory_tile_size_n must be a multiple of bus_width_bytes.n / sizeof = {bw.n // e}")
        else:
            if k.transpose_width_bytes % e:
                errors.append(f"transpose_width_bytes = {k.transpose_width_bytes} must be a multiple of sizeof = {e}")
            if k.transpose_width_bytes % bw.k:
                errors.append(f"transpose_width_bytes = {k.transpose_width_bytes} must be a multiple of bus_width_bytes.k = {bw.k}")
            limit = (k.memory_tile_size_n // k.parallelism_n) * (k.memory_tile_size_m // k.parallelism_m)
            if k.memory_tile_size_n > limit:
                warnings.append(f"memory_tile_size_n = {k.memory_tile_size_n} > (tile_n/PN)*(tile_m/PM) = {limit}: "
                                "the on-chip transpose of A cannot keep up (upstream CMake warning)")
        for ch in (self.memory_map.a, self.memory_map.b, self.memory_map.c):
            if ch not in ("ch0", "ch1"):
                errors.append(f"memory_map entries must be ch0 or ch1, got {ch!r}")
        if self.hls.export_flow not in ("none", "syn", "impl"):
            errors.append("hls.export_flow must be none, syn or impl")
        if self.vivado.preset not in VIVADO_PRESETS:
            errors.append(f"vivado.preset must be one of {sorted(VIVADO_PRESETS)}")
        if errors:
            raise ConfigError("; ".join(errors))

        dsp = k.parallelism_n * k.parallelism_m * DATA_TYPES[k.data_type]["dsp_per_mac"]
        if dsp > DEVICE["dsp"]:
            warnings.append(f"rough DSP estimate {dsp} > {DEVICE['dsp']} on the {DEVICE['part']}")
        return warnings

    # Config.h --------------------------------------------------------------------------------------------------

    def config_h_values(self):
        k, e, bw = self.kernel, self.elem_bytes, self.kernel.bus_width_bytes
        v = {
            "MM_DATA_TYPE":          k.data_type,
            "MM_MEMORY_BUS_WIDTH_N": bw.n,
            "MM_MEMORY_BUS_WIDTH_K": bw.k,
            "MM_MEMORY_BUS_WIDTH_M": bw.m,
            "MM_SIZE_N": 0, "MM_SIZE_K": 0, "MM_SIZE_M": 0,  # Dynamic sizes: unused.
            "MM_MEMORY_TILE_SIZE_N": k.memory_tile_size_n,
            "MM_MEMORY_TILE_SIZE_M": k.memory_tile_size_m,
            "MM_PARALLELISM_N":      k.parallelism_n,
            "MM_PARALLELISM_M":      k.parallelism_m,
            "MM_GRANULARITY_N":      1,
            "MM_TRANSPOSE_WIDTH":    k.transpose_width_bytes,
            "MM_CLOCK_INTERNAL":     self.clocks.accel_mhz,
            "MM_GOLDEN_DIR":         "",
            "MM_MAP_OP":             "Multiply",
            "MM_REDUCE_OP":          "Add",
            "MM_KERNEL_WIDTH_M":     k.parallelism_m * e,
            "MM_PIPE_DEPTH":         k.fifo_depths.pipe,
            "MM_A_SPLIT_DEPTH":      k.fifo_depths.a_split or 0,
            "MM_B_MEMORY_DEPTH":     k.fifo_depths.b_memory or 0,
            "MM_C_MEMORY_DEPTH":     k.fifo_depths.c_memory or 0,
            "MM_AXI_MAX_BURST":      k.m_axi.max_burst_len,
            "MM_AXI_OUTSTANDING":    k.m_axi.num_outstanding,
            "MM_AXI_LATENCY":        k.m_axi.latency,
        }
        for t, info in DATA_TYPES.items():
            v[f"MM_DATA_WIDTH_{t}"] = info["bytes"]
        return v

    def render_config_h(self, template_path=None):
        """Fill ${NAME} placeholders (nested ones like ${MM_DATA_WIDTH_${MM_DATA_TYPE}} from the inside out)."""
        template_path = template_path or os.path.join(KERNEL_DIR, "include", "Config.h.in")
        with open(template_path) as f:
            text = f.read()
        values = {k: str(v) for k, v in self.config_h_values().items()}
        pattern = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
        for _ in range(4):
            text, n = pattern.subn(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), text)
            if n == 0:
                break
        left = pattern.findall(text)
        if left:
            raise ConfigError(f"Config.h.in placeholders without a value: {sorted(set(left))}")
        return text

    # Hashes ----------------------------------------------------------------------------------------------------

    def config_hash(self):
        """sha256 of the canonical resolved config (sorted keys, defaults filled), 8 hex digits for display."""
        return _sha(json.dumps(self.to_dict(), sort_keys=True))

    def hls_hash(self):
        """What the HLS IP depends on: kernel, clocks, hls sections and the kernel source tree."""
        d = {"kernel": dataclasses.asdict(self.kernel), "clocks": dataclasses.asdict(self.clocks),
             "hls": dataclasses.asdict(self.hls), "sources": tree_hash(KERNEL_DIR)}
        return _sha(json.dumps(d, sort_keys=True))

    def build_id(self):
        return f"{self.name}-{self.config_hash()[:8]}"

    def auto_name(self):
        """Name from the parameters that usually vary (plan section 5 naming convention)."""
        k = self.kernel
        short = {"float": "f32", "half": "f16", "int32_t": "i32", "int16_t": "i16", "uint8_t": "u8"}[k.data_type]
        name = (f"{short}_pe{k.parallelism_n}x{k.parallelism_m}_t{k.memory_tile_size_n}x{k.memory_tile_size_m}"
                f"_{self.clocks.accel_mhz:g}mhz")
        if k.transposed_a:
            name += "_tA"
        if self.vivado.preset != "default":
            name += f"_{self.vivado.preset}"
        return name


# Helpers ----------------------------------------------------------------------------------------------------------

def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def tree_hash(path):
    """sha256 over relative paths and contents of all files below path (sorted)."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            h.update(os.path.relpath(full, path).encode())
            with open(full, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


def _from_dict(cls, d, where):
    if not isinstance(d, dict):
        raise ConfigError(f"{where}: expected an object")
    known = {f.name: f for f in fields(cls)}
    unknown = set(d) - set(known)
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    kwargs = {}
    for name, value in d.items():
        f = known[name]
        default = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default
        if dataclasses.is_dataclass(default):
            kwargs[name] = _from_dict(type(default), value, f"{where}.{name}")
        else:
            kwargs[name] = value
    return cls(**kwargs)
