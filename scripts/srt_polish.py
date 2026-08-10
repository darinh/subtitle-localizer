"""Deterministic clean-up pass for a source-language SRT (typically an ASR draft).

`autofix.py` is the target-side equivalent: it rewrites the consolidated *batch* using
target-language rules. This one works on an SRT and fixes only STRUCTURAL and TIMING
defects — the kind a machine can fix with certainty and a human should never spend a
review pass on:

  * drops empty and punctuation-only cues;
  * collapses a run of consecutive cues with identical text (an ASR stutter loop)
    into one cue spanning the whole run;
  * re-wraps each cue to <= max_lines lines of <= max_cpl chars (dialogue cues that
    already carry a "- " two-speaker layout are left alone);
  * enforces min_duration and pulls CPS toward max_cps by growing a cue into the
    silence that follows it — never past the next cue, so ordering is preserved;
  * removes overlaps, and renumbers sequentially.

It NEVER edits wording. Lines that cannot fit the readability budget need
condensation, which is a judgement call, so they are reported for review instead.

  python srt_polish.py work/the-film.src.srt              # in place (writes .bak)
  python srt_polish.py work/the-film.src.srt -o out.srt
  python srt_polish.py work/the-film.src.srt --dry-run
"""
import os
import re
import sys
import shutil

import config
import srt_utils

CFG = config.load()
PUNCT_ONLY = re.compile(r"^[\W_]+$", re.U)
MIN_GAP = 0.04            # keep a visible frame-ish gap between consecutive cues


def _norm(s):
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


def polish(path, out_path=None, dry_run=False, verbose=True):
    raw = open(path, encoding="utf-8").read()
    cues, problems = srt_utils.parse_srt(raw, strict=False)
    if problems:
        raise SystemExit(f"FAIL: {os.path.basename(str(path))} has {len(problems)} malformed "
                         f"block(s) — fix those first: {problems[:3]}")
    if not cues:
        raise SystemExit(f"FAIL: no cues in {path}")

    r = CFG.readability
    max_cpl = int(r.get("max_cpl", 42))
    max_lines = int(r.get("max_lines", 2))
    max_cps = float(r.get("max_cps", 17))
    min_dur = float(r.get("min_duration", 0.7))

    stats = {"dropped_empty": 0, "collapsed_loops": 0, "rewrapped": 0,
             "extended_short": 0, "cps_relieved": 0, "overlaps_fixed": 0}
    over_budget = []

    # --- 1. load into a working list, dropping empties -----------------------
    items = []
    for c in cues:
        st, en = srt_utils.cue_bounds(c["ts"])
        text = "\n".join(l for l in c["text"] if l.strip())
        if not text.strip() or PUNCT_ONLY.match(re.sub(r"</?[ib]>", "", text).strip()):
            stats["dropped_empty"] += 1
            continue
        items.append({"start": st, "end": en, "text": text})
    items.sort(key=lambda x: (x["start"], x["end"]))

    # --- 2. collapse consecutive identical cues (ASR stutter loop) ----------
    merged = []
    for it in items:
        if merged and _norm(re.sub(r"</?[ib]>", "", it["text"])) == \
                _norm(re.sub(r"</?[ib]>", "", merged[-1]["text"])):
            merged[-1]["end"] = max(merged[-1]["end"], it["end"])
            stats["collapsed_loops"] += 1
            continue
        merged.append(it)
    items = merged

    # --- 3. re-wrap text to the readability budget --------------------------
    for it in items:
        before = it["text"]
        it["text"] = srt_utils.wrap_cue(before, width=max_cpl)
        if it["text"] != before:
            stats["rewrapped"] += 1
        body = [l for l in it["text"].split("\n") if l.strip()]
        if len(body) > max_lines or any(len(re.sub(r"</?[ib]>", "", l)) > max_cpl for l in body):
            over_budget.append(it)

    # --- 4. timing hygiene: no overlap, min duration, CPS relief ------------
    for i, it in enumerate(items):
        nxt = items[i + 1]["start"] if i + 1 < len(items) else it["end"] + 10.0
        prev_end = items[i - 1]["end"] if i else 0.0
        if it["start"] < prev_end + MIN_GAP:
            it["start"] = prev_end + MIN_GAP
            stats["overlaps_fixed"] += 1
        ceiling = max(nxt - MIN_GAP, it["start"] + 0.05)
        if it["end"] > ceiling:
            it["end"] = ceiling
        if (it["end"] - it["start"]) < min_dur and it["end"] < ceiling:
            it["end"] = min(it["start"] + min_dur, ceiling)
            stats["extended_short"] += 1
        plain = re.sub(r"</?[ib]>", "", it["text"]).replace("\n", " ")
        need = len(plain.replace(" ", "")) / max_cps
        if (it["end"] - it["start"]) < need and it["end"] < ceiling:
            it["end"] = min(it["start"] + need, ceiling)
            stats["cps_relieved"] += 1
    items = [it for it in items if it["end"] > it["start"]]

    rows = [(i, _fmt(it["start"], it["end"]), it["text"]) for i, it in enumerate(items, 1)]

    if verbose:
        name = os.path.basename(str(path))
        print(f"== polish {name}: {len(cues)} -> {len(rows)} cues ==")
        for k, v in stats.items():
            if v:
                print(f"   {k}: {v}")
        if over_budget:
            print(f"   REVIEW {len(over_budget)} cue(s) still over the readability budget "
                  f"(> {max_lines}x{max_cpl}) — these need condensing, not re-wrapping:")
            for it in over_budget[:10]:
                print(f"      at {_fmt(it['start'], it['end']).split(' --> ')[0]} "
                      f"({len(re.sub(r'</?[ib]>', '', it['text']).replace(chr(10), ' '))} chars)")
        if dry_run:
            print("   (dry run — nothing written)")

    if dry_run:
        return rows, stats

    dst = out_path or path
    if str(dst) == str(path):
        shutil.copyfile(path, str(path) + ".bak")
    srt_utils.write_srt(rows, dst)
    reparsed, probs = srt_utils.parse_srt(open(dst, encoding="utf-8").read(), strict=True)
    if len(reparsed) != len(rows) or probs:
        raise SystemExit(f"FAIL: polished output did not re-parse cleanly ({len(reparsed)} vs {len(rows)})")
    if verbose:
        print(f"   wrote {os.path.basename(str(dst))} (re-parse OK)")
    return rows, stats


def _fmt(a, b):
    def one(t):
        t = max(t, 0.0)
        ms = int(round((t - int(t)) * 1000))
        s = int(t)
        if ms == 1000:
            ms, s = 0, s + 1
        return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d},{ms:03d}"
    return f"{one(a)} --> {one(b)}"


if __name__ == "__main__":
    argv = sys.argv[1:]
    out = argv[argv.index("-o") + 1] if "-o" in argv else None
    skip = {out} if out else set()
    args = [a for a in argv if not a.startswith("-") and a not in skip]
    if not args:
        raise SystemExit(__doc__)
    for p in args:
        polish(p, out_path=out, dry_run="--dry-run" in argv)
