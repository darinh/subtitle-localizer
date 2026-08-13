"""Language-agnostic structural + readability QA for ANY SRT file.

`validate_srt.py` judges a *delivered target* against its source JSON, manifest and
the target-language guardrails — it is deliberately target-specific, so pointing it
at a source-language draft would flag every source word as "leftover source".

This is the complementary tool: it judges an SRT *on its own terms*. Use it on an
ASR draft (transcribe.py) or any incoming SRT to answer "is this fit to ship /
fit to translate from?" before spending a worker pass on it.

HARD (structural, blocks use): BOM; malformed blocks; numbering break; non-monotonic
or inverted timestamps; empty cue; unbalanced <i>/<b>.
SOFT (readability/ASR smells): CPS over target/hard; >max_lines; over-long line;
sub-min duration; cue overlap; adjacent duplicate text (ASR loop); ASR boilerplate
hallucination ("Thanks for watching!" and friends); punctuation-only cue; runaway
ALL-CAPS; suspicious leading/trailing spacing; long unsubtitled gap.

  python srt_qa.py work/the-film.src.srt
  python srt_qa.py work/the-film.src.srt --top 40      # show more examples
"""
import os
import re
import sys

import config
import srt_utils
import asr_artifacts

CFG = config.load()
TAG = re.compile(r"</?([ib])>", re.I)
PUNCT_ONLY = re.compile(r"^[\W_]+$", re.U)
WORDCH = re.compile(r"[^\W\d_]", re.U)


def _plain(cue):
    return re.sub(r"</?[ib]>", "", " ".join(cue["text"])).strip()


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(int(round((p / 100.0) * (len(s) - 1))), len(s) - 1)
    return s[k]


def qa(path, top=15, verbose=True):
    raw, encoding = srt_utils.read_text(path)
    hard, soft = [], []

    if encoding != "utf-8":
        hard.append(
            f"file is {encoding}, not UTF-8 — every accented character displays as "
            f"mojibake in a player that assumes UTF-8 (srt_polish rewrites it)")
    if raw.startswith(srt_utils.BOM):
        hard.append("BOM present (strip it: UTF-8 without BOM)")
    if re.search(r"\\n|`n|`r", raw):
        hard.append("literal newline escape (\\n or backtick) in the text")
    if re.search(r"[\u00c3\u00c2][\u0080-\u00bf\u2018-\u2030\u20ac]", raw):
        hard.append("mojibake detected (double-decoded UTF-8)")

    cues, problems = srt_utils.parse_srt(raw, strict=False)
    if problems:
        hard.append(f"{len(problems)} malformed block(s): {problems[:3]}")
    if not cues:
        hard.append("no cues parsed")
        if verbose:
            _report(path, [], hard, soft, top)
        return len(hard), len(soft)

    r = CFG.readability
    max_cpl = int(r.get("max_cpl", 42))
    max_lines = int(r.get("max_lines", 2))
    max_cps = float(r.get("max_cps", 17))
    hard_cps = float(r.get("hard_cps", 26))
    min_dur = float(r.get("min_duration", 0.7))

    for i, c in enumerate(cues, 1):
        if c["num"] != i:
            hard.append(f"numbering break at position {i} (got {c['num']})")
            break

    cps_vals, cpl_vals, durs = [], [], []
    prev_end, prev_start, prev_plain = None, None, None
    n_over_target = n_over_hard = n_long_line = n_many_lines = n_short = n_overlap = 0
    gaps = []
    for c in cues:
        st, en = srt_utils.cue_bounds(c["ts"])
        if en <= st:
            hard.append(f"cue {c['num']}: end <= start ({c['ts']})")
            continue
        if prev_start is not None and st < prev_start - 1e-6:
            hard.append(f"cue {c['num']}: starts before the previous cue "
                        f"(non-monotonic: {st:.3f}s after {prev_start:.3f}s)")
        prev_start = st
        if prev_end is not None and st < prev_end - 1e-6:
            n_overlap += 1
            soft.append(f"cue {c['num']}: overlaps previous by {prev_end-st:.2f}s")
        if prev_end is not None and st - prev_end > 120:
            gaps.append((c["num"], st - prev_end))
        prev_end = en
        dur = en - st
        durs.append(dur)
        if srt_utils.ms(en) - srt_utils.ms(st) < srt_utils.ms(min_dur):
            n_short += 1
            soft.append(f"cue {c['num']}: duration {dur:.2f}s < {min_dur}s")
        plain = _plain(c)
        if not plain:
            hard.append(f"cue {c['num']}: empty text")
            continue
        if PUNCT_ONLY.match(plain):
            soft.append(f"cue {c['num']}: punctuation-only cue")
        body = [re.sub(r"</?[ib]>", "", l) for l in c["text"] if l.strip()]
        if len(body) > max_lines:
            n_many_lines += 1
            soft.append(f"cue {c['num']}: {len(body)} lines > {max_lines}")
        for l in body:
            cpl_vals.append(len(l))
            if len(l) > max_cpl:
                n_long_line += 1
                soft.append(f"cue {c['num']}: line {len(l)} chars > {max_cpl}")
        cv = len(plain.replace(" ", "")) / max(dur, 0.001)
        cps_vals.append(cv)
        if cv > hard_cps:
            n_over_hard += 1
            soft.append(f"cue {c['num']}: CPS {cv:.0f} > hard {hard_cps:g}")
        elif cv > max_cps:
            n_over_target += 1
            soft.append(f"cue {c['num']}: CPS {cv:.0f} > target {max_cps:g}")

        norm = re.sub(r"[\W_]+", " ", plain.lower()).strip()
        if prev_plain is not None and norm and norm == prev_plain:
            soft.append(f"cue {c['num']}: identical text to previous cue (ASR loop?)")
        prev_plain = norm

        joined = "\n".join(c["text"])
        for tg in ("i", "b"):
            if joined.lower().count(f"<{tg}>") != joined.lower().count(f"</{tg}>"):
                hard.append(f"cue {c['num']}: unbalanced <{tg}> tags")
        letters = WORDCH.findall(plain)
        if len(letters) >= 12 and all(ch.isupper() for ch in letters):
            soft.append(f"cue {c['num']}: ALL-CAPS ({len(letters)} letters)")

    for num, g in gaps[:5]:
        soft.append(f"cue {num}: {g/60:.1f} min with no subtitles before it "
                    "(scene without dialogue, or a dropout — spot-check)")

    # --- ASR boilerplate hallucinations -------------------------------------
    # Scattered across the runtime, so none of the adjacency-gated duplicate
    # checks above can see them. Report EVERY whole-cue match, and say which ones
    # recur often enough for srt_polish to be willing to delete them.
    halluc_pats = asr_artifacts.patterns_from_config(CFG)
    min_reps = asr_artifacts.min_repeats_from_config(CFG)
    strip_on = asr_artifacts.enabled_for_config(CFG)
    drop, matches, _counts = asr_artifacts.find(
        [_plain(c) for c in cues], halluc_pats, min_reps)
    for i in sorted(matches):
        if i not in drop:
            note = f" — below the x{min_reps} repeat threshold, left in place"
        elif strip_on:
            note = " — recurs; srt_polish will remove it"
        else:
            note = " — recurs, but asr.strip_hallucinations is off, so nothing removes it"
        soft.append(f"cue {cues[i]['num']}: ASR boilerplate hallucination "
                    f"{_plain(cues[i])!r}{note}")

    stats = {
        "cues": len(cues),
        "runtime_min": (srt_utils.cue_bounds(cues[-1]["ts"])[1] / 60.0) if cues else 0,
        "cps_p50": _pct(cps_vals, 50), "cps_p90": _pct(cps_vals, 90),
        "cps_max": max(cps_vals) if cps_vals else 0,
        "cpl_p90": _pct(cpl_vals, 90), "cpl_max": max(cpl_vals) if cpl_vals else 0,
        "dur_p50": _pct(durs, 50),
        "over_target_cps": n_over_target, "over_hard_cps": n_over_hard,
        "long_lines": n_long_line, "many_lines": n_many_lines,
        "short": n_short, "overlaps": n_overlap,
        "halluc": len(matches), "halluc_removable": len(drop),
    }
    if verbose:
        _report(path, stats, hard, soft, top)
    return len(hard), len(soft)


