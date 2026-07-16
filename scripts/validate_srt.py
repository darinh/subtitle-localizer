"""Validate a target SRT against its source JSON and the (config-driven) guardrails.

All language data comes from config (project.yaml + guardrails/<target>.yaml) — nothing
is hardcoded. HARD issues block delivery; soft issues are reported.

HARD: BOM; mojibake; malformed blocks; numbering break; banned term (with context
exemptions); leftover source-language (after stripping <i> spans + per-cue engok +
kept loanwords); unbalanced/added/dropped formatting tags vs source (per manifest);
canonical DROP violation (a non-droppable source cue missing from output); output
timestamps not an ordered subsequence of source.
SOFT: >max_lines; over-long line; CPS over hard_cps; overlaps; sub-min duration;
leftover SPEAKER:/[sound] tags; missing inaudible marker; per-cue digit-count drift.

  python validate_srt.py S01E02 [S01E03 ...]
"""
import os
import re
import sys
import json

import config
import srt_utils

CFG = config.load()
WORK = CFG.work
SPK = re.compile(r"(?m)^\s*[A-Z][A-Z'\u2019.\- ]{1,20}:\s")
SOUNDTAG = re.compile(r"\[[^\]]+\]")
ITAL = re.compile(r"<i>.*?</i>", re.S)
TAG = re.compile(r"</?([ib])>", re.I)
FAMILY_CTX = {"mi", "tu", "su", "sus", "mis", "tus", "nuestro", "nuestra", "nuestros",
              "nuestras", "el", "la", "los", "las", "un", "una"}
VALER_AFTER = ("la", "mucho", "poco", "nada", "más", "menos", "otra", "cada",
               "cualquier", "que", "pena")
VALER_BEFORE = ("más", "te", "le", "les", "se", "nos", "me")


def cps(text, ts):
    a, b = srt_utils.cue_bounds(ts)
    return len(text.replace(" ", "")) / max(b - a, 0.1)


def _exempt(term_sense, plain, m):
    """True if a context-exempt term occurrence is in an allowed sense."""
    before = plain[:m.start()].rstrip().rsplit(" ", 1)[-1].lower().strip(",.¡!¿?")
    after = plain[m.end():].lstrip()
    after_word = after.split(" ")[0].lower().strip(",.¡!¿?") if after else ""
    if term_sense == "family":
        return before in FAMILY_CTX or after[:1].isupper()
    if term_sense == "valer":
        return after_word in VALER_AFTER or before in VALER_BEFORE
    return False


def _tagcount(s):
    return tuple(sorted(t.lower() for t in TAG.findall(s)))


