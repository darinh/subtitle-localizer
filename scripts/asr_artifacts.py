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
  2. that exact text recurs at least `min_repeats` times in the same file.

Condition 2 is what protects a character who genuinely says "Thanks for watching"
once, in a film about a video producer. A person says it; a decoder falls back to
it over and over. Detection is reported at ANY count (`find` returns every whole-cue
match, so nothing is hidden) but removal needs the repeat threshold — report broadly,
delete conservatively.

Recurrence is counted per NORMALIZED EXACT TEXT, not per pattern family. Pooling a
family's counts would be more powerful against a model that varies its own wording,
but it lets three *different* real lines that happen to share one pattern delete each
other — and the premise of this whole check is that a starved decoder repeats itself
verbatim, so exact-text counting is both safer and sufficient (it catches every one
of the 27 cues measured above).

Patterns are narrow on purpose. A pattern ending in an open `.{0,N}` wildcard reads
as "this word followed by anything", which is an ordinary sentence: `titles by ...`
matches "Titles by that director are all garbage." Every pattern below therefore
pins its tail to something a line of dialogue does not have — a credit name, a
domain, a year.
"""
import re

# Whole-cue patterns, matched case-insensitively against text that has already had
# tags, line breaks and edge punctuation stripped. Each is anchored to a shape that
# ordinary dialogue does not have; see the note about open wildcards above.
#
# A credit tail: a genuine attribution names someone. `(?-i:...)` restores
# case-sensitivity inside the group — these patterns are compiled with re.I, which
# would otherwise make the [A-Z] class match lowercase and turn this back into
# "any word", the very hole it exists to close.
_CREDIT = (r"(?-i:(?:[A-Z0-9\u00c0-\u00dc][\w.\-']*|\S*\.(?:org|com|net|tv)\S*)"
           r"(?:\s+(?:[A-Z0-9\u00c0-\u00dc][\w.\-']*|and|&|de|van|der|la|el)){0,4})")

DEFAULT_PATTERNS = [
    # "Thanks for watching!" and its close variants, and nothing else.
    r"thanks?(?:\s+you)?(?:\s+(?:so|very)\s+much)?\s+for\s+watching"
    r"(?:\s+(?:this|the|my|our)(?:\s+\w+)?\s*(?:video|episode|channel))?",
    # the outro pair: thanks + an explicitly next-upload sign-off (NOT "see you at
    # dinner" — the tail is pinned to the channel idiom, not left open)
    r"thanks?(?:\s+you)?\s+for\s+(?:watching|listening)[,\s]+(?:and\s+)?(?:i'?ll\s+)?"
    r"see\s+you\s+(?:all\s+|guys\s+)?(?:in\s+the\s+next\s+(?:one|video|episode)"
    r"|next\s+(?:video|episode))",
    # the sign-off alone. Bare "See you next time" is omitted deliberately: it is
    # ordinary dialogue, so only the video/episode forms are treated as boilerplate.
    r"(?:and\s+)?(?:i'?ll\s+)?see\s+you\s+(?:all\s+|guys\s+)?"
    r"(?:in\s+the\s+next\s+(?:one|video|episode)|next\s+(?:video|episode))",
    # subscribe. A bare "Subscribe." is plausible dialogue, so require one of the
    # channel-idiom affordances around it.
    r"(?:please\s+subscribe\s+to\s+(?:my|our|the)\s+channel"
    r"|(?:don'?t\s+forget\s+to\s+)?subscribe\s+to\s+(?:my|our|the)\s+channel"
    r"|don'?t\s+forget\s+to\s+(?:like\s+and\s+)?subscribe"
    r"|like[,\s]+(?:comment[,\s]+)?(?:and\s+)?subscribe)",
    # attribution credits: the tail must NAME someone, which is what separates
    # "Subtitles by Amara.org" from "Subtitles by themselves do not help."
    r"(?:sub(?:titles?|titling)|captions?|transcription|translation)"
    r"\s*(?::|\bby\b|\bfrom\b)\s*" + _CREDIT,
    # the amara.org credit. Either the cue is essentially just the domain, or it is
    # explicitly a subtitle credit — "I found it on amara.org." is neither.
    r"(?:(?:sub(?:titles?|titling)|captions?)\b.{0,40}\bamara\.org\b.{0,30}"
    r"|(?:https?://)?(?:www\.)?amara\.org\S*)",
    # a cue that is essentially just a URL
    r"(?:(?:please\s+)?(?:visit|see|go\s+to|check\s+out)\s+)?(?:https?://)?www\.\S+",
    # a copyright line: pinned to a year, so "Copyright law will not save you" is not
    # boilerplate.
    r"(?:\u00a9|\(c\)|copyright)\s*(?:\u00a9\s*)?\d{4}(?:\s*[-\u2013]\s*\d{4})?"
    r"(?:\s+" + _CREDIT + r")?(?:[.,]?\s*all\s+rights\s+reserved)?",
    # "All rights reserved" only as a notice of its own, optionally after a
    # copyright marker — never buried in a sentence about a contract.
    r"(?:(?:\u00a9|\(c\)|copyright)\s*(?:\d{4})?\s*.{0,30}?[.,]?\s*)?"
    r"all\s+rights\s+reserved",
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


def _candidates(text):
    """Both forms a cue may legitimately take: as written, and with decorative
    wrapping removed. `_EDGE` is a character SET, so stripping "(c) 2010 Studio"
    would eat the leading "(" and hide it from the copyright pattern — matching
    the unstripped form as well keeps both that and "(Thanks for watching!)"."""
    full = plain(text)
    stripped = full.strip(_EDGE).strip()
    return [c for c in dict.fromkeys((stripped, full)) if c]


def compile_patterns(extra=None, replace=False):
    """Compile the boilerplate patterns. `extra` (project config) is appended to the
    built-ins, or replaces them entirely when `replace` is true."""
    extra = _as_pattern_list(extra)
    pats = list(extra) if replace else DEFAULT_PATTERNS + extra
    out = []
    for i, p in enumerate(pats):
        try:
            out.append(re.compile(p, re.I | re.U))
        except re.error as e:
            where = ("asr.hallucination_extra_patterns"
                     if p in extra else "asr_artifacts.DEFAULT_PATTERNS")
            raise SystemExit(
                f"FAIL: invalid ASR hallucination regex in {where} "
                f"(entry {i}): {p!r}\n       {type(e).__name__}: {e}")
    return out


def _as_pattern_list(extra):
    """Validate `hallucination_extra_patterns`. A bare string is the likely mistake
    and must NOT be accepted: list("abc") is ['a','b','c'], which would compile a
    pattern per character and delete every one-letter cue in the file."""
    if extra is None:
        return []
    if isinstance(extra, str):
        raise SystemExit(
            "FAIL: asr.hallucination_extra_patterns must be a LIST of regexes, "
            f"not a single string ({extra!r}). Write it as:\n"
            "         hallucination_extra_patterns:\n"
            f"           - '{extra}'")
    if not isinstance(extra, (list, tuple)):
        raise SystemExit("FAIL: asr.hallucination_extra_patterns must be a list of "
                         f"regex strings (got {type(extra).__name__})")
    bad = [x for x in extra if not isinstance(x, str)]
    if bad:
        raise SystemExit("FAIL: asr.hallucination_extra_patterns entries must be "
                         f"strings; got {bad[:3]}")
    return list(extra)


def _section(cfg):
    """The project config's `asr:` mapping, validated.

    A missing section is fine (defaults apply). Anything else that is not a mapping
    is a mistake and must fail loud rather than be coerced to {}: `asr: false` reads
    to a human as "turn this off", and silently defaulting it would instead turn
    deletion ON.
    """
    a = getattr(cfg, "asr", None)
    if a is None or a == {}:
        return {}
    if not isinstance(a, dict):
        raise SystemExit(
            f"FAIL: the project config's `asr:` section must be a mapping, got "
            f"{a!r}. To turn the filter off write:\n"
            "         asr:\n           strip_hallucinations: false")
    return a


def _bool_opt(section, key, default):
    """A strict boolean. YAML's `false` is a bool, but a QUOTED \"false\" is a
    truthy string — accepting it would silently enable deletion."""
    if key not in section:
        return default
    v = section[key]
    if isinstance(v, bool):
        return v
    raise SystemExit(f"FAIL: asr.{key} must be true or false (got {v!r}). "
                     "Remove the quotes if you wrote it as a string.")


def patterns_from_config(cfg):
    """Compile using a loaded project config's `asr:` section."""
    a = _section(cfg)
    return compile_patterns(a.get("hallucination_extra_patterns"),
                            _bool_opt(a, "hallucination_patterns_replace", False))


