"""Detect off-by-one (or worse) content misalignment between a title's target batch
and its source JSON — drift that structural validation cannot see.

NOTE: this is ONE layer. Deterministic checks prevent structural loss, NOT all
semantic misalignment (short/repeated/similar-length cues can still be swapped).
The adversarial review gate is the semantic backstop; it MUST sample any window
flagged here, plus a stratified sample elsewhere.

Heuristic: a correct translation's per-cue length correlates with the source per-cue
length AT THE SAME index; a shifted region correlates better at offset +/-1. Over a
sliding window we compare residuals of len(tgt[n]) vs k*len(src[n+off]) for off in
{-1,0,+1} and flag windows where a nonzero offset fits clearly better.

  python align_check.py S01E02 [...]      # exit 1 if any title looks shifted
"""
import os
import re
import sys
import json
import glob
import statistics

import config
import srt_utils

CFG = config.load()
WORK = CFG.work
CUE = re.compile(r"^(\d+)(?:\s*\.\.\s*\d+)?\|\|\|(.*)$")


def load_tgt(key):
    es, cur = {}, None
    for fn in sorted(glob.glob(str(WORK / f"{key}.tgt*.txt"))):
        for raw in srt_utils.normalize(open(fn, encoding="utf-8").read()).split("\n"):
            m = CUE.match(raw)
            if m:
                cur = int(m.group(1)); es[cur] = [m.group(2)]
            elif cur is not None:
                es[cur].append(raw)
    return {n: " ".join(v).strip() for n, v in es.items()}


def check(key, win=30, margin=0.18):
    src = {c["num"]: " ".join(c["text"]) for c in json.load(open(WORK / f"{key}.src.json", encoding="utf-8"))}
    tgt = load_tgt(key)
    nums = sorted(n for n in src if n in tgt and tgt[n] not in ("<DROP>", "")
                  and "<DROP>" not in tgt[n] and src[n].strip())
    Len = {n: (len(src[n]), len(tgt[n])) for n in nums}
    ratios = [b / a for n in nums for a, b in [Len[n]] if a > 3]
    k = statistics.median(ratios) if ratios else 1.0
    flagged = []
    for i in range(0, max(len(nums) - win, 0), max(win // 2, 1)):
        window = nums[i:i + win]
        res = {-1: 0.0, 0: 0.0, 1: 0.0}
        for n in window:
            le = Len[n][1]
            for off in (-1, 0, 1):
                m = n + off
                if m in src and len(src[m]) > 0:
                    res[off] += abs(le - k * len(src[m]))
        best = min(res, key=res.get)
        if best != 0 and res[0] > 0 and (res[0] - res[best]) / res[0] > margin:
            flagged.append((window[0], window[-1], best, round((res[0] - res[best]) / res[0], 2)))
    merged = []
    for f in flagged:
        if merged and f[0] <= merged[-1][1] + win and f[2] == merged[-1][2]:
            merged[-1] = (merged[-1][0], f[1], f[2], max(merged[-1][3], f[3]))
        else:
            merged.append(list(f))
    if merged:
        print(f"{key}: POSSIBLE MISALIGNMENT (k={k:.2f}):")
        for a, b, off, imp in merged:
            print(f"   cues {a}-{b}: better at offset {off:+d} (improve {imp:.0%}) -> reviewer must sample")
    else:
        print(f"{key}: alignment OK (k={k:.2f})")
    return 1 if merged else 0


if __name__ == "__main__":
    sys.exit(1 if sum(check(k) for k in sys.argv[1:]) else 0)
