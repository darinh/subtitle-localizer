"""Assemble the target SRT from the consolidated batch + source JSON.

Guarantees (fail loud on any violation):
  * reattaches EXACT source timestamps (byte-identical);
  * canonical DROP policy (shared with validator/reconstruct): a source cue may be
    absent ONLY if it is a sound cue (and guardrails.sdh.drop_pure_sound) OR its
    number is in <key>.dropok.txt; <DROP> on anything else is rejected;
  * rejects blank lines inside a cue; auto-wraps to <=2 lines;
  * OPTIONAL adjacent-cue merge (features.allow_merge): a batch entry "N..M|||text"
    spans source timestamps start(N)..end(M) (helps CPS on rapid short cues);
  * after writing, RE-PARSES its own output and asserts kept-count + timestamp
    subsequence;
  * writes an immutable delivery MANIFEST (<key>.manifest.json) mapping each output
    cue to its source cue number(s), timestamp, and source/target text hashes — the
    authoritative record for reconstruct.py and apply_patch.py.

  python build_srt.py S01E02 [S01E03 ...]
"""
import os
import re
import sys
import json
import glob
import hashlib

import config
import srt_utils

CFG = config.load()
WORK = CFG.work
DROP = "<DROP>"
CUE = re.compile(r"^(\d+)(?:\s*\.\.\s*(\d+))?\|\|\|(.*)$")   # N|||  or  N..M|||


def _hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def parse_batches(paths):
    """Return ordered list of entries [{lo, hi, text}]. Detects duplicate coverage."""
    entries = []
    for fn in paths:
        cur = None
        def store(_fn=fn):
            if cur is not None:
                while cur["lines"] and cur["lines"][-1] == "":
                    cur["lines"].pop()
                entries.append({"lo": cur["lo"], "hi": cur["hi"],
                                "text": "\n".join(cur["lines"])})
        raw = srt_utils.normalize(open(fn, encoding="utf-8").read())
        for line in raw.split("\n"):
            m = CUE.match(line)
            if m:
                store()
                lo = int(m.group(1)); hi = int(m.group(2)) if m.group(2) else lo
                cur = {"lo": lo, "hi": hi, "lines": [m.group(3)]}
            elif cur is not None:
                cur["lines"].append(line)
        store()
    return entries