def validate(key, verbose=True, standalone=False):
    srt_path = WORK / f"{key}{CFG.srt_suffix}"
    src_path = WORK / f"{key}.src.json"
    man_path = WORK / f"{key}.manifest.json"
    raw = open(srt_path, encoding="utf-8").read()
    hard, soft = [], []

    engok = {}
    ep = WORK / f"{key}.engok.txt"
    if ep.exists():
        for ln in ep.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            m = re.match(r"^\s*(\d+)\s*\|\|\|(.+)$", ln)
            if not m:
                print(f"   WARN engok line not scoped 'NUM|||phrase', ignored: {ln.strip()!r}")
                continue
            engok.setdefault(int(m.group(1)), []).append(m.group(2).strip())

    enc = CFG.encoding
    if enc.get("utf8_no_bom", True) and raw.startswith(srt_utils.BOM):
        hard.append("BOM present")
    if enc.get("forbid_mojibake", True):
        moji = re.search(r"[\u00c3\u00c2][\u0080-\u00bf\u2018-\u2030\u20ac]", raw)
        if moji:
            hard.append(f"mojibake detected (e.g. {moji.group()!r})")
    if enc.get("forbid_literal_newline_escapes", True) and re.search(r"\\n|`n|`r", raw):
        hard.append("literal newline escape (\\n or backtick) present")

    cues, problems = srt_utils.parse_srt(raw, strict=False)
    if problems:
        hard.append(f"{len(problems)} malformed block(s): {problems[:3]}")

    max_lines = int(CFG.readability.get("max_lines", 2))
    max_cpl = int(CFG.readability.get("max_cpl", 42))
    max_cps = float(CFG.readability.get("max_cps", 17))
    hard_cps = float(CFG.readability.get("hard_cps", 26))
    min_dur = float(CFG.readability.get("min_duration", 0.7))

    prev_end = -1.0
    for i, c in enumerate(cues, 1):
        if c["num"] != i:
            hard.append(f"numbering break at position {i} (got {c['num']})")
            break
    for c in cues:
        st, en_ = srt_utils.cue_bounds(c["ts"])
        if st < prev_end - 0.05:
            soft.append(f"cue {c['num']}: overlaps previous")
        if (en_ - st) < min_dur:
            soft.append(f"cue {c['num']}: duration {en_-st:.2f}s < {min_dur}s (timestamp suspect)")
        prev_end = en_
        body = [re.sub(r"</?[ib]>", "", l) for l in c["text"]]
        plain = " ".join(body).strip()
        if len([l for l in c["text"] if l.strip()]) > max_lines:
            soft.append(f"cue {c['num']}: >{max_lines} lines")
        for l in body:
            if len(l) > max_cpl + 3:
                soft.append(f"cue {c['num']}: line >{max_cpl+3} chars ({len(l)})")
        cue_cps = cps(plain, c["ts"])
        if cue_cps > hard_cps:
            soft.append(f"cue {c['num']}: CPS {cue_cps:.0f} > {hard_cps:g} "
                        "(mandatory review; compare to source)")
        elif cue_cps > max_cps:
            soft.append(f"cue {c['num']}: CPS {cue_cps:.0f} > target {max_cps:g} "
                        "(review; compare to source)")
        # banned (always)
        if CFG.banned_regex:
            mm = CFG.banned_regex.search(plain)
            if mm:
                hard.append(f"cue {c['num']}: BANNED '{mm.group().strip()}'")
        # context-exempt terms
        for ce in CFG.context_exempt:
            for m in re.finditer(r"\b(?:" + ce["term"] + r")\b", plain, re.I):
                if not _exempt(ce.get("sense"), plain, m):
                    hard.append(f"cue {c['num']}: BANNED(context) '{m.group()}'")
                    break
        # caution: ambiguous homographs -> soft flag (verify sense), never a hard block
        if CFG.caution_regex:
            cm = CFG.caution_regex.search(plain)
            if cm:
                soft.append(f"cue {c['num']}: CAUTION ambiguous term '{cm.group()}' (verify es-419 sense)")
        # leftover source-language
        scan = ITAL.sub("", " ".join(c["text"]))
        for w in CFG.kept_loanwords:
            scan = re.sub(r"\b" + re.escape(w) + r"\b", "", scan, flags=re.I)
        for phrase in engok.get(c["num"], ()):
            scan = re.sub(re.escape(phrase), "", scan, flags=re.I)
        if CFG.leftover_regex:
            m = CFG.leftover_regex.search(scan)
            if m:
                hard.append(f"cue {c['num']}: leftover source-language '{m.group()}'")
        # tag balance within target cue
        joined = "\n".join(c["text"])
        for tg in ("i", "b"):
            if joined.lower().count(f"<{tg}>") != joined.lower().count(f"</{tg}>"):
                hard.append(f"cue {c['num']}: unbalanced <{tg}> tags")
    if SPK.search(raw):
        soft.append("leftover SPEAKER: label(s) present")
    if SOUNDTAG.search(re.sub(re.escape(CFG.sdh.get("inaudible_render", "")), "", raw)):
        soft.append("leftover [sound] tag(s)")

    # ---- source cross-check (canonical drop + coverage + tags/anchors) ----
    if src_path.exists():
        src = json.load(open(src_path, encoding="utf-8"))
        by_num = {c["num"]: c for c in src}
        dropok = set()
        dp = WORK / f"{key}.dropok.txt"
        if dp.exists():
            for ln in dp.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^\s*(\d+)", ln)   # leading int only; "NUM|||reason" allowed
                if m:
                    dropok.add(int(m.group(1)))
        drop_sound = CFG.sdh.get("drop_pure_sound", True)

        def is_droppable(num):
            c = by_num.get(num, {})
            return bool((c.get("sound") and drop_sound) or num in dropok)

        if man_path.exists():
            # MANIFEST-FIRST (exact; handles 1:1 and merged spans): every source cue is
            # either covered by exactly one output cue or an approved drop.
            man = json.load(open(man_path, encoding="utf-8"))
            out_cues = {c["num"]: "\n".join(c["text"]) for c in cues}
            n_nondrop = sum(1 for mc in man.get("cues", []) if not mc.get("drop"))
            if len(cues) != n_nondrop:
                hard.append(f"delivered SRT has {len(cues)} cues but manifest expects "
                            f"{n_nondrop} (truncated/extra — cue lost?)")
            covered, dropped_nums = {}, set()
            for mc in man.get("cues", []):
                if mc.get("drop"):
                    for n in mc["src"]:
                        dropped_nums.add(n)
                    continue
                if mc["out"] not in out_cues:
                    hard.append(f"manifest out-cue {mc['out']} missing from delivered SRT (lost cue)")
                    continue
                for n in mc["src"]:
                    if n in covered:
                        hard.append(f"source cue {n} covered by 2 output cues")
                    covered[n] = mc["out"]
                oc = next((c for c in cues if c["num"] == mc["out"]), None)
                if oc and oc["ts"] != mc["ts"]:
                    hard.append(f"cue {mc['out']}: timestamp {oc['ts']} != manifest {mc['ts']}")
            for c in src:
                n = c["num"]
                if n in covered:
                    continue
                if n in dropped_nums:
                    if not is_droppable(n):
                        hard.append(f"cue {n} dropped but NOT droppable (sound/dropok) — real dialogue?")
                else:
                    hard.append(f"source cue {n} unaccounted (neither translated nor dropped)")
            # per-cue tag/anchor checks
            for mc in man.get("cues", []):
                if mc.get("drop"):
                    continue
                o = out_cues.get(mc["out"], "")
                src_text = " ".join(" ".join(by_num[n]["text"]) for n in mc["src"] if n in by_num)
                if _tagcount(o) != _tagcount(src_text):
                    hard.append(f"cue {mc['out']}: formatting-tag set differs from source")
                sd = len(re.findall(r"\d+", src_text))
                od = len(re.findall(r"\d+", re.sub(r"</?[ib]>", "", o)))
                if sd and abs(sd - od) >= 2:
                    soft.append(f"cue {mc['out']}: digit-count {od} vs source {sd} (check numbers)")
                if any(by_num[n].get("inaudible") for n in mc["src"]) \
                        and CFG.sdh.get("inaudible_render") not in o:
                    soft.append(f"cue {mc['out']}: source inaudible but no '{CFG.sdh.get('inaudible_render')}'")
                # named-entity anchor: an entity present in the source cue should appear
                # verbatim in the target (soft). Entries may be "Name" or "Source -> Target".
                for ent in CFG.named_entities:
                    s_tok, _, t_tok = (x.strip() for x in str(ent).partition("->"))
                    want = t_tok or s_tok
                    if re.search(r"\b" + re.escape(s_tok) + r"\b", src_text, re.I) \
                            and not re.search(r"\b" + re.escape(want) + r"\b", o, re.I):
                        soft.append(f"cue {mc['out']}: named entity '{want}' expected (from '{s_tok}') but absent")
            if verbose:
                print(f"   source={len(src)} output={len(cues)} dropped={len(dropped_nums)}")
        else:
            # FALLBACK (no manifest, e.g. standalone): timestamps must be an ordered
            # subsequence of the source; only droppable cues may be missing.
            src_ts = [c["ts"] for c in src]
            i, matched = 0, [False] * len(src)
            for c in cues:
                while i < len(src_ts) and src_ts[i] != c["ts"]:
                    i += 1
                if i == len(src_ts):
                    hard.append(f"output timestamp not in source order: {c['ts']}")
                    break
                matched[i] = True
                i += 1
            for k, c in enumerate(src):
                if not matched[k] and not is_droppable(c["num"]):
                    hard.append(f"DIALOGUE cue dropped (src #{c['num']} {c['ts']}): "
                                f"{' '.join(c['text'])[:40]}")
            if verbose:
                print(f"   source={len(src)} output={len(cues)} dropped={len(src)-sum(matched)}")
    elif standalone:
        soft.append("standalone mode: no source cross-check")
    else:
        hard.append(f"MISSING {src_path.name} — cannot verify vs source (pass standalone=True to skip)")

    if verbose:
        print(f"== {key}: {len(cues)} cues | HARD={len(hard)} SOFT={len(soft)} ==")
        for x in hard[:40]:
            print("  HARD -", x)
        for x in soft[:25]:
            print("  soft -", x)
    return len(hard)


if __name__ == "__main__":
    total = sum(validate(ep) for ep in sys.argv[1:])
    sys.exit(0 if total == 0 else 1)
