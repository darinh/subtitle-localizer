"""Canonical policy for ASR *boilerplate hallucinations* — the one place that
decides what counts as one, so the transcriber, the QA report and the polish pass
can never disagree about it.

WHAT THIS CATCHES (and why the existing loop guards do not)
-----------------------------------------------------------
`transcribe._collapse_loops` and `srt_polish`'s duplicate collapse both handle a
decoder *stutter*: the same token or n-gram emitted back-to-back with no silence
between. A boilerplate hallucination is a different failure with a different
shape: over music, screaming or near-silence the model falls back on phrasing that
saturated its training data — YouTube outros such as "Thanks for watching!",
"Please subscribe", "Subtitles by ..." — and emits it as a lone, well-formed cue.
Those cues are scattered across the whole runtime, seconds or minutes apart, so
every adjacency-gated guard steps right over them. Measured on one 88-minute film:
27 of 964 cues (2.8%) were "Thanks for watching!", and the film's two professional
subtitle tracks (1908 cues between them) contained the phrase exactly zero times.

THE RULE (deliberately conservative — this deletes dialogue if it is wrong)
--------------------------------------------------------------------------
A cue is a hallucination only when BOTH hold:

  1. its ENTIRE text matches a boilerplate pattern — a cue that merely *contains*
     the phrase is real dialogue wrapped around it and is never touched; and
  2. that pattern family recurs at least `min_repeats` times in the same file.

Condition 2 is what protects a character who genuinely says "Thanks for watching"
once, in a film about a video producer. A person says it; a decoder falls back to
it over and over. Detection is reported at ANY count (`find` returns every whole-cue
match, so nothing is hidden) but removal needs the repeat threshold — report broadly,
delete conservatively.

Counting is per pattern FAMILY rather than per exact string, so a model that varies
its own boilerplate ("Thanks for watching!" / "Thank you for watching.") is still
caught; the families below are narrow enough that this cannot pool unrelated lines.
"""
import re

# Whole-cue patterns, matched case-insensitively against text that has already had
# tags, line breaks and edge punctuation stripped. Keep each one anchored to a
# recognizable boilerplate family — a pattern broad enough to match ordinary
# dialogue would delete it.
DEFAULT_PATTERNS = [
    r"thanks?(?:\s+you)?(?:\s+(?:so|very)\s+much)?\s+for\s+watching"
    r"(?:\s+(?:this|the|my|our)(?:\s+\w+)?\s*(?:video|episode|channel))?",
    r"thanks?(?:\s+you)?\s+for\s+(?:watching|listening|joining\s+(?:me|us))"
    r"[,\s]+(?:and\s+)?(?:i'?ll\s+)?see\s+you\s+.{0,30}",
    r"(?:and\s+)?(?:i'?ll\s+)?see\s+you\s+(?:all\s+|guys\s+)?"
    r"(?:in\s+the\s+next\s+(?:one|video|episode)|next\s+(?:time|video|week))",
    r"(?:please\s+)?(?:don'?t\s+forget\s+to\s+)?"
    r"(?:like[,\s]+(?:comment[,\s]+)?(?:and\s+)?)?subscribe"
    r"(?:\s+to\s+(?:my|our|the)\s+channel)?",
    # attribution credits. The alternation is wrapped so the required "by/from/:"
    # tail applies to every branch — without the group a bare "titles" would match.
    r"(?:sub(?:titles?|titling)|titles?|captions?|transcriptions?|translations?)"
    r"\s*(?::|\bby\b|\bfrom\b|\bcreated\s+by\b|\bprovided\s+by\b)\s*.{0,60}",
    r".{0,40}\bamara\.org\b.{0,40}",
    r".{0,40}\bwww\.\S+",
    r"(?:\u00a9|\(c\)|copyright)\s+.{0,60}",
]

