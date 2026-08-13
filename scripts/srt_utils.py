"""Centralized, robust SRT parsing/writing used by the whole pipeline.

A true cue boundary is a blank line FOLLOWED BY an index line + a timestamp line.
Internal blank lines inside cue text therefore never split (and silently drop) a
cue. Parsing is strict by default and fails loudly on any malformed/unconsumed
block. Content-agnostic: no language or project specifics here.
"""
import re

BOM = "\ufeff"
_TSPART = r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}"
# Split only at a blank line that precedes a proper "<index>\n<timestamp -->" header.
_BOUNDARY = re.compile(r"\n[ \t]*\n+(?=\d+[ \t]*\n[ \t]*" + _TSPART + r"[ \t]*-->)")
_TSLINE = re.compile(_TSPART + r"\s*-->\s*" + _TSPART)
_HEADER = re.compile(r"(?m)^[ \t]*\d+[ \t]*\n[ \t]*" + _TSPART + r"[ \t]*-->")


def normalize(text):
    if text.startswith(BOM):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


# Encodings tried, in order, when reading a subtitle we did not write ourselves.
# latin-1 is last because it maps all 256 byte values and therefore never fails —
# it is the backstop, not a guess.
READ_ENCODINGS = ("utf-8", "cp1252", "latin-1")

# Byte-order marks are the one part of encoding detection that is not guesswork.
# utf-32 must be tested before utf-16: a utf-32-le BOM starts with the utf-16-le one.
# The value is the codec used to DECODE (plain "utf-16"/"utf-32" consume the BOM
# themselves); the label is what gets reported.
_BOMS = ((b"\x00\x00\xfe\xff", "utf-32", "utf-32-be"),
         (b"\xff\xfe\x00\x00", "utf-32", "utf-32-le"),
         (b"\xff\xfe", "utf-16", "utf-16-le"),
         (b"\xfe\xff", "utf-16", "utf-16-be"))


def read_text(path, encodings=READ_ENCODINGS, encoding=None):
    """Read a subtitle file that is not necessarily UTF-8. Returns (text, encoding).

    Subtitle files in the wild are very often cp1252/Latin-1 — Spanish, French and
    Portuguese sidecars especially. Opening one with encoding="utf-8" raises before
    a single cue is read, and opening it with errors="replace" is worse: every
    accented character silently becomes U+FFFD, and the damage then looks like it
    was in the source file rather than in how we read it.

    LIMIT, and why the encoding is always reported rather than just used: past the
    BOM cases below this is a fallback chain, not charset detection. cp1252 and
    latin-1 accept essentially any byte, so a subtitle in another single-byte
    encoding (cp1251 Cyrillic, cp1253 Greek, cp1250 Central European) decodes
    "successfully" into mojibake instead of failing. Nothing here can tell those
    apart from a Western sidecar, so callers print what was assumed and every entry
    point takes an explicit `encoding` override for when the guess is wrong.

    A utf-8 BOM is deliberately NOT stripped — `normalize` does that for parsing,
    while callers that audit the file (srt_qa) need to see that it was present.
    Pure-ASCII text decodes as "utf-8", so the returned encoding names a real
    problem only when the bytes genuinely are not UTF-8.
    """
    with open(path, "rb") as f:
        data = f.read()
    if encoding:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError) as e:
            raise SystemExit(f"FAIL: cannot decode {path} as {encoding}: {e}")
    for bom, codec, label in _BOMS:
        if data.startswith(bom):
            return data.decode(codec), label
    for enc in encodings:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"FAIL: cannot decode {path} as any of {', '.join(encodings)}")


def parse_srt(text, strict=True):
    """Return ([{num:int, ts:str, text:[str,...]}], problems). Raises ValueError on
    malformed blocks when strict=True (so corruption is never silent)."""
    text = normalize(text).strip("\n")
    if not text:
        return [], []
    blocks = _BOUNDARY.split(text)
    cues, problems = [], []
    for bi, block in enumerate(blocks):
        lines = block.split("\n")
        while lines and lines[0].strip() == "":
            lines.pop(0)
        while lines and lines[-1].strip() == "":
            lines.pop()
        if not lines:
            continue
        if not lines[0].strip().isdigit():
            problems.append((bi, "no-index", block[:50]))
            continue
        num = int(lines[0].strip())
        if len(lines) < 2 or not _TSLINE.search(lines[1]):
            problems.append((num, "no-timestamp", block[:50]))
            continue
        textlines = lines[2:]
        # detect a swallowed cue boundary inside text (a lone index line followed by
        # a timestamp line that failed to split, e.g. missing blank-line separator).
        for j in range(len(textlines) - 1):
            if textlines[j].strip().isdigit() and _TSLINE.search(textlines[j + 1]):
                problems.append((num, "embedded-header", textlines[j].strip()))
                break
        cues.append({"num": num, "ts": lines[1].strip(), "text": textlines})
    # cross-check: number of valid headers in the raw text must equal parsed cues.
    n_headers = len(_HEADER.findall(text))
    if n_headers != len(cues):
        problems.append(("count", f"headers={n_headers} parsed={len(cues)}"))
    if strict and problems:
        raise ValueError(f"SRT parse problems x{len(problems)}: {problems[:5]}")
    return cues, problems


