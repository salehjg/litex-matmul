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

from .numeric import NumericError, is_mixed_config, resolve as resolve_numeric, resolve_mixed

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KERNEL_DIR = os.path.join(ROOT, "kernels", "gemm")
KERNEL_MIXED_DIR = os.path.join(ROOT, "kernels", "gemm_mixed")  # separate A/B/C types (docs/notes3)

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


# Fields added after 2026-11-24 are hashed only when they are not None, so the build ids (and HLS hashes) of earlier
# configs stay the same (flow/tests pins them). Section -> field names.
LATE_FIELDS = {"kernel": ("data_bits", "frac_bits", "accum_bits", "b_type", "b_bits")}


def _hash_dict(d):
    d = json.loads(json.dumps(d))
    for section, names in LATE_FIELDS.items():
        for n in names:
            if d.get(section, {}).get(n, "absent") is None:
                del d[section][n]
    return d


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
    data_type:             str = "float"   # float | half | int | uint | fixed | ufixed (flow/gemmflow/numeric.py)
    data_bits:             Optional[int] = None   # None: 32 for float, 16 for half; required otherwise
    frac_bits:             Optional[int] = None   # fixed/ufixed: fractional bits
    accum_bits:            Optional[int] = None   # C (the sum): None = data_bits; wider -> kernels/gemm_mixed
    b_type:                Optional[str] = None   # B format when it differs from A: int | uint | pow2 (DenseShift)
    b_bits:                Optional[int] = None   # B bits (pow2: 2, 3, 4)
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

    def is_mixed(self):
        """True when the config needs kernels/gemm_mixed (B format of its own, or a C wider than the element)."""
        k = self.kernel
        return is_mixed_config(k.data_type, k.data_bits, k.b_type, k.b_bits, k.accum_bits)

    def kernel_dir(self):
        return KERNEL_MIXED_DIR if self.is_mixed() else KERNEL_DIR

    def numeric(self):
        """The element format (flow/gemmflow/numeric.py): a NumericFormat, or a MixedFormat (A, B, C, MAC) for
        kernels/gemm_mixed. Raises ConfigError if unsupported."""
        k = self.kernel
        try:
            if self.is_mixed():
                return resolve_mixed(k.data_type, k.data_bits, k.frac_bits, k.b_type, k.b_bits, k.accum_bits)
            return resolve_numeric(k.data_type, k.data_bits, k.frac_bits, k.accum_bits)
        except NumericError as e:
            raise ConfigError(str(e)) from None

    def operands(self):
        """(A, B, C) NumericFormats; all the same one for single-type configs."""
        nf = self.numeric()
        return (nf.a, nf.b, nf.c) if nf.is_mixed else (nf, nf, nf)

    @property
    def elem_bytes(self):
        """sizeof(Data_t): container bytes of one element (several packed elements share one for 2/4-bit types)."""
        return self.numeric().container_bytes if not self.is_mixed() else self.operands()[0].container_bytes

    def elems(self, nbytes, operand="a"):
        """Number of elements of operand a/b/c in nbytes of memory (512-bit words hold 512 / bits elements)."""
        f = self.operands()["abc".index(operand)]
        return nbytes * 8 // f.storage_bits

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
        k = self.kernel
        mult_k = self.elems(k.bus_width_bytes.k)
        if not k.transposed_a:
            mult_k = math.lcm(mult_k, self.elems(k.transpose_width_bytes))
        mult_n = self.elems(k.bus_width_bytes.n) if k.transposed_a else 1
        mult_m = math.lcm(self.elems(k.bus_width_bytes.m, "b"), self.elems(k.bus_width_bytes.m, "c"))
        return mult_n, mult_k, mult_m

    def peak_gops(self):
        """2 operations per multiply-add, P_N x P_M multiply-adds per cycle (GFLOP/s for float types)."""
        k = self.kernel
        return 2 * k.parallelism_n * k.parallelism_m * self.clocks.accel_mhz * 1e6 / 1e9

    peak_gflops = peak_gops  # name used by earlier scripts and records

    # Validation ---------------------------------------------------------------------------------------------------

    def validate(self):
        """Raise ConfigError on any rule gemm_hls enforces (CMake FATAL_ERROR or static_assert) or the element format
        does not support; return warnings. Widths are counted in elements per bus word (512 / bits for 512-bit words)."""
        k = self.kernel
        errors, warnings = [], []
        nf = self.numeric()
        fa, fb, fc = self.operands()
        bw = k.bus_width_bytes
        for name, fmts in (("n", (fa,)), ("k", (fa,)), ("m", (fb, fc))):
            w = getattr(bw, name)
            for f in fmts:
                if w <= 0 or (w * 8) % f.storage_bits:
                    errors.append(f"bus_width_bytes.{name}={w} must be a positive multiple of the {f.storage_bits}-bit "
                                  "element")
        for name in ("parallelism_n", "parallelism_m", "memory_tile_size_n", "memory_tile_size_m"):
            if getattr(k, name) <= 0:
                errors.append(f"kernel.{name} must be positive")
        if errors:
            raise ConfigError("; ".join(errors))

        width_m, width_n, width_c = self.elems(bw.m, "b"), self.elems(bw.n), self.elems(bw.m, "c")
        if width_m % k.parallelism_m:
            errors.append(f"elements per M bus word = {width_m} must be a multiple of parallelism_m = {k.parallelism_m}")
        if k.memory_tile_size_m % width_m:
            errors.append(f"memory_tile_size_m = {k.memory_tile_size_m} must be a multiple of the elements per M bus "
                          f"word = {width_m}")
        if nf.is_mixed:
            # kernels/gemm_mixed ConvertWidthC splits a compute pack over several C words or gathers several packs.
            if width_c % k.parallelism_m and k.parallelism_m % width_c:
                errors.append(f"C elements per bus word = {width_c} and parallelism_m = {k.parallelism_m} must divide "
                              "one another")
            if k.memory_tile_size_m % width_c:
                errors.append(f"memory_tile_size_m = {k.memory_tile_size_m} must be a multiple of the C elements per "
                              f"bus word = {width_c}")
        if k.memory_tile_size_n % k.parallelism_n:
            errors.append(f"memory_tile_size_n = {k.memory_tile_size_n} must be a multiple of parallelism_n = {k.parallelism_n}")
        if k.memory_tile_size_m % k.parallelism_m:
            errors.append(f"memory_tile_size_m = {k.memory_tile_size_m} must be a multiple of parallelism_m = {k.parallelism_m}")
        elif k.memory_tile_size_n % k.parallelism_n == 0 and \
                k.memory_tile_size_m // k.parallelism_m < k.memory_tile_size_n // k.parallelism_n:
            # src/Compute.cpp static_assert(kInnerTilesM >= kInnerTilesN): double buffering of A.
            errors.append(f"tile_m/PM = {k.memory_tile_size_m // k.parallelism_m} must be >= tile_n/PN = "
                          f"{k.memory_tile_size_n // k.parallelism_n} (double buffering of A, Compute.cpp)")
        if k.memory_tile_size_m % k.parallelism_m == 0 and k.memory_tile_size_m // k.parallelism_m < k.parallelism_n:
            # src/Compute.cpp forwards the next outer product's A values in the slots m1 = locationN .. PN-1 of the
            # m1 loop (kInnerTilesM long). Shorter, the PEs at the end get none: wrong C, then a hung kernel (int4 PE
            # 48x64 tile 768x2048 on the card, 2026-11-24). Upstream has no check for it.
            errors.append(f"tile_m/PM = {k.memory_tile_size_m // k.parallelism_m} must be >= parallelism_n = "
                          f"{k.parallelism_n} (A forwarding along the PE chain, Compute.cpp)")
        # src/Compute.cpp WriteC_Flattened counts n1 and m1 in ap_uint<hlslib::ConstLog2(x)> (floor): they only reach
        # x - 1 for powers of two. Otherwise C comes out wrong (misc/pe_forwarding/sim.py). Upstream has no check.
        for what, size, par in (("tile_n/PN", k.memory_tile_size_n, k.parallelism_n),
                                ("tile_m/PM", k.memory_tile_size_m, k.parallelism_m)):
            q = size // par if size % par == 0 else 0
            if q and q & (q - 1):
                errors.append(f"{what} = {q} must be a power of two (C write-back counters, Compute.cpp)")
        if k.transposed_a:
            if k.memory_tile_size_n % width_n:
                errors.append(f"transposed A: memory_tile_size_n must be a multiple of the elements per N bus word = {width_n}")
        else:
            if (k.transpose_width_bytes * 8) % fa.storage_bits:
                errors.append(f"transpose_width_bytes = {k.transpose_width_bytes} must hold whole {fa.storage_bits}-bit "
                              "elements")
            if k.transpose_width_bytes % bw.k:
                errors.append(f"transpose_width_bytes = {k.transpose_width_bytes} must be a multiple of bus_width_bytes.k = {bw.k}")
            limit = (k.memory_tile_size_n // k.parallelism_n) * (k.memory_tile_size_m // k.parallelism_m)
            if k.memory_tile_size_n > limit:
                warnings.append(f"memory_tile_size_n = {k.memory_tile_size_n} > (tile_n/PN)*(tile_m/PM) = {limit}: "
                                "the on-chip transpose of A cannot keep up (upstream CMake warning)")
        for res_name in ("mult_resource", "add_resource"):
            v = getattr(k, res_name)
            if v is not None and v not in nf.valid_impls():
                allowed = list(nf.valid_impls()) or ["null (not wired into kernels/gemm_mixed)"]
                errors.append(f"kernel.{res_name} = {v!r} must be one of {allowed} for {nf.spec}")
        for ch in (self.memory_map.a, self.memory_map.b, self.memory_map.c):
            if ch not in ("ch0", "ch1"):
                errors.append(f"memory_map entries must be ch0 or ch1, got {ch!r}")
        if self.hls.export_flow not in ("none", "syn", "impl"):
            errors.append("hls.export_flow must be none, syn or impl")
        if self.vivado.preset not in VIVADO_PRESETS:
            errors.append(f"vivado.preset must be one of {sorted(VIVADO_PRESETS)}")
        if errors:
            raise ConfigError("; ".join(errors))

        dsp = k.parallelism_n * k.parallelism_m * nf.dsp_per_mac(k.mult_resource, k.add_resource)
        if dsp > DEVICE["dsp"]:
            warnings.append(f"rough DSP estimate {dsp} > {DEVICE['dsp']} on the {DEVICE['part']}")
        return warnings

    # Config.h --------------------------------------------------------------------------------------------------

    def config_h_values(self):
        """Placeholder values for Config.h.in. Widths are in container units (bytes / sizeof(Data_t)), as the kernel
        counts elements: for packed 2/4-bit types the logical bus width is 512 / bits containers per 64-byte word."""
        if self.is_mixed():
            return self._config_h_values_mixed()
        k, bw, nf = self.kernel, self.kernel.bus_width_bytes, self.numeric()
        e = nf.container_bytes
        v = {
            "MM_DATA_TYPE":          nf.c_type,
            "MM_MEMORY_BUS_WIDTH_N": nf.logical_bytes(bw.n),
            "MM_MEMORY_BUS_WIDTH_K": nf.logical_bytes(bw.k),
            "MM_MEMORY_BUS_WIDTH_M": nf.logical_bytes(bw.m),
            "MM_SIZE_N": 0, "MM_SIZE_K": 0, "MM_SIZE_M": 0,  # Dynamic sizes: unused.
            "MM_MEMORY_TILE_SIZE_N": k.memory_tile_size_n,
            "MM_MEMORY_TILE_SIZE_M": k.memory_tile_size_m,
            "MM_PARALLELISM_N":      k.parallelism_n,
            "MM_PARALLELISM_M":      k.parallelism_m,
            "MM_GRANULARITY_N":      1,
            "MM_TRANSPOSE_WIDTH":    nf.logical_bytes(k.transpose_width_bytes),
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
        v["MM_DATA_WIDTH"] = e  # sizeof(Data_t); Config.h.in writes it as ${MM_DATA_WIDTH_${MM_DATA_TYPE}}
        return v

    def _config_h_values_mixed(self):
        """kernels/gemm_mixed/include/Config.h.in: one type, bit width and logical bus width per operand."""
        k, bw, nf = self.kernel, self.kernel.bus_width_bytes, self.numeric()
        fa, fb, fc = nf.a, nf.b, nf.c
        return {
            "MM_DATA_TYPE_A": fa.ap_type, "MM_DATA_TYPE_B": fb.ap_type, "MM_DATA_TYPE_C": fc.ap_type,
            "MM_BITS_A": fa.bits, "MM_BITS_B": fb.storage_bits, "MM_BITS_C": fc.bits,
            "MM_MEMORY_BUS_WIDTH_N": fa.logical_bytes(bw.n),
            "MM_MEMORY_BUS_WIDTH_K": fa.logical_bytes(bw.k),
            "MM_MEMORY_BUS_WIDTH_M": fb.logical_bytes(bw.m),
            "MM_MEMORY_BUS_WIDTH_C": fc.logical_bytes(bw.m),
            "MM_SIZE_N": 0, "MM_SIZE_K": 0, "MM_SIZE_M": 0,
            "MM_MEMORY_TILE_SIZE_N": k.memory_tile_size_n,
            "MM_MEMORY_TILE_SIZE_M": k.memory_tile_size_m,
            "MM_PARALLELISM_N":      k.parallelism_n,
            "MM_PARALLELISM_M":      k.parallelism_m,
            "MM_GRANULARITY_N":      1,
            "MM_TRANSPOSE_WIDTH":    fa.logical_bytes(k.transpose_width_bytes),
            "MM_CLOCK_INTERNAL":     self.clocks.accel_mhz,
            "MM_GOLDEN_DIR":         "",
            "MM_MAC":                nf.mac.upper(),
            "MM_SHIFT_BITS":         fb.bits if fb.kind == "pow2" else 0,
            "MM_KERNEL_WIDTH_M":     k.parallelism_m * fb.container_bytes,
            "MM_DATA_WIDTH_A":       fa.container_bytes,
            "MM_PIPE_DEPTH":         k.fifo_depths.pipe,
            "MM_A_SPLIT_DEPTH":      k.fifo_depths.a_split or 0,
            "MM_B_MEMORY_DEPTH":     k.fifo_depths.b_memory or 0,
            "MM_C_MEMORY_DEPTH":     k.fifo_depths.c_memory or 0,
            "MM_AXI_MAX_BURST":      k.m_axi.max_burst_len,
            "MM_AXI_OUTSTANDING":    k.m_axi.num_outstanding,
            "MM_AXI_LATENCY":        k.m_axi.latency,
        }

    def render_config_h(self, template_path=None):
        """Fill ${NAME} placeholders (nested ones like ${MM_DATA_WIDTH_${MM_DATA_TYPE}} from the inside out)."""
        template_path = template_path or os.path.join(self.kernel_dir(), "include", "Config.h.in")
        with open(template_path) as f:
            text = f.read()
        values = {k: str(v) for k, v in self.config_h_values().items()}
        # The nested width lookup is resolved here: type names such as ap_fixed<16, 8> are not placeholder names.
        if "MM_DATA_WIDTH" in values:
            text = text.replace("${MM_DATA_WIDTH_${MM_DATA_TYPE}}", values.pop("MM_DATA_WIDTH"))
        pattern = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
        for _ in range(4):
            text, n = pattern.subn(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), text)
            if n == 0:
                break
        left = pattern.findall(text)
        if left:
            raise ConfigError(f"Config.h.in placeholders without a value: {sorted(set(left))}")
        if self.numeric().needs_ap_header:  # Config.h.in includes no ap_int.h before Data_t (notes2 plan 2.1)
            anchor = '#include "hlslib/xilinx/Operators.h"\n'
            if anchor not in text:
                raise ConfigError("Config.h.in: cannot place the ap_fixed.h include")
            text = text.replace(anchor, anchor + "#include <ap_fixed.h>  // litex-matmul: ap_* element type\n", 1)
        return text

    # Hashes ----------------------------------------------------------------------------------------------------

    def config_hash(self):
        """sha256 of the canonical resolved config (sorted keys, defaults filled), 8 hex digits for display."""
        return _sha(json.dumps(_hash_dict(self.to_dict()), sort_keys=True))

    def hls_hash(self):
        """What the HLS IP depends on: kernel, clocks, hls sections and the kernel source tree."""
        d = _hash_dict({"kernel": dataclasses.asdict(self.kernel), "clocks": dataclasses.asdict(self.clocks),
                        "hls": dataclasses.asdict(self.hls)})
        d["sources"] = tree_hash(self.kernel_dir())
        return _sha(json.dumps(d, sort_keys=True))

    def build_id(self):
        return f"{self.name}-{self.config_hash()[:8]}"

    def auto_name(self):
        """Name from the parameters that usually vary (plan section 5 naming convention)."""
        k = self.kernel
        short = self.numeric().short
        name = (f"{short}_pe{k.parallelism_n}x{k.parallelism_m}_t{k.memory_tile_size_n}x{k.memory_tile_size_m}"
                f"_{self.clocks.accel_mhz:g}mhz")
        if k.transposed_a:
            name += "_tA"
        if self.vivado.preset != "default":
            name += f"_{self.vivado.preset}"
        for res, tag in ((k.mult_resource, "mul"), (k.add_resource, "add")):  # operator binding, e.g. _mulfabric
            if res:
                name += f"_{tag}{res}"
        mm = self.memory_map
        if (mm.a, mm.b, mm.c) != ("ch0", "ch1", "ch1"):  # non-default map: channel digits of A, B, C
            name += f"_m{mm.a[-1]}{mm.b[-1]}{mm.c[-1]}"
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
