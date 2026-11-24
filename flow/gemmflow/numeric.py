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
#   T2 fixed/ufixed x 8, 16, 32      ap_fixed<W, W-F> / ap_ufixed<W, W-F> (AP_TRN, AP_WRAP)
#   T3 int/uint x 2, 4 (probe)       ap_int<W> / ap_uint<W>, packed densely: the kernel gets a logical bus width
#

import math
import re
from dataclasses import dataclass
from typing import Optional

BUS_BITS = 512                      # physical memory word (LiteDRAM native port, kernel m_axi width)
FAMILIES = ("float", "half", "int", "uint", "fixed", "ufixed", "pow2")
NATIVE_INT_BITS = (8, 16, 32, 64)
PACKED_INT_BITS = (2, 4)            # T3: divide 512, sizeof(ap_int<W>) = 1
FIXED_BITS = (8, 16, 32)
PLAN = "docs/notes2/int-dtype-plan.md section 3"


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

    # Derived ------------------------------------------------------------------------------------------------------
    @property
    def elems_per_word(self):
        return BUS_BITS // self.bits

    @property
    def packed(self):
        """Several elements per byte or per container (T3): the kernel is given a logical bus width."""
        return self.bits < 8 * self.container_bytes

    def logical_bytes(self, physical_bytes):
        """Bus width in container units, as the kernel computes elements (bytes / sizeof(Data_t))."""
        return physical_bytes * 8 * self.container_bytes // self.bits

    @property
    def needs_ap_header(self):
        return self.c_type.startswith("ap_")

    @property
    def spec(self):
        if self.kind == "float":
            return f"float{self.bits}"
        if self.kind == "fixed":
            return f"{'' if self.signed else 'u'}fixed{self.bits}.{self.frac_bits}"
        return f"{'' if self.signed else 'u'}int{self.bits}"

    @property
    def short(self):
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
        mult = 0 if mult_resource == "fabric" else (1 if self.bits <= 18 else math.ceil(self.bits / 25) * math.ceil(self.bits / 18))
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
        raise NumericError("data_type 'pow2' (power-of-two weights, e.g. DenseShift) needs separate A/B/C types and a "
                           f"shift operator in the kernel: not supported yet ({PLAN})")
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
        raise NumericError(f"accum_bits {accum_bits} != data_bits {nf.bits}: a wider accumulator needs a separate C type "
                           f"in the kernel ({PLAN})")
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
