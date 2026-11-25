#
# denseshift.py - DenseShift layers (Li et al., ICCV 2023, arXiv:2208.09708; docs/notes3/denseshift-plan.md).
#
# An n-bit DenseShift weight is w = s * 2^S with s in {-1, +1} and S in {0 .. T}, T = 2^(n-1) - 1: no zero, all 2^n
# codes used (2-bit {+-1, +-2}, 3-bit {+-1, +-2, +-4, +-8}). Training uses the sign-scale decomposition (Eq. 3-6):
#   s   = sign(w_sign)                       sign(0) = +1
#   S_0 = 0,  S_t = H(w_t) * (S_{t-1} + 1)   t = 1 .. T, H(x) = 1 for x > 0 else 0
# so every weight has T + 1 float parameters. H and sign pass gradients straight through (STE). The forward value is
# s * 2^S; the backward goes through s * sqrt(S + 1) (local learning-rate rescale, Eq. 8) or, with rescale=False,
# through s * 2^S (Eq. 7). Low-variance init: all parameters ~ N(0, 1e-3^2) (Sec. 3.3). This follows the official
# 3-bit layer (github.com/xinlinli1990/noah-research, S3-Training/dsconv2d.py), generalised to any n >= 2.
#
# Hardware code of a weight (kernels/gemm_mixed, sw/gemm_host/src/numeric.hpp): code = (s < 0) << (n-1) | S.
#
# Needs only torch (system Python: /usr/bin/python3). Self-test: /usr/bin/python3 ml/denseshift/denseshift.py
#

import math

import torch
import torch.nn as nn
import torch.nn.functional as F


class _Heaviside(torch.autograd.Function):
    """1 for x > 0, else 0; identity backward (official STEBinarize01F)."""

    @staticmethod
    def forward(ctx, x):
        return (x > 0).to(x.dtype)

    @staticmethod
    def backward(ctx, g):
        return g


class _Sign(torch.autograd.Function):
    """+1 for x >= 0, else -1; identity backward (official STBinarizeF)."""

    @staticmethod
    def forward(ctx, x):
        return torch.where(x >= 0, torch.ones_like(x), -torch.ones_like(x))

    @staticmethod
    def backward(ctx, g):
        return g


heaviside = _Heaviside.apply
ste_sign = _Sign.apply


class DenseShiftMixin:
    """Turns a layer's .weight into the sign parameter and adds T exponent parameters (weight_t[0 .. T-1])."""

    def _ds_setup(self, bits, rescale=True, init="lowvar", init_std=1e-3):
        if bits < 2:
            raise ValueError("DenseShift needs at least 2 bits (sign + exponent)")
        self.ds_bits = bits
        self.ds_T = 2 ** (bits - 1) - 1
        self.ds_rescale = rescale
        self.weight_t = nn.Parameter(torch.empty(self.ds_T, *self.weight.shape))
        self.ds_reset(init, init_std)

    @property
    def weight_sign(self):
        return self.weight

    def ds_reset(self, init="lowvar", init_std=1e-3):
        with torch.no_grad():
            if init == "lowvar":
                self.weight.normal_(0, init_std)
                self.weight_t.normal_(0, init_std)
            elif init == "kaiming":  # ablation: the default init the paper replaces (Fig. 5 left)
                fan_out = self.weight[0].numel() // self.weight.shape[1] * self.weight.shape[0] \
                    if self.weight.dim() == 4 else self.weight.shape[0]
                std = math.sqrt(2.0 / fan_out)
                self.weight.normal_(0, std)
                self.weight_t.normal_(0, std)
            else:
                raise ValueError(f"init {init!r}")

    def ds_exponent(self):
        """S as a float tensor (part of the autograd graph)."""
        s = torch.zeros_like(self.weight)
        for t in range(self.ds_T):
            s = (s + 1) * heaviside(self.weight_t[t])
        return s

    def ds_weight(self):
        S = self.ds_exponent()
        sgn = ste_sign(self.weight)
        if not self.ds_rescale:
            return sgn * torch.pow(2.0, S)                    # Eq. 7
        bw = sgn * torch.sqrt(S + 1)                          # Eq. 8 backward
        fw = (sgn * torch.pow(2.0, S)).detach()
        return bw + (fw - bw.detach())

    @torch.no_grad()
    def ds_codes(self):
        """(sign_bit, exponent) as int tensors: sign_bit 1 = negative; weight = (-1)^sign_bit * 2^exponent."""
        S = self.ds_exponent().round().to(torch.int64)
        sign_bit = (self.weight < 0).to(torch.int64)
        return sign_bit, S

    @torch.no_grad()
    def export_codes(self):
        """Hardware codes: (sign_bit << (n-1)) | exponent, same shape as the weight."""
        sign_bit, S = self.ds_codes()
        return (sign_bit << (self.ds_bits - 1)) | S

    def extra_repr(self):
        return super().extra_repr() + f", ds_bits={self.ds_bits}, rescale={self.ds_rescale}"