def ts_seconds(t):
    t = t.strip()
    h, m, rest = t.split(":")
    sec, ms = re.split(r"[,.]", rest)
    return (int(h) * 60 + int(m)) * 60 + int(sec) + int(ms.ljust(3, "0")[:3]) / 1000


def format_ts(t):
    """Seconds -> 'HH:MM:SS,mmm'.

    Rounding is applied to a TOTAL second count before the hour/minute/second
    split, so a value that rounds up to the next second carries correctly: 59.9999
    is 00:01:00,000, never the invalid 00:00:60,000. (Note the parser above will
    happily accept a 60 in the seconds field, so a formatter that got this wrong
    would ship a broken timestamp silently.)
    """
    t = max(float(t), 0.0)
    ms = int(round(t * 1000))
    s, ms = divmod(ms, 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def format_span(a, b):
    """Seconds pair -> a full SRT timestamp line."""
    return f"{format_ts(a)} --> {format_ts(b)}"


def ms(t):
    """Seconds -> whole milliseconds, the resolution an SRT can actually carry.

    Compare and derive durations through this rather than with raw floats: SRT
    stores milliseconds, and `10.0 + 0.7` is 0.6999999999999993 in binary floating
    point, so a cue extended to exactly the minimum duration otherwise re-parses as
    fractionally under it and gets flagged by the very check that set it.
    """
    return int(round(float(t) * 1000))


_BOUNDS = re.compile(r"(" + _TSPART + r")[ \t]*-->[ \t]*(" + _TSPART + r")")


def cue_bounds(ts):
    """'HH:MM:SS,mmm --> HH:MM:SS,mmm' -> (start_seconds, end_seconds).

    Matches the two timestamps rather than splitting the line, because SubRip
    allows trailing display coordinates after the end time
    (``... --> 00:00:12,500 X1:100 X2:200 Y1:80 Y2:120``). Splitting on "-->" and
    handing the remainder to ts_seconds makes those extra colons explode with an
    unpack error, which is a crash rather than the clean failure a caller expects.
    """
    m = _BOUNDS.search(ts)
    if not m:
        raise ValueError(f"not a timestamp line: {ts!r}")
    return ts_seconds(m.group(1)), ts_seconds(m.group(2))


def write_srt(cues, path):
    """cues: iterable of (index:int, ts:str, text:str). UTF-8, NO BOM, \\n EOL."""
    out = []
    for idx, ts, text in cues:
        out += [str(idx), ts, text.strip("\n"), ""]
    data = "\n".join(out)
    assert not data.startswith(BOM)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)


def wrap_cue(text, width=42):
    """Re-wrap a single-speaker cue into <=2 balanced lines each <=width chars.
    Preserves two-speaker dialogue cues (lines starting with '- '). If the text
    cannot fit in 2*width, returns the most balanced 2-line split (validator then
    flags it as a condensation/CPS issue)."""
    lines = [l for l in text.split("\n")]
    nonblank = [l for l in lines if l.strip()]
    if any(l.lstrip().startswith("- ") for l in nonblank):
        return text
    words = " ".join(nonblank).split()
    if not words:
        return text
    full = " ".join(words)
    if len(full) <= width:
        return full
    best_fit, best_any = None, None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        d = abs(len(a) - len(b))
        if best_any is None or d < best_any[0]:
            best_any = (d, a, b)
        if len(a) <= width and len(b) <= width:
            if best_fit is None or d < best_fit[0]:
                best_fit = (d, a, b)
    pick = best_fit or best_any
    if pick is None:
        return full          # a single token longer than width: nothing to split on
    return pick[1] + "\n" + pick[2]


if __name__ == "__main__":
    sample = ("1\n00:00:01,000 --> 00:00:02,000\nPrimera\n\nSegunda\n\n"
              "2\n00:00:03,000 --> 00:00:04,000\nTercera\n")
    cues, probs = parse_srt(sample)
    assert len(cues) == 2, cues
    assert cues[0]["text"] == ["Primera", "", "Segunda"], cues[0]
    assert abs(ts_seconds("01:14:13,500") - (3600 + 14 * 60 + 13.5)) < 1e-6
    print("srt_utils self-test OK:", len(cues), "cues; internal blank line preserved")