def min_repeats_from_config(cfg, default=3):
    a = _section(cfg)
    raw = a.get("hallucination_min_repeats", default)
    # bool is an int subclass: `false` would become 0 -> clamped to 1, which quietly
    # removes the recurrence safeguard entirely. Reject it.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SystemExit("FAIL: asr.hallucination_min_repeats must be an integer "
                         f">= 1 (got {raw!r})")
    if raw < 1:
        raise SystemExit("FAIL: asr.hallucination_min_repeats must be >= 1 "
                         f"(got {raw}); 0 would delete every single match")
    return raw


def enabled_for_config(cfg):
    return _bool_opt(_section(cfg), "strip_hallucinations", True)


def match_family(text, patterns):
    """Index of the first pattern that matches the WHOLE cue, else None."""
    for cand in _candidates(text):
        for i, p in enumerate(patterns):
            if p.fullmatch(cand):
                return i
    return None


def text_key(text):
    """Normalized identity used to count recurrence: case- and punctuation-blind, so
    "Thanks for watching!" and "thanks for watching" are the same utterance."""
    return re.sub(r"[\W_]+", " ", plain(text).lower()).strip()


def find(texts, patterns=None, min_repeats=3):
    """Classify a whole file's cue texts in ONE pass.

    Returns (drop, matches, counts):
      drop     - set of indices meeting BOTH conditions (safe to delete)
      matches  - {index: family_index} for every whole-cue boilerplate match,
                 including those below the repeat threshold (report these)
      counts   - {normalized text: occurrences among the matched cues}
    """
    if patterns is None:
        patterns = compile_patterns()
    matches, keys = {}, {}
    for i, t in enumerate(texts):
        fam = match_family(t, patterns)
        if fam is not None:
            matches[i] = fam
            keys[i] = text_key(t)
    counts = {}
    for k in keys.values():
        counts[k] = counts.get(k, 0) + 1
    drop = {i for i, k in keys.items() if counts[k] >= min_repeats}
    return drop, matches, counts


