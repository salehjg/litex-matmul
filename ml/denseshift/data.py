#
# data.py - CIFAR-10 and MNIST as uint8 tensors, without torchvision (docs/notes3/denseshift-plan.md, Part A).
#
# Downloads once into build/ml/data/ (git-ignored). CIFAR-10 uses the binary release (fixed-size records, no pickle);
# MNIST the idx files. Returns (x_train, y_train, x_test, y_test): images N x C x H x W uint8, labels int64.
#

import gzip
import hashlib
import os
import tarfile
import urllib.request

import numpy as np
import torch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(ROOT, "build", "ml", "data")

CIFAR_URL = "https://www.cs.toronto.edu/~kriz/cifar-10-binary.tar.gz"
MNIST_URL = "https://ossci-datasets.s3.amazonaws.com/mnist/"
MNIST_FILES = ("train-images-idx3-ubyte.gz", "train-labels-idx1-ubyte.gz",
               "t10k-images-idx3-ubyte.gz", "t10k-labels-idx1-ubyte.gz")


def _fetch(url, path):
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        print(f"downloading {url}", flush=True)
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, path)
    return path


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cifar10():
    path = _fetch(CIFAR_URL, os.path.join(DATA_DIR, "cifar-10-binary.tar.gz"))
    rec = 1 + 3 * 32 * 32
    parts = {"train": [], "test": []}
    with tarfile.open(path, "r:gz") as tar:
        for m in tar.getmembers():
            name = os.path.basename(m.name)
            if not m.isfile() or not name.endswith(".bin") or "batch" not in name:
                continue
            raw = np.frombuffer(tar.extractfile(m).read(), dtype=np.uint8)
            if raw.size % rec:
                raise ValueError(f"{m.name}: size {raw.size} is not a multiple of {rec}")
            parts["test" if name == "test_batch.bin" else "train"].append((name, raw.reshape(-1, rec)))
    out = []
    for split in ("train", "test"):
        arr = np.concatenate([a for _, a in sorted(parts[split])])
        out += [torch.from_numpy(arr[:, 1:].reshape(-1, 3, 32, 32).copy()), torch.from_numpy(arr[:, 0].astype(np.int64))]
    if out[0].shape[0] != 50000 or out[2].shape[0] != 10000:
        raise ValueError(f"CIFAR-10: got {out[0].shape[0]} / {out[2].shape[0]} images")
    return tuple(out)


def _idx(path):
    with gzip.open(path, "rb") as f:
        data = f.read()
    ndim = data[3]
    dims = [int.from_bytes(data[4 + 4 * i: 8 + 4 * i], "big") for i in range(ndim)]
    return np.frombuffer(data, dtype=np.uint8, offset=4 + 4 * ndim).reshape(dims)


def mnist():
    arrs = [_idx(_fetch(MNIST_URL + f, os.path.join(DATA_DIR, "mnist", f))) for f in MNIST_FILES]
    xtr, ytr, xte, yte = arrs
    if xtr.shape != (60000, 28, 28) or xte.shape != (10000, 28, 28):
        raise ValueError(f"MNIST: shapes {xtr.shape} {xte.shape}")
    return (torch.from_numpy(xtr.copy()).unsqueeze(1), torch.from_numpy(ytr.astype(np.int64)),
            torch.from_numpy(xte.copy()).unsqueeze(1), torch.from_numpy(yte.astype(np.int64)))


if __name__ == "__main__":
    for name, fn in (("cifar10", cifar10), ("mnist", mnist)):
        xtr, ytr, xte, yte = fn()
        print(name, tuple(xtr.shape), tuple(xte.shape), "labels", ytr.bincount().tolist())
    print("md5", md5(os.path.join(DATA_DIR, "cifar-10-binary.tar.gz")))
