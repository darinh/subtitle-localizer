"""Consolidate per-worker target batch files (work/<key>.tgtNN.txt) into one
work/<key>.tgt01.txt, self-healing recurring worker/editor output bugs and
guarding against stale/overlapping partials.

Self-heal (sanitizer): a UTF-8 BOM anywhere; a literal "\\n" or PowerShell
backtick escape (`n / `r`n / `r) instead of a real line break; double-encoded
UTF-8 (mojibake). Merges a worker's mis-prefixed continuation lines (same cue
number repeated consecutively in ONE file). Treats the same cue number appearing
in TWO different files as a HARD overlap error (remove stale partials first).
Coverage is checked against <key>.src.json.

  python normalize_batch.py S01E02
"""
import os
import re
import sys
import glob
import json

import config

CFG = config.load()
WORK = CFG.work
CUE = re.compile(r"^(\d+)(?:\s*\.\.\s*(\d+))?\|\|\|(.*)$")   # N|||  or  N..M|||  (merge-aware)


def _sanitize(text):
    text = (text.replace("\ufeff", "").replace("\\n", "\n")
                .replace("`r`n", "\n").replace("`n", "\n").replace("`r", "\n"))
    out = []
    for ln in text.split("\n"):
        if "\u00c3" in ln or "\u00c2" in ln:
            for enc in ("latin-1", "cp1252"):
                try:
                    f = ln.encode(enc).decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
                if f != ln and "\u00c3" not in f and "\u00c2" not in f:
                    ln = f
                    break
        out.append(ln)
    return "\n".join(out)


def normalize(key):
    files = sorted(glob.glob(str(WORK / f"{key}.tgt*.txt")))
    if not files:
        raise SystemExit(f"FAIL: no target batches ({key}.tgt*.txt) for {key}")
    cues = []          # list of [lo, hi, [lines]]
    owner = {}         # cue num -> basename that defined it
    for fn in files:
        base = os.path.basename(fn)
        cur = None     # continuation tracking does NOT cross file boundaries
        for raw in _sanitize(open(fn, encoding="utf-8").read().replace("\r", "")).split("\n"):
            m = CUE.match(raw)
            if m:
                lo = int(m.group(1)); hi = int(m.group(2)) if m.group(2) else lo; text = m.group(3)
                # mis-prefixed continuation: a worker repeats `N|||` for cue N's later lines
                if cur is not None and lo == cur[0] and hi == cur[0] and cur[1] == cur[0]:
                    cur[2].append(text)
                    continue
                span = list(range(lo, hi + 1))
                for n in span:
                    if n in owner:
                        if owner[n] == base:
                            raise SystemExit(f"FAIL: cue {n} recurs non-consecutively in {base} (real dup)")
                        raise SystemExit(
                            f"FAIL: cue {n} appears in BOTH {owner[n]} and {base} "
                            f"(overlapping/stale batches for {key}); remove stale partials first")
                    owner[n] = base
                if cur is not None:
                    cues.append(cur)
                cur = [lo, hi, [text]]
            elif cur is not None:
                cur[2].append(raw)                      # plain continuation
        if cur is not None:
            cues.append(cur)

    src = json.load(open(WORK / f"{key}.src.json", encoding="utf-8"))
    want = {c["num"] for c in src}
    got = set(owner)
    if want != got:
        raise SystemExit(f"FAIL: coverage off. missing={sorted(want-got)[:10]} extra={sorted(got-want)[:10]}")

    cues.sort(key=lambda c: c[0])     # numeric order (defends against lexicographic glob)
    out, empties = [], []
    for lo, hi, lines in cues:
        head = f"{lo}" if hi == lo else f"{lo}..{hi}"
        while len(lines) > 1 and lines[-1] == "":
            lines.pop()
        if not lines or (len(lines) == 1 and lines[0].strip() == ""):
            empties.append(lo)
            out.append(f"{head}|||<DROP>")  # placeholder; validated against the sound flag
            continue
        out.append(f"{head}|||{lines[0]}")
        out.extend(lines[1:])
    if empties:
        print("WARN empty cues (set <DROP>):", empties)
    for fn in files:
        os.remove(fn)
    (WORK / f"{key}.tgt01.txt").write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print(f"{key}: normalized -> {len(cues)} entries covering {len(got)} cues in one file")


if __name__ == "__main__":
    for k in sys.argv[1:]:
        normalize(k)
