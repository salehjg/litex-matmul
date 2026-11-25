#
# numeric.py - number formats of the GEMM kernel's elements (docs/notes2/int-dtype-plan.md).
#
# A build config describes the element format with kernel.data_type (family: float, half, int, uint, fixed, ufixed;
# pow2 reserved) plus kernel.data_bits / frac_bits / accum_bits. resolve() turns that into a NumericFormat: the C type
# for Config.h, sizeof (container bytes), elements per 512-bit memory word, the canonical spec string shared with the
# host runner (float32, int8, uint4, fixed16.8), the short name used in build names, and the result-row columns.
#
# The kernel uses one type for A, B, C and the accumulation (gemm_hls Data_t), so integer and fixed-point results wrap
# at the element width. Tiers that need no kernel edit (plan section 3):
#   T0 float32 (and half)            float, half
#   T1 int/uint x 8, 16, 32, 64      int8_t ... uint64_t (native)
#   T2 fixed/ufixed x 16, 32         ap_fixed<W, W-F> / ap_ufixed<W, W-F> (AP_TRN, AP_WRAP); 8-bit crashes Vitis HLS
#   T3 int/uint x 2, 4 (probe)       ap_int<W> / ap_uint<W>, packed densely: the kernel gets a logical bus width
#
# Mixed formats (kernels/gemm_mixed, docs/notes3/denseshift-plan.md): kernel.b_type / b_bits give B its own format and
# kernel.accum_bits a wider C (the sum). resolve_mixed() returns a MixedFormat with the same interface as
# NumericFormat (spec, kind, short, columns(), runner_args()), so scripts need not tell them apart:
#   A  int/uint x 2, 4, 8, 16                 ap_int<W> / ap_uint<W>
#   B  int/uint x 2, 4, 8, 16  -> MAC multiply
#      pow2 x 2, 3, 4          -> MAC DenseShift: code = sign << (n-1) | exponent, weight (-1)^s 2^e, no zero
#                                 (Li et al., ICCV 2023); 3-bit codes are stored in 4-bit lanes
#   C  int x 16, 32 (the accumulator; sums wrap at this width)
#

import math
import re
from dataclasses import dataclass
from typing import Optional

BUS_BITS = 512                      # physical memory word (LiteDRAM native port, kernel m_axi width)
FAMILIES = ("float", "half", "int", "uint", "fixed", "ufixed", "pow2")
NATIVE_INT_BITS = (8, 16, 32, 64)
PACKED_INT_BITS = (2, 4)            # T3: divide 512, sizeof(ap_int<W>) = 1
FIXED_BITS = (16, 32)              # 8-bit ap_fixed crashes Vitis HLS 2026.1 csynth (see FIXED8_NOTE)
FIXED8_NOTE = ("Vitis HLS 2026.1 csynth crashes (clang-3.9-csynth reflow, exit 254) on every ap_fixed<8,*> / "
               "ap_ufixed<8,*> tried (docs/notes2/int-dtype-progress.md, dtype_hls)")
PLAN = "docs/notes2/int-dtype-plan.md section 3"
PLAN3 = "docs/notes3/denseshift-plan.md"
MIXED_A_BITS = (2, 4, 8, 16)
MIXED_B_INT_BITS = (2, 4, 8, 16)
DSHIFT_BITS = (2, 3, 4)
MIXED_C_BITS = (16, 32)


class NumericError(ValueError):
    pass


def _ap_sizeof(bits):
    """sizeof(ap_int<W>) in Vitis C simulation: valtype<(W+7)/8> -> 1, 2, 4 (3 and 4 bytes), 8 (5..8 bytes)."""
    n = (bits + 7) // 8
    return {1: 1, 2: 2, 3: 4, 4: 4}.get(n, 8)


