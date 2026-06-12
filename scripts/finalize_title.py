"""Finalize a title: normalize -> autofix -> build -> validate -> align-check ->
ATOMIC deliver next to the video, with transactional state. Works for movie or
series (delivery target = the title's own video file).

Gate chain (any failure stops BEFORE delivery and records why):
  1. normalize_batch  consolidate worker batches (self-heal, overlap guard)
  2. autofix          deterministic config-driven safe fixes
  3. build_srt        assemble SRT + write delivery manifest (fail-loud)
  4. validate_srt     HARD checks must be zero
  5. align_check      no off-by-one drift
  6. deliver          atomic temp -> os.replace next to the video; verify no BOM

  python finalize_title.py S01E02 [S01E03 ...]
"""
import os
import sys
import tempfile

import config
import state_db
import normalize_batch
import autofix
import build_srt
import validate_srt
import align_check

CFG = config.load()
WORK = CFG.work


def finalize(key):
    con = state_db.connect(CFG)
    video = CFG.video_for(key)

    try:
        normalize_batch.normalize(key)
    except SystemExit as e:
        state_db.set_status(con, key, "built", f"normalize: {e}"[:400]); con.close()
        print(key, "NORMALIZE ISSUE:", e); return False

    autofix.autofix(key)

    try:
        _, kept, dropped = build_srt.build(key)
    except SystemExit as e:
        state_db.set_status(con, key, "failed", f"build: {e}"[:400]); con.close()
        print(key, "BUILD FAILED:", e); return False

    hard = validate_srt.validate(key, verbose=False)
    state_db.upsert_title(con, key, n_cues_out=kept)
    if hard > 0:
        state_db.set_status(con, key, "built", f"{hard} HARD validation issues")
        con.close()
        print(f"{key}: built {kept} (dropped {dropped}) but {hard} HARD issues -> NOT delivered")
        return False

    if align_check.check(key) > 0:
        state_db.set_status(con, key, "built", "alignment shift detected")
        con.close()
        print(f"{key}: ALIGNMENT SHIFT -> NOT delivered (re-translate flagged range)")
        return False

    if not video or not video.exists():
        state_db.set_status(con, key, "built", "no video for delivery")
        con.close()
        print(f"{key}: built+validated but no video to deliver beside (check media_root).")
        return False

    data = (WORK / f"{key}{CFG.srt_suffix}").read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        state_db.set_status(con, key, "failed", "BOM in built srt"); con.close()
        raise SystemExit(f"{key}: built SRT has a BOM (should be impossible)")
    dest = video.with_name(video.stem + CFG.srt_suffix)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        os.write(fd, data); os.close(fd)
        os.replace(tmp, dest)              # atomic on the same filesystem
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    state_db.set_status(con, key, "done")
    con.close()
    print(f"{key}: DONE {kept} cues (dropped {dropped}) -> delivered: {dest.name}")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for k in sys.argv[1:]:
        finalize(k)
