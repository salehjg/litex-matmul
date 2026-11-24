#!/usr/bin/env python3
"""solve_delta.py - which A element was wrong, and how, for each wrong C row-tile of an integer run.
  solve_delta.py <prefix> <bad.txt> N K M BITS TILE_M
<prefix>.a / .b are gemm_runner --dump-inputs files (int64 codes, A N x K, B K x M), bad.txt is --dump-bad output.
For a wrong row i inside M tile t: diff_j = (got - ref) mod 2^W must equal delta * B[k][j] for one k (one corrupted A
element). Prints k, delta, the original and the seen A value, the flipped bits, and whether the seen value equals
another A element (a stale or neighbouring value instead of a bit flip)."""
import collections
import sys

import numpy as np

prefix, badf, N, K, M, W, TM = sys.argv[1], sys.argv[2], *map(int, sys.argv[3:8])
mask = (1 << W) - 1
A = np.fromfile(prefix + ".a", dtype=np.int64).reshape(N, K)
B = np.fromfile(prefix + ".b", dtype=np.int64).reshape(K, M)
Au, Bu = (A & mask).astype(np.uint64), (B & mask).astype(np.uint64)
groups = collections.defaultdict(dict)
for line in open(badf):
    i, j, got, ref = map(int, line.split())
    groups[(i, j // TM)][j] = (got - ref) & mask
mod = np.uint64(mask) if W < 64 else None
stats = collections.Counter()
for (i, t), d in sorted(groups.items()):
    js = np.array(sorted(d))
    diff = np.array([d[j] for j in js], dtype=np.uint64)
    # full tile: entries not in the dump have diff 0
    allj = np.arange(t * TM, min((t + 1) * TM, M))
    full = np.zeros(len(allj), dtype=np.uint64); full[js - t * TM] = diff
    found = []
    for k in range(K):
        bk = Bu[k, allj]
        odd = np.nonzero(bk & np.uint64(1))[0]
        if not len(odd):
            continue
        j0 = odd[0]
        inv = pow(int(bk[j0]), -1, 1 << W)
        delta = (int(full[j0]) * inv) & mask
        if delta and np.all(((bk * np.uint64(delta)) & np.uint64(mask)) == full):
            found.append((k, delta))
    if len(found) != 1:
        stats["unsolved" if not found else "ambiguous"] += 1
        print(f"row {i} tile {t}: {len(d)} bad, {'no single-element solution' if not found else f'{len(found)} solutions'}")
        continue
    k, delta = found[0]
    a0 = int(Au[i, k]); a1 = (a0 + delta) & mask; flip = a0 ^ a1
    others = [(ii, kk) for ii, kk in zip(*np.nonzero(Au == np.uint64(a1)))][:3]
    stats["solved"] += 1
    print(f"row {i} (PE {i % 32}, lane {i % 16}) tile {t}: k={k} A {a0:0{W//4}x} -> {a1:0{W//4}x} flip {flip:0{W//4}x} "
          f"bits {[b for b in range(W) if flip >> b & 1]}  seen value also at {others}")
print(dict(stats))