_TAGS = re.compile(r"</?[ib]>", re.I)
_WS = re.compile(r"\s+")
# quotes/brackets/dashes and terminal punctuation a hallucination may be dressed in
_EDGE = "\"'\u201c\u201d\u2018\u2019()[]{}<>-\u2013\u2014.,!?;:\u2026\u00a1\u00bf* \t"


def plain(text):
    """Cue text -> single-line, tag-free, whitespace-collapsed string."""
    return _WS.sub(" ", _TAGS.sub("", str(text)).replace("\n", " ")).strip()


def _candidate(text):
    """The string a whole-cue pattern is matched against."""
    return plain(text).strip(_EDGE).strip()


def compile_patterns(extra=None, replace=False):
    """Compile the boilerplate patterns. `extra` (project config) is appended to the
    built-ins, or replaces them entirely when `replace` is true."""
    pats = list(extra or []) if replace else DEFAULT_PATTERNS + list(extra or [])
    return [re.compile(p, re.I | re.U) for p in pats]


def patterns_from_config(cfg):
    """Compile using a loaded project config's `asr:` section."""
    a = getattr(cfg, "asr", {}) or {}
    return compile_patterns(a.get("hallucination_extra_patterns") or [],
                            bool(a.get("hallucination_patterns_replace", False)))


def min_repeats_from_config(cfg, default=3):
    a = getattr(cfg, "asr", {}) or {}
    try:
        return max(1, int(a.get("hallucination_min_repeats", default)))
    except (TypeError, ValueError):
        return default


def enabled_for_config(cfg):
    a = getattr(cfg, "asr", {}) or {}
    return bool(a.get("strip_hallucinations", True))


def match_family(text, patterns):
    """Index of the first pattern that matches the WHOLE cue, else None."""
    cand = _candidate(text)
    if not cand:
        return None
    for i, p in enumerate(patterns):
        if p.fullmatch(cand):
            return i
    return None


def find(texts, patterns=None, min_repeats=3):
    """Classify a whole file's cue texts in ONE pass.

    Returns (drop, matches, counts):
      drop     - set of indices meeting BOTH conditions (safe to delete)
      matches  - {index: family_index} for every whole-cue boilerplate match,
                 including those below the repeat threshold (report these)
      counts   - {family_index: occurrences in this file}
    """
    if patterns is None:
        patterns = compile_patterns()
    matches = {}
    for i, t in enumerate(texts):
        fam = match_family(t, patterns)
        if fam is not None:
            matches[i] = fam
    counts = {}
    for fam in matches.values():
        counts[fam] = counts.get(fam, 0) + 1
    drop = {i for i, fam in matches.items() if counts[fam] >= min_repeats}
    return drop, matches, counts


if __name__ == "__main__":
    pats = compile_patterns()
    halluc = ["Thanks for watching!", "Thank you for watching.",
              "thanks for watching", "Please subscribe to my channel",
              "Subtitles by Amara.org", "Subtitles: J. Doe",
              "See you in the next video!", "www.foo.com",
              "\u00a9 2010 Some Studio", "<i>Thanks for watching!</i>"]
    keep = ["Thanks for watching my back out there.",
            "I don't know.", "Help!", "Watching. Just watching.",
            "Thanks.", "Subscribe to the theory that we're all doomed.",
            "He said thanks for watching and then he shot him."]
    for t in halluc:
        assert match_family(t, pats) is not None, f"missed: {t!r}"
    for t in keep:
        assert match_family(t, pats) is None, f"false positive: {t!r}"
    # repeat threshold: one genuine utterance survives, a recurring one does not
    drop, matches, _ = find(["Thanks for watching!", "Hello."], pats, min_repeats=3)
    assert drop == set() and set(matches) == {0}
    drop, _, _ = find(["Thanks for watching!", "Hi.", "Thank you for watching.",
                       "Bye.", "thanks for watching"], pats, min_repeats=3)
    assert drop == {0, 2, 4}, drop
    print("asr_artifacts self-test OK:", len(pats), "patterns")