def build(key, out_path=None):
    src = json.load(open(WORK / f"{key}.src.json", encoding="utf-8"))
    by_num = {c["num"]: c for c in src}
    order = [c["num"] for c in src]              # source order (positions)
    pos = {n: i for i, n in enumerate(order)}
    if len(by_num) != len(src):
        raise SystemExit("FAIL: source has duplicate cue numbers")

    droppable = set()
    drop_reason = {}
    if CFG.sdh.get("drop_pure_sound", True):
        for c in src:
            if c.get("sound"):
                droppable.add(c["num"])
                drop_reason[c["num"]] = "sound"
    dropok_path = WORK / f"{key}.dropok.txt"
    if dropok_path.exists():
        for ln in dropok_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(\d+)\s*(?:\|\|\|(.*))?$", ln)   # "NUM" or "NUM|||reason"
            if m:
                n = int(m.group(1))
                droppable.add(n)
                drop_reason[n] = (m.group(2) or "dropok").strip()

    paths = sorted(glob.glob(str(WORK / f"{key}.tgt*.txt")))
    if not paths:
        raise SystemExit(f"FAIL: no batch files for {key}")
    entries = parse_batches(paths)

    allow_merge = bool(CFG.features.get("allow_merge", False))
    covered, seen = [], {}
    for e in entries:
        lo, hi = e["lo"], e["hi"]
        if hi != lo and not allow_merge:
            raise SystemExit(f"FAIL: merged entry {lo}..{hi} but features.allow_merge is false")
        if lo not in pos or hi not in pos:
            raise SystemExit(f"FAIL: entry {lo}..{hi} references unknown source cue")
        if pos[hi] < pos[lo]:
            raise SystemExit(f"FAIL: entry {lo}..{hi} is reversed")
        block = order[pos[lo]:pos[hi] + 1]
        if hi != lo and block != list(range(lo, hi + 1)):
            # merged range must be contiguous source cue numbers
            raise SystemExit(f"FAIL: merged entry {lo}..{hi} is not a contiguous source block {block}")
        if hi != lo:
            spk = {by_num[n].get("speaker") for n in block}
            if len(spk) > 1:
                raise SystemExit(f"FAIL: merged entry {lo}..{hi} spans different speakers {spk} "
                                 f"(merge only ADJACENT SAME-speaker cues)")
        for n in block:
            if n in seen:
                raise SystemExit(f"FAIL: source cue {n} covered twice (entries {seen[n]} and {lo}..{hi})")
            seen[n] = f"{lo}..{hi}"
            covered.append(n)
    missing = [n for n in order if n not in seen]
    if missing:
        raise SystemExit(f"FAIL: unaccounted source cues: {missing[:15]}")

    built, manifest, kept, dropped = [], [], 0, 0
    for e in sorted(entries, key=lambda x: pos[x["lo"]]):
        lo, hi = e["lo"], e["hi"]
        t = e["text"].strip()
        block = order[pos[lo]:pos[hi] + 1]
        if t == DROP:
            if hi != lo:
                raise SystemExit(f"FAIL: <DROP> cannot span a merged range {lo}..{hi}")
            if lo not in droppable:
                raise SystemExit(
                    f"FAIL: cue {lo} <DROP> but it is NOT a sound cue / not in dropok "
                    f"(real dialogue?). Add to {dropok_path.name} with a reason if intentional.")
            dropped += 1
            manifest.append({"src": [lo], "drop": True, "reason": drop_reason.get(lo, "sound")})
            continue
        if t == "":
            raise SystemExit(f"FAIL: empty translation for cue {lo} (use {DROP} only for sound cues)")
        body = e["text"].strip("\n")
        if any(line.strip() == "" for line in body.split("\n")):
            raise SystemExit(f"FAIL: cue {lo} has a blank line inside it (would corrupt the SRT)")
        body = srt_utils.wrap_cue(body, width=int(CFG.readability.get("max_cpl", 42)))
        if len([l for l in body.split("\n") if l.strip()]) > int(CFG.readability.get("max_lines", 2)):
            raise SystemExit(f"FAIL: cue {lo} has too many display lines after wrapping")
        start = srt_utils.cue_bounds(by_num[lo]["ts"])[0]
        ts = by_num[lo]["ts"] if hi == lo else \
            re.split(r"\s*-->\s*", by_num[lo]["ts"])[0] + " --> " + re.split(r"\s*-->\s*", by_num[hi]["ts"])[1]
        kept += 1
        built.append((kept, ts, body))
        src_text = " ".join(" ".join(by_num[n]["text"]) for n in block)
        manifest.append({"out": kept, "src": block, "ts": ts,
                         "src_hash": _hash(src_text), "tgt_hash": _hash(body), "drop": False})

    out_path = out_path or WORK / f"{key}{CFG.srt_suffix}"
    srt_utils.write_srt(built, out_path)

    # ---- post-write proof: re-parse + timestamp subsequence ----
    reparsed, problems = srt_utils.parse_srt(open(out_path, encoding="utf-8").read(), strict=True)
    if len(reparsed) != kept:
        raise SystemExit(f"FAIL: re-parsed {len(reparsed)} != kept {kept} (output corrupt)")
    want_ts = [m["ts"] for m in manifest if not m["drop"]]
    for got, w in zip(reparsed, want_ts):
        if got["ts"] != w:
            raise SystemExit(f"FAIL: timestamp mismatch at output cue {got['num']}: {got['ts']} != {w}")

    json.dump({"key": key, "schema": 1, "kept": kept, "dropped": dropped,
               "allow_merge": allow_merge,
               "config_version": _hash(open(CFG.path, encoding="utf-8").read()),
               "cues": manifest},
              open(WORK / f"{key}.manifest.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    print(f"{key}: built {kept} cues (dropped {dropped}); re-parse OK, timestamps identical -> {os.path.basename(str(out_path))}")
    return out_path, kept, dropped


if __name__ == "__main__":
    for ep in sys.argv[1:]:
        build(ep)
