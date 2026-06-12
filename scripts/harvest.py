"""Gate harvesting of a title's worker outputs (makes the harvest discipline executable).

A title is ready to finalize only when, for EVERY slice:
  * the worker output <key>.tgtNN.txt exists,
  * its last cue == the slice's contract last cue (from _ranges/<key>.rK.src.json),
  * the file is BYTE-STABLE (unchanged since the previous harvest observation).

Run it once to record sizes/mtimes, then again (after a short pause, or with --confirm)
to require stability before finalizing. Records per-slice state in pipeline.db.

  python harvest.py S01E02            # observe + report readiness
  python harvest.py S01E02 --confirm  # require byte-stability vs the last observation
"""
import os
import re
import sys
import glob
import json

import config
import state_db

CFG = config.load()
WORK = CFG.work


def _last_cue(path):
    nums = []
    for ln in open(path, encoding="utf-8"):
        m = re.match(r"^(\d+)(?:\s*\.\.\s*(\d+))?\|\|\|", ln)
        if m:
            nums.append(int(m.group(2) or m.group(1)))
    return max(nums) if nums else None


def harvest(key, confirm=False):
    parts = sorted(glob.glob(str(CFG.ranges / f"{key}.r*.src.json")))
    if not parts:
        raise SystemExit(f"FAIL: no slices for {key} (run slice_ranges.py first)")
    con = state_db.connect(CFG)
    ready = True
    for sp in parts:
        k = int(re.search(r"\.r(\d+)\.src\.json$", sp).group(1))
        cues = json.load(open(sp, encoding="utf-8"))
        lo, hi = cues[0]["num"], cues[-1]["num"]
        out = WORK / f"{key}.tgt{k:02d}.txt"
        if not out.exists():
            print(f"  r{k}: MISSING {out.name}")
            state_db.record_slice(con, key, k, lo, hi, status="pending")
            ready = False
            continue
        st = out.stat()
        lc = _last_cue(out)
        row = con.execute("SELECT size, mtime FROM slices WHERE title=? AND part=?",
                          (key, k)).fetchone()
        stable = bool(row) and row[0] == st.st_size and abs((row[1] or 0) - st.st_mtime) < 1e-6
        last_ok = (lc == hi)
        if not last_ok:
            flag = f"LAST-CUE {lc}!={hi}"
        elif confirm and not stable:
            flag = "not byte-stable yet"
        else:
            flag = "READY"
        if flag != "READY":
            ready = False
        state_db.record_slice(con, key, k, lo, hi, size=st.st_size, mtime=st.st_mtime,
                              status="written" if flag == "READY" else "dispatched")
        print(f"  r{k}: cues {lo}..{hi}  last={lc}  {flag}")
    con.close()
    print(f"{key}: {'ALL SLICES READY -> finalize' if ready else 'NOT READY'}")
    return ready


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    confirm = "--confirm" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    ok = all(harvest(k, confirm) for k in args)
    sys.exit(0 if ok else 1)
