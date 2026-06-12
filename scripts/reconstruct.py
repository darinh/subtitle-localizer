"""Rebuild the consolidated batch (work/<key>.tgt01.txt) from an already-delivered
target SRT — recovery for when a late/zombie worker clobbers a batch file after
harvest/review.

MANIFEST-FIRST (exact, safe under duplicate timestamps): uses <key>.manifest.json
(written by build_srt.py) to map each delivered output cue back to its source cue
number(s). Merged spans round-trip as "N..M|||text"; approved drops as "N|||<DROP>".

If the manifest is absent, falls back to a timestamp two-pointer walk against the
source JSON — which FAILS LOUD on duplicate/ambiguous timestamps rather than
risk misassociating text.

  python reconstruct.py S01E02
After reconstruct, RE-APPLY any reviewer fixes (see apply_patch.py) before re-finalize.
"""
import os
import re
import sys
import json

import config
import srt_utils

CFG = config.load()
WORK = CFG.work


def _delivered_cues(key):
    srt = (WORK / f"{key}{CFG.srt_suffix}").read_text(encoding="utf-8")
    cues, _ = srt_utils.parse_srt(srt, strict=True)
    return cues


def reconstruct(key):
    src = json.load(open(WORK / f"{key}.src.json", encoding="utf-8"))
    order = [c["num"] for c in src]
    cues = _delivered_cues(key)
    man_path = WORK / f"{key}.manifest.json"
    out = {}                # src_num -> emitted line(s)
    cover = set()

    if man_path.exists():
        man = json.load(open(man_path, encoding="utf-8"))
        by_out = {c["num"]: "\n".join(c["text"]) for c in cues}
        for mc in man["cues"]:
            if mc.get("drop"):
                for n in mc["src"]:
                    out[n] = f"{n}|||<DROP>"
                    cover.add(n)
                continue
            text = by_out.get(mc["out"])
            if text is None:
                raise SystemExit(f"FAIL: manifest out-cue {mc['out']} missing from delivered SRT")
            block = mc["src"]
            head = f"{block[0]}" if len(block) == 1 else f"{block[0]}..{block[-1]}"
            out[block[0]] = f"{head}|||{text}"
            for n in block:
                cover.add(n)
        mode = "manifest"
    else:
        # degraded fallback: match each source cue's ts to a delivered cue's ts
        dts = [c["ts"] for c in cues]
        if len(set(dts)) != len(dts):
            raise SystemExit("FAIL: delivered SRT has duplicate timestamps; cannot reconstruct "
                             "safely without a manifest. Re-translate this title.")
        sts = [c["ts"] for c in src]
        if len(set(sts)) != len(sts):
            raise SystemExit("FAIL: source has duplicate timestamps; cannot reconstruct safely "
                             "without a manifest. Re-translate this title.")
        # canonical droppability (sound cues + reviewed dropok)
        droppable = set()
        if CFG.sdh.get("drop_pure_sound", True):
            droppable |= {c["num"] for c in src if c.get("sound")}
        dp = WORK / f"{key}.dropok.txt"
        if dp.exists():
            for ln in dp.read_text(encoding="utf-8").splitlines():
                mm = re.match(r"^\s*(\d+)", ln)
                if mm:
                    droppable.add(int(mm.group(1)))
        dmap = {c["ts"]: "\n".join(c["text"]) for c in cues}
        for c in src:
            if c["ts"] in dmap:
                out[c["num"]] = f"{c['num']}|||{dmap[c['ts']]}"
            elif c["num"] in droppable:
                out[c["num"]] = f"{c['num']}|||<DROP>"   # legitimately absent (a drop)
            else:
                raise SystemExit(f"FAIL: source cue {c['num']} ({c['ts']}) absent from the delivered "
                                 f"SRT but is NOT droppable — the delivered file is incomplete.")
            cover.add(c["num"])
        mode = "timestamp-walk (degraded)"

    missing = [n for n in order if n not in cover]
    if missing:
        raise SystemExit(f"FAIL: reconstruct coverage gap: {missing[:15]}")
    lines = [out[n] for n in order if n in out]   # one line per output entry (merges keyed by block[0])
    (WORK / f"{key}.tgt01.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{key}: reconstructed {len(order)} cues via {mode} -> {key}.tgt01.txt "
          f"(RE-APPLY reviewer fixes, then re-finalize)")


if __name__ == "__main__":
    for k in sys.argv[1:]:
        reconstruct(k)