if __name__ == "__main__":
    pats = compile_patterns()
    halluc = ["Thanks for watching!", "Thank you for watching.",
              "thanks for watching", "Please subscribe to my channel",
              "Don't forget to subscribe", "Like and subscribe",
              "Subtitles by Amara.org", "Subtitles: J. Doe",
              "See you in the next video!", "Visit www.example.com",
              "\u00a9 2010 Some Studio", "(c) 2010 Some Studio",
              "All rights reserved.", "\u00a9 2010 Some Studio. All rights reserved.",
              "Subtitles by the Amara.org community", "amara.org",
              "<i>Thanks for watching!</i>", "(Thanks for watching!)"]
    # Every one of these is ordinary dialogue that an earlier, looser pattern set
    # deleted. They are the regression suite for this module.
    keep = ["Thanks for watching my back out there.",
            "I don't know.", "Help!", "Watching. Just watching.",
            "Thanks.", "Thank you.", "Thanks for the ride.",
            "He said thanks for watching and then he shot him.",
            "Subscribe.", "Subscribe to the theory that we're all doomed.",
            "See you next time.", "See you next week.",
            "I'll see you next time.", "See you guys next week.",
            "Titles by that director are all garbage.",
            "Subtitles by themselves do not tell the whole story.",
            "Translations by machines never sound right.",
            "Translations from the Greek are hard.",
            "Copyright law will not save you",
            "Copyright is a complicated subject in law.",
            "Copyright 2024 is going to be terrible.",
            "The contract says all rights reserved for the studio.",
            "Are you sure all rights reserved is the default?",
            "Subtitles by themselves do not help.",
            "Translations from machines are often wrong.",
            "Subtitles by Monday we need them.",
            "Subtitles by tomorrow, please.",
            "I found it on amara.org.",
            "They uploaded the video to amara.org yesterday.",
            "Thanks for listening, see you at dinner",
            "Go to www.police.gov to report it."]
    for t in halluc:
        assert match_family(t, pats) is not None, f"missed: {t!r}"
    for t in keep:
        assert match_family(t, pats) is None, f"false positive: {t!r}"
    # repeat threshold: one genuine utterance survives, a recurring one does not
    drop, matches, _ = find(["Thanks for watching!", "Hello."], pats, min_repeats=3)
    assert drop == set() and set(matches) == {0}
    drop, _, _ = find(["Thanks for watching!", "Hi.", "thanks for watching",
                       "Bye.", "Thanks for watching."], pats, min_repeats=3)
    assert drop == {0, 2, 4}, drop
    # counting is per EXACT text, so distinct utterances never pool their counts
    drop, matches, _ = find(["Please subscribe to my channel",
                             "Don't forget to subscribe", "Like and subscribe"],
                            pats, min_repeats=3)
    assert drop == set() and len(matches) == 3, (drop, matches)
    # a bare string in the config is a mistake, not a list of one-character regexes
    for bad in ("Thanks for watching", 5, ["ok", 7]):
        try:
            compile_patterns(bad)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"accepted malformed extra patterns: {bad!r}")
    try:
        compile_patterns(["(unclosed"])
    except SystemExit as e:
        assert "invalid ASR hallucination regex" in str(e), e
    else:
        raise AssertionError("accepted an invalid regex")

    # config validation: a falsy non-mapping must not be coerced into "defaults on",
    # a quoted boolean must not read as true, and min_repeats must not collapse to 1
    class _C:
        def __init__(self, asr):
            self.asr = asr

    for bad_section in (False, 0, "off", []):
        try:
            enabled_for_config(_C(bad_section))
        except SystemExit:
            pass
        else:
            raise AssertionError(f"accepted asr: {bad_section!r}")
    for bad in ("false", "true", 1, 0):
        try:
            enabled_for_config(_C({"strip_hallucinations": bad}))
        except SystemExit:
            pass
        else:
            raise AssertionError(f"accepted strip_hallucinations: {bad!r}")
    for bad in (False, True, 0, -1, "three", 2.5):
        try:
            min_repeats_from_config(_C({"hallucination_min_repeats": bad}))
        except SystemExit:
            pass
        else:
            raise AssertionError(f"accepted min_repeats: {bad!r}")
    assert enabled_for_config(_C(None)) is True
    assert enabled_for_config(_C({"strip_hallucinations": False})) is False
    assert min_repeats_from_config(_C({"hallucination_min_repeats": 5})) == 5
    print("asr_artifacts self-test OK:", len(pats), "patterns,",
          len(keep), "real-dialogue regressions")
