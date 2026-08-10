"""Deterministic clean-up pass for a source-language SRT (typically an ASR draft).

`autofix.py` is the target-side equivalent: it rewrites the consolidated *batch* using
target-language rules. This one works on an SRT and fixes only STRUCTURAL and TIMING
defects — the kind a machine can fix with certainty and a human should never spend a
review pass on:

  * drops empty and punctuation-only cues;
  * drops ASR boilerplate hallucinations ("Thanks for watching!", "Please subscribe")
    — whole-cue matches only, and only when the family recurs (see asr_artifacts);
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
import asr_artifacts

CFG = config.load()
PUNCT_ONLY = re.compile(r"^[\W_]+$", re.U)
MIN_GAP = 0.04            # keep a visible frame-ish gap between consecutive cues
DUP_GAP = 1.5             # identical text further apart than this is a real repeat


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

    stats = {"dropped_empty": 0, "dropped_hallucination": 0, "collapsed_loops": 0,
             "rewrapped": 0, "extended_short": 0, "cps_relieved": 0,
             "overlaps_fixed": 0}
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

    # --- 2. flag ASR boilerplate hallucinations -----------------------------
    # A whole-cue YouTube outro emitted over music or screaming. These are spread
    # across the runtime, so the adjacency-gated collapse in step 3 cannot see
    # them. They are only FLAGGED here and removed in step 6, after every timing
    # decision has been made: a deleted cue must still act as a blocker, or its
    # neighbour would collapse across the hole it left or stretch into it, and a
    # surviving cue's timing would then depend on what was deleted next to it.
    if asr_artifacts.enabled_for_config(CFG):
        drop, _matches, _counts = asr_artifacts.find(
            [it["text"] for it in items],
            asr_artifacts.patterns_from_config(CFG),
            asr_artifacts.min_repeats_from_config(CFG))
        for i in drop:
            items[i]["halluc"] = True

    # --- 3. collapse consecutive identical cues (ASR stutter loop) ----------
    # Only when they are ADJACENT IN TIME. A line genuinely repeated later in the
    # film is not a loop, and merging it would delete a cue and stretch the
    # survivor across the gap between them.
    merged = []
    for it in items:
        if merged and _norm(re.sub(r"</?[ib]>", "", it["text"])) == \
                _norm(re.sub(r"</?[ib]>", "", merged[-1]["text"])) \
                and (it["start"] - merged[-1]["end"]) <= DUP_GAP:
            merged[-1]["end"] = max(merged[-1]["end"], it["end"])
            stats["collapsed_loops"] += 1
            continue
        merged.append(it)
    items = merged

    # --- 4. re-wrap text to the readability budget --------------------------
    for it in items:
        before = it["text"]
        it["text"] = srt_utils.wrap_cue(before, width=max_cpl)
        if it["text"] != before:
            stats["rewrapped"] += 1
        body = [l for l in it["text"].split("\n") if l.strip()]
        if it.get("halluc"):
            continue          # about to be deleted: never ask a human to review it
        if len(body) > max_lines or any(len(re.sub(r"</?[ib]>", "", l)) > max_cpl for l in body):
            over_budget.append(it)

    # --- 5. timing hygiene: no overlap, min duration, CPS relief ------------
    for i, it in enumerate(items):
        nxt = items[i + 1]["start"] if i + 1 < len(items) else it["end"] + 10.0
        prev_end = items[i - 1]["end"] if i else 0.0
        if it["start"] < prev_end + MIN_GAP:
            it["start"] = prev_end + MIN_GAP
            stats["overlaps_fixed"] += 1
        ceiling = max(nxt - MIN_GAP, it["start"] + 0.05)
        if it["end"] > ceiling:
            it["end"] = ceiling
        # extend in whole milliseconds so the result actually clears min_duration
        # once serialized; float addition lands a hair under and re-flags the cue.
        want = (srt_utils.ms(it["start"]) + srt_utils.ms(min_dur)) / 1000.0
        if it["end"] < want and it["end"] < ceiling:
            it["end"] = min(want, ceiling)
            stats["extended_short"] += 1
        plain = re.sub(r"</?[ib]>", "", it["text"]).replace("\n", " ")
        need = len(plain.replace(" ", "")) / max_cps
        if (it["end"] - it["start"]) < need and it["end"] < ceiling:
            it["end"] = min(it["start"] + need, ceiling)
            stats["cps_relieved"] += 1
    items = [it for it in items if it["end"] > it["start"]]

    # --- 6. now drop the flagged hallucinations ------------------------------
    # Last, so that every timing decision above was made with them still in place
    # and no surviving cue's timing depends on what was removed beside it.
    removed_halluc = [(it["start"], asr_artifacts.plain(it["text"]))
                      for it in items if it.get("halluc")]
    if removed_halluc:
        items = [it for it in items if not it.get("halluc")]
        stats["dropped_hallucination"] = len(removed_halluc)

    rows = [(i, _fmt(it["start"], it["end"]), it["text"]) for i, it in enumerate(items, 1)]

    if verbose:
        name = os.path.basename(str(path))
        print(f"== polish {name}: {len(cues)} -> {len(rows)} cues ==")
        for k, v in stats.items():
            if v:
                print(f"   {k}: {v}")
        if removed_halluc:
            # a deletion is never silent: name EVERY cue that was removed, uncapped
            print(f"   removed {len(removed_halluc)} ASR boilerplate hallucination(s):")
            for st, txt in removed_halluc:
                print(f"      {srt_utils.format_ts(st)}  {txt!r}")
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
    return srt_utils.format_span(a, b)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Deterministic structural + timing clean-up for an SRT. "
                    "Never edits wording.")
    ap.add_argument("paths", nargs="+", help="SRT file(s) to clean up")
    ap.add_argument("-o", "--out", help="write here instead of in place "
                                        "(only valid with a single input)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    a = ap.parse_args()
    if a.out and len(a.paths) > 1:
        ap.error("-o takes a single input file; with several inputs each is "
                 "polished in place (a .bak is kept)")
    for p in a.paths:
        polish(p, out_path=a.out, dry_run=a.dry_run)