def _report(path, stats, hard, soft, top):
    name = os.path.basename(str(path))
    print(f"== QA {name}: HARD={len(hard)} SOFT={len(soft)} ==")
    if stats:
        print(f"   {stats['cues']} cues over {stats['runtime_min']:.1f} min | "
              f"median cue {stats['dur_p50']:.1f}s")
        print(f"   CPS  p50={stats['cps_p50']:.1f} p90={stats['cps_p90']:.1f} "
              f"max={stats['cps_max']:.1f} | over-target {stats['over_target_cps']}, "
              f"over-hard {stats['over_hard_cps']}")
        print(f"   CPL  p90={stats['cpl_p90']:.0f} max={stats['cpl_max']:.0f} | "
              f"long lines {stats['long_lines']}, >max-lines {stats['many_lines']}")
        print(f"   timing: {stats['short']} sub-min-duration, {stats['overlaps']} overlaps")
        if stats.get("halluc"):
            print(f"   ASR boilerplate: {stats['halluc']} hallucinated cue(s), "
                  f"{stats['halluc_removable']} removable by srt_polish")
    for x in hard[:top]:
        print("  HARD -", x)
    if len(hard) > top:
        print(f"  ... +{len(hard)-top} more HARD")
    for x in soft[:top]:
        print("  soft -", x)
    if len(soft) > top:
        print(f"  ... +{len(soft)-top} more soft")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Structural + readability QA for any SRT, judged on its own terms.")
    ap.add_argument("paths", nargs="+", help="SRT file(s) to inspect")
    ap.add_argument("--top", type=int, default=15,
                    help="how many example findings to print per class")
    a = ap.parse_args()
    total_hard = 0
    for p in a.paths:
        h, _ = qa(p, top=a.top)
        total_hard += h
    sys.exit(0 if total_hard == 0 else 1)
