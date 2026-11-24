#
# results.py - append-only result store: one JSON object per line (the source of truth) plus a CSV regenerated from it
# with flattened keys (plan section 9). Leading columns are fixed; the rest are sorted, so the CSV is stable.
#

import csv
import json
import os

SCHEMA_VERSION = 2  # 2: element-format columns (dtype, dtype_kind, dtype_bits, ...; docs/notes2)
LEADING = ["schema_version", "campaign", "status", "build_id", "config_hash", "hls_hash", "timestamp", "git_sha",
           "git_dirty", "dtype", "dtype_kind", "dtype_bits", "n", "k", "m", "rep"]


def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


class ResultStore:
    def __init__(self, path_jsonl):
        self.jsonl = path_jsonl
        self.csv = os.path.splitext(path_jsonl)[0] + ".csv"
        os.makedirs(os.path.dirname(path_jsonl), exist_ok=True)

    def rows(self):
        if not os.path.exists(self.jsonl):
            return []
        with open(self.jsonl) as f:
            return [json.loads(l) for l in f if l.strip()]

    def append(self, row):
        row = {"schema_version": SCHEMA_VERSION, **row}
        with open(self.jsonl, "a") as f:
            f.write(json.dumps(row) + "\n")

    def write_csv(self):
        rows = [flatten(r) for r in self.rows()]
        if not rows:
            return
        keys = sorted({k for r in rows for k in r})
        cols = [k for k in LEADING if k in keys] + [k for k in keys if k not in LEADING]
        with open(self.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