@dataclass(frozen=True)
class NumericFormat:
    family: str                 # config family: float, half, int, uint, fixed, ufixed
    kind: str                   # quantization type for the records: float | int | fixed | pow2
    bits: int                   # element width (storage and arithmetic)
    signed: bool
    frac_bits: Optional[int]    # fixed point only
    c_type: str                 # Data_t in Config.h
    container_bytes: int        # sizeof(Data_t)
    tier: str
    lane_bits: Optional[int] = None  # storage width when wider than bits (3-bit DenseShift codes in 4-bit lanes)

    # Derived ------------------------------------------------------------------------------------------------------
    @property
    def storage_bits(self):
        return self.lane_bits or self.bits

    @property
    def elems_per_word(self):
        return BUS_BITS // self.storage_bits

    @property
    def packed(self):
        """Several elements per byte or per container (T3): the kernel is given a logical bus width."""
        return self.storage_bits < 8 * self.container_bytes

    def logical_bytes(self, physical_bytes):
        """Bus width in container units, as the kernel computes elements (bytes / sizeof(Data_t))."""
        return physical_bytes * 8 * self.container_bytes // self.storage_bits

    @property
    def ap_type(self):
        """The element as an ap_[u]int type (kernels/gemm_mixed computes in exact widths)."""
        if self.kind == "int":
            return f"ap_{'' if self.signed else 'u'}int<{self.bits}>"
        if self.kind == "pow2":
            return self.c_type
        raise NumericError(f"{self.spec}: no ap_int type")

    @property
    def is_mixed(self):
        return False

    def runner_args(self):
        """gemm_runner options that select this format."""
        return ["--dtype", self.spec]

    @property
    def needs_ap_header(self):
        return self.c_type.startswith("ap_")

    @property
    def spec(self):
        if self.kind == "pow2":
            return f"dshift{self.bits}"
        if self.kind == "float":
            return f"float{self.bits}"
        if self.kind == "fixed":
            return f"{'' if self.signed else 'u'}fixed{self.bits}.{self.frac_bits}"
        return f"{'' if self.signed else 'u'}int{self.bits}"

    @property
    def short(self):
        if self.kind == "pow2":
            return f"ds{self.bits}"
        if self.kind == "float":
            return f"f{self.bits}"
        if self.kind == "fixed":
            return f"{'' if self.signed else 'u'}fx{self.bits}f{self.frac_bits}"
        return f"{'i' if self.signed else 'u'}{self.bits}"

    @property
    def hls_ops(self):
        """Vitis config_op operator names for this format's multiply and add."""
        if self.family == "float":
            return {"mult": "fmul", "add": "fadd"}
        if self.family == "half":
            return {"mult": "hmul", "add": "hadd"}
        return {"mult": "mul", "add": "add"}

    def valid_impls(self):
        base = ("all", "dsp", "fabric")
        return base + ("meddsp", "fulldsp", "maxdsp", "primitivedsp") if self.kind == "float" else base

    def dsp_per_mac(self, mult_resource=None, add_resource=None):
        """Rough DSP48E1 count per multiply-add (for a budget warning only)."""
        if self.family == "float":
            return 5
        if self.family == "half":
            return 3
        if mult_resource == "fabric" or (mult_resource is None and self.bits <= 8):
            mult = 0  # Vivado maps 8-bit and narrower multiplies to LUTs (int8/int4 builds: ~40 DSPs in total)
        else:
            mult = 1 if self.bits <= 18 else math.ceil(self.bits / 25) * math.ceil(self.bits / 18)
        add = 1 if add_resource == "dsp" else 0
        return mult + add

    def columns(self):
        """Flat result-row fields (plan section 4.3). Per-operand and quantization fields are future-proofing: today
        A, B, C and the accumulator share one format and there is no host-side affine quantization."""
        return {
            "dtype": self.spec, "dtype_kind": self.kind, "dtype_bits": self.bits, "dtype_signed": self.signed,
            "dtype_frac_bits": self.frac_bits, "elems_per_word": self.elems_per_word,
            "acc_dtype": self.spec, "acc_bits": self.bits,
            "a_dtype": self.spec, "b_dtype": self.spec, "c_dtype": self.spec,
            "quant_scale": None, "quant_zero_point": None, "pow2_exp_bits": None, "pow2_dense": None,
        }


