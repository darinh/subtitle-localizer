"""Split <key>.src.json into N contiguous range slices for parallel workers.
Writes work/_ranges/<key>.rK.src.json (same cue schema) and asserts the
concatenation of all slices equals the full ordered cue list (every cue once,
no overlap/gap). Prints each slice's first/last cue number = the worker contract.

  python slice_ranges.py S01E02            # uses slices_per_title from config
  python slice_ranges.py S01E02 5          # override part count
"""
import os
import sys
import json
import math

import config

CFG = config.load()
WORK = CFG.work
RANGES = CFG.ranges


def slice_title(key, nparts=None):
    src = WORK / f"{key}.src.json"
    if not src.exists():
        raise SystemExit(f"FAIL: {src} not found (run extract.py + parse_source.py first)")
    cues = json.load(open(src, encoding="utf-8"))
    n = len(cues)
    if n == 0:
        raise SystemExit(f"FAIL: {key} has 0 cues")
    nparts = min(nparts or CFG.slices_per_title, n)
    RANGES.mkdir(parents=True, exist_ok=True)
    size = math.ceil(n / nparts)
    parts, covered = [], []
    for k in range(nparts):
        chunk = cues[k * size:(k + 1) * size]
        if not chunk:
            continue
        out = RANGES / f"{key}.r{k + 1}.src.json"
        json.dump(chunk, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
        covered += [c["num"] for c in chunk]
        parts.append((k + 1, chunk[0]["num"], chunk[-1]["num"], len(chunk)))
    allnums = [c["num"] for c in cues]
    if covered != allnums:
        raise SystemExit(f"FAIL: coverage mismatch for {key} ({len(covered)} vs {len(allnums)})")
    if len(set(allnums)) != len(allnums):
        raise SystemExit(f"FAIL: {key} source has duplicate cue numbers")
    return parts


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    key = sys.argv[1]
    nparts = int(sys.argv[2]) if len(sys.argv) > 2 else None
    parts = slice_title(key, nparts)
    print(f"{key}: {len(parts)} parts, {sum(p[3] for p in parts)} cues")
    for k, a, b, c in parts:
        print(f"  r{k}: cues {a}..{b}  ({c})  -> _ranges/{key}.r{k}.src.json")