class DenseShiftConv2d(DenseShiftMixin, nn.Conv2d):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding=0, bias=False, bits=3,
                 rescale=True, init="lowvar", init_std=1e-3, **kw):
        super().__init__(in_channels, out_channels, kernel_size, stride=stride, padding=padding, bias=bias, **kw)
        self._ds_setup(bits, rescale, init, init_std)

    def forward(self, x):
        return self._conv_forward(x, self.ds_weight(), self.bias)


class DenseShiftLinear(DenseShiftMixin, nn.Linear):
    def __init__(self, in_features, out_features, bias=False, bits=3, rescale=True, init="lowvar", init_std=1e-3):
        super().__init__(in_features, out_features, bias=bias)
        self._ds_setup(bits, rescale, init, init_std)

    def forward(self, x):
        return F.linear(x, self.ds_weight(), self.bias)


def decode_codes(codes, bits):
    """Hardware codes -> weight values (float tensor)."""
    codes = torch.as_tensor(codes)
    sign = 1 - 2 * ((codes >> (bits - 1)) & 1)
    exp = codes & ((1 << (bits - 1)) - 1)
    return sign.to(torch.float32) * torch.pow(2.0, exp.to(torch.float32))


# Self-test --------------------------------------------------------------------------------------------------------

def _official_3bit(w_sign, t1, t2, t3):
    """Transcription of the official DenseShiftConv2d3bit weight computation (dsconv2d.py) for the comparison."""
    def b01(x):
        return heaviside(x)
    shift = b01(t1)
    shift = (shift + 1) * b01(t2)
    shift = (shift + 1) * b01(t3)
    s = ste_sign(w_sign)
    bw = s * torch.sqrt(shift + 1)
    w = bw.clone()
    with torch.no_grad():
        w += s * torch.pow(torch.full_like(shift, 2.0), shift) - bw
    return w


def selftest():
    torch.manual_seed(0)
    for bits in (2, 3, 4):
        lin = DenseShiftLinear(64, 32, bits=bits, init_std=1.0)  # wide init: reach every code
        w = lin.ds_weight()
        T = 2 ** (bits - 1) - 1
        allowed = {float(s * 2 ** e) for s in (-1, 1) for e in range(T + 1)}
        assert set(w.detach().unique().tolist()) <= allowed, bits
        codes = lin.export_codes()
        assert codes.min() >= 0 and codes.max() < 2 ** bits
        assert torch.equal(decode_codes(codes, bits), w.detach()), bits
        assert len(codes.unique()) == 2 ** bits, f"{bits}-bit: not every code reached"
    # 3-bit matches the official layer, values and gradients.
    lin = DenseShiftLinear(16, 8, bits=3, init_std=1.0)
    x = torch.randn(5, 16)
    y = lin(x).square().sum()
    y.backward()
    g_ours = [lin.weight.grad.clone(), lin.weight_t.grad.clone()]
    ws = lin.weight.detach().clone().requires_grad_()
    ts = [lin.weight_t[i].detach().clone().requires_grad_() for i in range(3)]
    w_off = _official_3bit(ws, *ts)
    assert torch.equal(w_off.detach(), lin.ds_weight().detach())
    F.linear(x, w_off).square().sum().backward()
    assert torch.allclose(ws.grad, g_ours[0]) and all(torch.allclose(ts[i].grad, g_ours[1][i]) for i in range(3))
    # Conv: forward equals conv2d with the decoded weights.
    conv = DenseShiftConv2d(4, 6, 3, padding=1, bits=4, init_std=1.0)
    xc = torch.randn(2, 4, 8, 8)
    ref = F.conv2d(xc, decode_codes(conv.export_codes(), 4), padding=1)
    assert torch.allclose(conv(xc), ref)
    # Low-variance init: parameters ~ N(0, 1e-3).
    lv = DenseShiftLinear(256, 256, bits=3)
    assert abs(lv.weight_t.std().item() - 1e-3) < 1e-4
    print("denseshift selftest: ok (2/3/4-bit codes, official 3-bit values and gradients, conv, init)")


if __name__ == "__main__":
    selftest()