def resolve(data_type, data_bits=None, frac_bits=None, accum_bits=None):
    """NumericFormat for a config's kernel.data_type / data_bits / frac_bits / accum_bits, or NumericError."""
    fam = data_type
    if fam not in FAMILIES:
        legacy = re.fullmatch(r"(u?)int(\d+)_t", str(fam))
        hint = f" (use data_type '{legacy.group(1)}int' with data_bits {legacy.group(2)})" if legacy else ""
        raise NumericError(f"kernel.data_type {fam!r} not in {list(FAMILIES)}{hint}")
    if fam == "pow2":
        raise NumericError("data_type 'pow2' (DenseShift power-of-two weights) is a B format: set kernel.b_type 'pow2' "
                           f"and b_bits, with data_type int/uint for A ({PLAN3})")
    if frac_bits is not None and fam not in ("fixed", "ufixed"):
        raise NumericError(f"kernel.frac_bits is only for fixed/ufixed, not {fam}")

    if fam in ("float", "half"):
        natural = 32 if fam == "float" else 16
        if data_bits not in (None, natural):
            raise NumericError(f"data_type {fam} is {natural} bits; data_bits {data_bits} is not supported")
        nf = NumericFormat(fam, "float", natural, True, None, fam, natural // 8, "T0")
    elif fam in ("int", "uint"):
        signed = fam == "int"
        if data_bits is None:
            raise NumericError(f"data_type {fam} needs data_bits")
        if data_bits in NATIVE_INT_BITS:
            nf = NumericFormat(fam, "int", data_bits, signed, None, f"{'' if signed else 'u'}int{data_bits}_t",
                               data_bits // 8, "T1")
        elif data_bits in PACKED_INT_BITS:
            nf = NumericFormat(fam, "int", data_bits, signed, None, f"ap_{'' if signed else 'u'}int<{data_bits}>",
                               _ap_sizeof(data_bits), "T3")
        else:
            raise NumericError(f"{fam}{data_bits}: supported widths are {NATIVE_INT_BITS} (native) and "
                               f"{PACKED_INT_BITS} (packed); other widths need the kernel to count elements by bits "
                               f"instead of sizeof ({PLAN})")
    else:  # fixed, ufixed
        signed = fam == "fixed"
        if data_bits == 8:
            raise NumericError(f"{fam}8: {FIXED8_NOTE}")
        if data_bits not in FIXED_BITS:
            raise NumericError(f"{fam}: data_bits must be one of {FIXED_BITS} ({PLAN})")
        if frac_bits is None:
            raise NumericError(f"{fam} needs frac_bits")
        top = data_bits - 1 if signed else data_bits
        if not 0 <= frac_bits <= top:
            raise NumericError(f"{fam}{data_bits}: frac_bits must be in 0..{top}")
        tmpl = "ap_fixed" if signed else "ap_ufixed"
        nf = NumericFormat(fam, "fixed", data_bits, signed, frac_bits, f"{tmpl}<{data_bits}, {data_bits - frac_bits}>",
                           _ap_sizeof(data_bits), "T2")
    if accum_bits is not None and accum_bits != nf.bits:
        raise NumericError(f"accum_bits {accum_bits} != data_bits {nf.bits}: a wider accumulator is a mixed format "
                           f"(kernels/gemm_mixed), resolved by resolve_mixed ({PLAN3})")
    return nf


_SPEC = re.compile(r"(?P<u>u?)(?P<fam>float|int|fixed)(?P<bits>\d+)(?:\.(?P<frac>\d+))?")


def parse_spec(spec):
    """Canonical spec string (float32, float16, int8, uint4, fixed16.8, ufixed8.4) -> NumericFormat."""
    m = _SPEC.fullmatch(spec or "")
    if not m:
        raise NumericError(f"bad dtype spec {spec!r}")
    bits, frac = int(m["bits"]), (int(m["frac"]) if m["frac"] is not None else None)
    if m["fam"] == "float":
        if m["u"] or frac is not None:
            raise NumericError(f"bad dtype spec {spec!r}")
        return resolve({32: "float", 16: "half"}.get(bits, f"float{bits}"))
    if m["fam"] == "int" and frac is not None:
        raise NumericError(f"bad dtype spec {spec!r}")
    return resolve(m["u"] + m["fam"], bits, frac)


def resolve_pow2(bits):
    """n-bit DenseShift weight code: sign bit + (n-1) exponent bits, no zero (Li et al., ICCV 2023)."""
    if bits not in DSHIFT_BITS:
        raise NumericError(f"pow2 (DenseShift) b_bits must be one of {DSHIFT_BITS}")
    lane = 2 if bits == 2 else 4  # lanes must divide the 512-bit word
    return NumericFormat("pow2", "pow2", bits, True, None, f"ap_uint<{lane}>", 1, "T4", lane if lane != bits else None)


@dataclass(frozen=True)
class MixedFormat:
    """A, B and C formats of kernels/gemm_mixed plus the MAC (multiply or denseshift)."""
    a: NumericFormat
    b: NumericFormat
    c: NumericFormat
    mac: str

    family = "mixed"
    is_mixed = True
    needs_ap_header = False  # kernels/gemm_mixed/include/Config.h.in includes ap_int.h

    @property
    def kind(self):
        return "pow2" if self.mac == "denseshift" else "int"

    @property
    def bits(self):
        return self.a.bits

    @property
    def spec(self):
        return f"{self.a.spec}x{self.b.spec}-{self.c.spec}"

    @property
    def short(self):
        return f"{self.a.short}x{self.b.short}c{self.c.bits}"

    @property
    def c_type(self):
        return f"{self.a.ap_type} x {self.b.ap_type} -> {self.c.ap_type}"

    @property
    def hls_ops(self):
        return {"mult": "mul", "add": "add"}

    def valid_impls(self):
        return ()  # mult_resource / add_resource are not wired into kernels/gemm_mixed

    def dsp_per_mac(self, mult_resource=None, add_resource=None):
        if self.mac == "denseshift" or max(self.a.bits, self.b.bits) <= 8:
            return 0
        return 1

    def runner_args(self):
        return ["--dtype", self.a.spec, "--b-dtype", self.b.spec, "--c-dtype", self.c.spec]

    def columns(self):
        """As NumericFormat.columns(); dtype is the combined spec, dtype_bits / elems_per_word are A's."""
        return {
            "dtype": self.spec, "dtype_kind": self.kind, "dtype_bits": self.a.bits, "dtype_signed": self.a.signed,
            "dtype_frac_bits": None, "elems_per_word": self.a.elems_per_word,
            "acc_dtype": self.c.spec, "acc_bits": self.c.bits,
            "a_dtype": self.a.spec, "b_dtype": self.b.spec, "c_dtype": self.c.spec,
            "quant_scale": None, "quant_zero_point": None,
            "pow2_exp_bits": self.b.bits - 1 if self.mac == "denseshift" else None,
            "pow2_dense": True if self.mac == "denseshift" else None,
        }


def is_mixed_config(data_type, data_bits, b_type, b_bits, accum_bits):
    """True when the config needs kernels/gemm_mixed: B has its own format or C is wider than the element."""
    if b_type is not None or b_bits is not None:
        return True
    return accum_bits is not None and accum_bits != data_bits


def resolve_mixed(data_type, data_bits, frac_bits=None, b_type=None, b_bits=None, accum_bits=None):
    if data_type not in ("int", "uint") or frac_bits is not None:
        raise NumericError(f"mixed formats (kernels/gemm_mixed): A must be int or uint, not {data_type} ({PLAN3})")
    if data_bits not in MIXED_A_BITS:
        raise NumericError(f"mixed formats: A bits must be one of {MIXED_A_BITS}")
    a = resolve(data_type, data_bits)
    b_type = b_type or data_type
    b_bits = b_bits or data_bits
    if b_type == "pow2":
        b, mac = resolve_pow2(b_bits), "denseshift"
    elif b_type in ("int", "uint"):
        if b_bits not in MIXED_B_INT_BITS:
            raise NumericError(f"mixed formats: int B bits must be one of {MIXED_B_INT_BITS}")
        b, mac = resolve(b_type, b_bits), "multiply"
    else:
        raise NumericError(f"kernel.b_type must be int, uint or pow2, not {b_type!r}")
    c_bits = accum_bits or 32
    if c_bits not in MIXED_C_BITS:
        raise NumericError(f"mixed formats: accum_bits (C) must be one of {MIXED_C_BITS}")
    return MixedFormat(a, b, resolve("int", c_bits), mac)


def parse_mixed_spec(spec):
    """Combined spec 'int8xdshift3-int32' -> MixedFormat."""
    m = re.fullmatch(r"(u?int\d+)x(u?int\d+|dshift\d+)-(int\d+)", spec or "")
    if not m:
        raise NumericError(f"bad mixed dtype spec {spec!r}")
    a, b, c = parse_spec(m[1]), m[2], parse_spec(m[3])
    if b.startswith("dshift"):
        b_type, b_bits = "pow2", int(b[6:])
    else:
        nb = parse_spec(b)
        b_type, b_bits = nb.family, nb.bits
    return resolve_mixed(a.family, a.bits, None, b_type, b_bits, c.bits)
