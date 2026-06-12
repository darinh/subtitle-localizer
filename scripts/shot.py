"""Grab a video frame at a cue's start time for VISUAL GROUNDING of ambiguous
gender / addressee / on-screen action. Write the finding back into the glossary so
text-only reviewers inherit it.

  python shot.py S01E02 979            # frame at the start of source cue 979
Saves work/<key>.cue<NNNN>.jpg. (NB the hour matters: 01:14:13 is 1h14m, not 14m.)
"""
import os
import re
import sys
import json
import subprocess

import config

CFG = config.load()
WORK = CFG.work


def shot(key, cue_num, width=760):
    src = json.load(open(WORK / f"{key}.src.json", encoding="utf-8"))
    by = {c["num"]: c for c in src}
    if int(cue_num) not in by:
        raise SystemExit(f"FAIL: cue {cue_num} not in {key}.src.json")
    start = re.split(r"\s*-->\s*", by[int(cue_num)]["ts"])[0].replace(",", ".")
    video = CFG.video_for(key)
    if not video or not video.exists():
        raise SystemExit(f"FAIL: no video for {key}")
    out = WORK / f"{key}.cue{int(cue_num):04d}.jpg"
    r = subprocess.run(["ffmpeg", "-y", "-ss", start, "-i", str(video),
                        "-frames:v", "1", "-vf", f"scale={width}:-1", str(out), "-loglevel", "error"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise SystemExit(f"FAIL: ffmpeg frame grab:\n{r.stderr}")
    print(f"{key}: cue {cue_num} @ {start} -> {out}")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    shot(sys.argv[1], sys.argv[2])
