"""Parse an extracted source-language SRT into the canonical source JSON:
  work/<key>.src.json = [{num, ts, text:[...], speaker, sound, bleep, inaudible}]

Flags drive downstream policy:
  sound     : pure sound/music annotation -> a worker may <DROP> it (builder enforces).
  inaudible : "[inaudible]"/"[indistinct]" -> kept and rendered guardrails.sdh.inaudible_render.
  bleep     : "[bleep]"/"(bleeped)" -> censored-subtitle policy (never invent; recover/neutral).
  speaker   : a leading "NAME:" attribution label (kept as metadata; stripped on display).

Sound detection handles speaker-prefixed, dashed two-part, and UNCLOSED trailing
bracket runs (only when their content matches the sound-word vocabulary), per the
hard-won SDH edge cases.
"""
import os
import re
import sys
import json

import config
import srt_utils

CFG = config.load()
WORK = CFG.work

SPK = re.compile(r"^\s*([A-Z][A-Z'\u2019.\-]{1,20}(?:\s[A-Z][A-Z'\u2019.\-]{1,20})?):\s*")
CLOSED = re.compile(r"[\[(][^\])]*[\])]")
INAUDIBLE = re.compile(r"\[(?:inaudible|indistinct)\]", re.I)
BLEEP = re.compile(r"[\[(](?:bleep|bleeped|censored)[\])]", re.I)
_SOUND_WORDS = CFG.sound_words or set()


def _strip_speaker(line):
    return SPK.sub("", line)


def is_sound(textlines):
    """True iff the cue is purely a sound/music annotation (no spoken words)."""
    body = [l for l in textlines if l.strip()]
    if not body:
        return False
    joined = " ".join(_strip_speaker(l).strip() for l in body)
    joined = re.sub(r"^[-\s]+", "", joined)
    # remove all CLOSED bracket groups; pure sound cue -> nothing left
    stripped = CLOSED.sub("", joined)
    stripped = re.sub(r"[-\s.,!?]+", "", stripped)
    if stripped == "":
        return True
    # handle an UNCLOSED trailing bracket run, but ONLY if its content is a sound word
    m = re.search(r"[\[(]([^\])]*)$", joined)
    if m:
        head = joined[:m.start()]
        head = re.sub(r"[-\s.,!?]+", "", CLOSED.sub("", head))
        words = re.findall(r"[a-záéíóúñ]+", m.group(1).lower())
        if head == "" and words and all(w in _SOUND_WORDS for w in words):
            return True
    return False


def parse(key):
    srt_path = WORK / f"{key}.src.srt"
    if not srt_path.exists():
        raise SystemExit(f"FAIL: {srt_path} not found (run extract.py first)")
    cues, problems = srt_utils.parse_srt(open(srt_path, encoding="utf-8").read(), strict=True)
    out = []
    for c in cues:
        textlines = c["text"]
        speaker = None
        for l in textlines:
            sm = SPK.match(l)
            if sm:
                speaker = sm.group(1)
                break
        joined = " ".join(textlines)
        out.append({
            "num": c["num"],
            "ts": c["ts"],
            "text": textlines,
            "speaker": speaker,
            "sound": is_sound(textlines),
            "inaudible": bool(INAUDIBLE.search(joined)),
            "bleep": bool(BLEEP.search(joined)),
        })
    nums = [c["num"] for c in out]
    if len(set(nums)) != len(nums):
        raise SystemExit(f"FAIL: {key} source has duplicate cue numbers")
    dst = WORK / f"{key}.src.json"
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    n_sound = sum(c["sound"] for c in out)
    print(f"{key}: parsed {len(out)} cues ({n_sound} sound, "
          f"{sum(c['inaudible'] for c in out)} inaudible, {sum(c['bleep'] for c in out)} bleep) -> {dst.name}")
    return out


if __name__ == "__main__":
    for k in sys.argv[1:]:
        parse(k)
