"""Self-tests for every integrity guarantee. Creates an isolated temp project +
work dir, points SUBLOC_CONFIG at it, then exercises the pipeline on synthetic data
(no ffmpeg/media needed except a dummy video file for the delivery step).

  python tests/test_pipeline.py        # exits non-zero on any failure
"""
import os
import sys
import io
import json
import shutil
import tempfile
import traceback
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TMP = Path(tempfile.mkdtemp(prefix="subloc_test_"))
WORK = TMP / "work"
MEDIA = TMP / "media"
WORK.mkdir(parents=True)
MEDIA.mkdir(parents=True)
(MEDIA / "T01.mkv").write_bytes(b"\x00")          # dummy video for delivery test

CFG_YAML = f"""
project_name: "Test"
source_language: en
target:
  guardrails: es-419
  code: es-MX
  name: "Español (México)"
  srt_suffix: ".es-419.srt"
paths: {{ media_root: "{MEDIA.as_posix()}", work_dir: "{WORK.as_posix()}" }}
layout:
  kind: movie
  titles:
    - {{ key: "T01", video: "T01.mkv" }}
    - {{ key: "M01", video: "M01.mkv" }}
    - {{ key: "DUP", video: "DUP.mkv" }}
slices_per_title: 4
features: {{ allow_merge: true }}
foreign_speech: {{ policy: preserve-source-tag-set }}
catchphrases:
  - {{ type: word, find: "downtown", replace: "el centro" }}
"""
(TMP / "project.yaml").write_text(CFG_YAML, encoding="utf-8")
os.environ["SUBLOC_CONFIG"] = str(TMP / "project.yaml")
sys.path.insert(0, str(SCRIPTS))

import config            # noqa: E402
import srt_utils         # noqa: E402
import parse_source      # noqa: E402
import slice_ranges      # noqa: E402
import normalize_batch   # noqa: E402
import autofix           # noqa: E402
import build_srt         # noqa: E402
import validate_srt      # noqa: E402
import align_check       # noqa: E402
import reconstruct       # noqa: E402
import apply_patch       # noqa: E402
import finalize_title    # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def expect_raises(name, fn, expected=SystemExit):
    """Assert fn() fails, and fails the way we MEANT it to.

    A catch-all here would pass on a typo in the lambda (NameError) or on an
    unhandled TypeError from deep inside — it would certify a crash as a
    validation. So the exception type has to match what the code promises.
    """
    global PASS, FAIL
    try:
        fn()
    except expected as e:
        PASS += 1
        print(f"  ok   {name} (raised {type(e).__name__})")
    except Exception as e:  # noqa: BLE001 - the wrong failure is still a failure
        FAIL += 1
        print(f"  FAIL {name} (raised {type(e).__name__}: {e}, "
              f"expected {expected.__name__})")
    else:
        FAIL += 1
        print(f"  FAIL {name} (expected an error)")


print("[0] project overrides")
_cfg = config.load()
check("target code override", _cfg.target_code == "es-MX")
check("target name override", _cfg.target_name == "Español (México)")
check("foreign-speech override merged",
      _cfg.foreign_speech.get("policy") == "preserve-source-tag-set"
      and _cfg.foreign_speech.get("never_italicize_target") is True)


SRC_SRT = """1
00:00:01,000 --> 00:00:03,000
Hello there.

2
00:00:03,200 --> 00:00:05,000
[music playing]

3
00:00:05,200 --> 00:00:08,000
I think it's [inaudible] now.

4
00:00:08,200 --> 00:00:10,000
- Yes.
- No.

5
00:00:10,200 --> 00:00:12,000
<i>Bonjour.</i>

6
00:00:12,200 --> 00:00:14,000
This is great.

7
00:00:14,200 --> 00:00:16,000
[applause]

8
00:00:16,200 --> 00:00:19,000
Goodbye, my friend.
"""

GOOD = {
    1: "Hola.",
    2: "<DROP>",
    3: "Creo que [ininteligible] ahora.",
    4: "- Sí.\n- No.",
    5: "<i>Bonjour.</i>",
    6: "Esto es genial.",
    7: "<DROP>",
    8: "Adiós, amigo.",
}


def write_src(key="T01"):
    (WORK / f"{key}.src.srt").write_text(SRC_SRT, encoding="utf-8")
    parse_source.parse(key)


def write_batches(key, mapping, parts=4, defects=False):
    for f in WORK.glob(f"{key}.tgt*.txt"):
        f.unlink()
    nums = sorted(mapping)
    size = -(-len(nums) // parts)
    for k in range(parts):
        chunk = nums[k * size:(k + 1) * size]
        if not chunk:
            continue
        lines = []
        for n in chunk:
            txt = mapping[n]
            first = txt.split("\n")[0]
            rest = txt.split("\n")[1:]
            if defects and n == chunk[0]:
                first = "\ufeff" + first      # BOM heal test on first cue of slice
            lines.append(f"{n}|||{first}")
            lines.extend(rest)
        (WORK / f"{key}.tgt{k+1:02d}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# === 1. srt_utils ============================================================
print("[1] srt_utils")
cues, _ = srt_utils.parse_srt("1\n00:00:01,000 --> 00:00:02,000\nA\n\nB\n")
check("internal blank line preserved", len(cues) == 1 and cues[0]["text"] == ["A", "", "B"])
check("ts hour handling", abs(srt_utils.ts_seconds("01:14:13,500") - (3600 + 14 * 60 + 13.5)) < 1e-6)
expect_raises("strict parse rejects missing timestamp",
              lambda: srt_utils.parse_srt("1\nnot-a-timestamp\nX\n", strict=True),
              expected=ValueError)

# === 2. parse_source =========================================================
print("[2] parse_source")
write_src("T01")
src = json.load(open(WORK / "T01.src.json", encoding="utf-8"))
by = {c["num"]: c for c in src}
check("8 cues parsed", len(src) == 8)
check("[music playing] flagged sound", by[2]["sound"] is True)
check("[applause] flagged sound", by[7]["sound"] is True)
check("dialogue not sound", by[1]["sound"] is False)
check("inaudible flagged", by[3]["inaudible"] is True)
check("two-speaker not sound", by[4]["sound"] is False)

# === 3. slice_ranges =========================================================
print("[3] slice_ranges")
parts = slice_ranges.slice_title("T01")
covered = []
for k, _a, _b, _c in parts:
    covered += [c["num"] for c in json.load(open(WORK / "_ranges" / f"T01.r{k}.src.json", encoding="utf-8"))]
check("slices cover all cues once", covered == [c["num"] for c in src])

# === 4. normalize_batch (self-heal + overlap guard) ==========================
print("[4] normalize_batch")
write_batches("T01", GOOD, parts=4, defects=True)
# inject a mis-prefixed continuation + a literal \n escape into cue 8
p4 = sorted(WORK.glob("T01.tgt*.txt"))[-1]
txt = p4.read_text(encoding="utf-8")
p4.write_text(txt.replace("8|||Adiós, amigo.", "8|||Adiós,\\namigo."), encoding="utf-8")
normalize_batch.normalize("T01")
cons = (WORK / "T01.tgt01.txt").read_text(encoding="utf-8")
check("consolidated single file exists", (WORK / "T01.tgt01.txt").exists())
check("BOM healed (cue 1 header intact)", cons.startswith("1|||Hola."))
check("literal \\n healed to real break", "Adiós,\namigo." in cons)
# overlap guard
write_batches("T01", GOOD, parts=4)
(WORK / "T01.tgtZZ.txt").write_text("1|||dup\n", encoding="utf-8")
expect_raises("overlap (same cue in two files) rejected", lambda: normalize_batch.normalize("T01"))
(WORK / "T01.tgtZZ.txt").unlink()

# === 5. autofix ==============================================================
print("[5] autofix")
write_batches("T01", {**GOOD, 6: "Está en downtown."}, parts=4)
normalize_batch.normalize("T01")
autofix.autofix("T01")
check("catchphrase word-fix applied (downtown->el centro)",
      "el centro" in (WORK / "T01.tgt01.txt").read_text(encoding="utf-8"))

# === 6. build_srt ============================================================
print("[6] build_srt")
write_batches("T01", GOOD, parts=4)
normalize_batch.normalize("T01")
out, kept, dropped = build_srt.build("T01")
check("kept = 6, dropped = 2 (sound cues)", kept == 6 and dropped == 2)
check("manifest written", (WORK / "T01.manifest.json").exists())
built = srt_utils.parse_srt((WORK / "T01.es-419.srt").read_text(encoding="utf-8"))[0]
check("output timestamps byte-identical to source (kept cues)",
      [c["ts"] for c in built] == [by[n]["ts"] for n in (1, 3, 4, 5, 6, 8)])
# DROP on dialogue must fail
write_batches("T01", {**GOOD, 6: "<DROP>"}, parts=4)
normalize_batch.normalize("T01")
expect_raises("<DROP> on a dialogue cue rejected", lambda: build_srt.build("T01"))
# dropok allowlist lets it through
(WORK / "T01.dropok.txt").write_text("6\n", encoding="utf-8")
write_batches("T01", {**GOOD, 6: "<DROP>"}, parts=4)
normalize_batch.normalize("T01")
_o, k2, d2 = build_srt.build("T01")
check("dropok allows a reviewed non-sound drop", d2 == 3)
(WORK / "T01.dropok.txt").unlink()
# blank line inside a cue must fail
write_batches("T01", {**GOOD, 6: "Linea1\n\nLinea2"}, parts=4)
normalize_batch.normalize("T01")
expect_raises("blank line inside a cue rejected", lambda: build_srt.build("T01"))

# === 7. validate_srt =========================================================
print("[7] validate_srt")
write_batches("T01", GOOD, parts=4); normalize_batch.normalize("T01"); build_srt.build("T01")
check("clean target validates with 0 HARD", validate_srt.validate("T01", verbose=False) == 0)
# target-CPS band must be reported even below the high-density threshold
write_batches("T01", {**GOOD, 6: "á" * 36}, parts=4)
normalize_batch.normalize("T01"); build_srt.build("T01")
_cps_out = io.StringIO()
with redirect_stdout(_cps_out):
    _cps_hard = validate_srt.validate("T01", verbose=True)
check("17-21 CPS target band reported",
      _cps_hard == 0
      and f"CPS {validate_srt.cps('á' * 36, by[6]['ts']):.0f} > target 17"
      in _cps_out.getvalue())
check("named entity survives display-line wrap",
      validate_srt._contains_named_entity("Premio\nMeikou", "Premio Meikou"))
# banned term
write_batches("T01", {**GOOD, 6: "No me jodas."}, parts=4)
normalize_batch.normalize("T01"); build_srt.build("T01")
check("banned Spain-ism caught", validate_srt.validate("T01", verbose=False) >= 1)
# context-exempt: 'tía' in family context passes; bare/dude flagged
write_batches("T01", {**GOOD, 6: "Es mi tía Marta."}, parts=4)
normalize_batch.normalize("T01"); build_srt.build("T01")
check("'mi tía' (family) NOT flagged", validate_srt.validate("T01", verbose=False) == 0)
# leftover source-language
write_batches("T01", {**GOOD, 6: "This is the best."}, parts=4)
normalize_batch.normalize("T01"); build_srt.build("T01")
check("leftover source-language caught", validate_srt.validate("T01", verbose=False) >= 1)
# unbalanced tag
write_batches("T01", {**GOOD, 6: "<i>Texto sin cierre."}, parts=4)
normalize_batch.normalize("T01"); build_srt.build("T01")
check("unbalanced <i> caught", validate_srt.validate("T01", verbose=False) >= 1)
# banned 'coger' (the #1 LatAm trap)
write_batches("T01", {**GOOD, 6: "Voy a coger el plato."}, parts=4)
normalize_batch.normalize("T01"); build_srt.build("T01")
check("banned 'coger' caught", validate_srt.validate("T01", verbose=False) >= 1)
# Spain vosotros verb ending
write_batches("T01", {**GOOD, 6: "¿Qué hacéis aquí?"}, parts=4)
normalize_batch.normalize("T01"); build_srt.build("T01")
check("vosotros ending '-éis' caught", validate_srt.validate("T01", verbose=False) >= 1)
# caution term -> SOFT (not a hard block)
write_batches("T01", {**GOOD, 6: "El móvil del crimen es claro."}, parts=4)
normalize_batch.normalize("T01"); build_srt.build("T01")
check("caution term 'móvil' does NOT hard-block", validate_srt.validate("T01", verbose=False) == 0)
# dropok with a reason that contains digits must not spuriously drop cue 3
(WORK / "T01.dropok.txt").write_text("2|||music at 3 am\n", encoding="utf-8")
write_batches("T01", GOOD, parts=4); normalize_batch.normalize("T01")
_o, _k, _d = build_srt.build("T01")
check("dropok reason digits don't create spurious drops (cue 3 kept)",
      any(m.get("out") and 3 in m["src"] for m in
          json.load(open(WORK / "T01.manifest.json", encoding="utf-8"))["cues"]))
(WORK / "T01.dropok.txt").unlink()

# === 8. align_check ==========================================================
print("[8] align_check")
write_batches("T01", GOOD, parts=4); normalize_batch.normalize("T01")
check("aligned content passes", align_check.check("T01") == 0)

# === 9. reconstruct ==========================================================
print("[9] reconstruct")
write_batches("T01", GOOD, parts=4); normalize_batch.normalize("T01"); build_srt.build("T01")
(WORK / "T01.tgt01.txt").write_text("1|||CLOBBERED\n", encoding="utf-8")   # simulate clobber
reconstruct.reconstruct("T01")
after = (WORK / "T01.tgt01.txt").read_text(encoding="utf-8")
check("manifest reconstruct restores full coverage",
      all(f"{n}|||" in after for n in (1, 3, 4, 5, 6, 8)) and "2|||<DROP>" in after)

# === 9b. validate catches a truncated delivered SRT (lost trailing cue) ======
print("[9b] truncation catch")
write_batches("T01", GOOD, parts=4); normalize_batch.normalize("T01"); build_srt.build("T01")
srt_p = WORK / "T01.es-419.srt"
blocks = srt_p.read_text(encoding="utf-8").rstrip().split("\n\n")
srt_p.write_text("\n\n".join(blocks[:-1]) + "\n", encoding="utf-8")   # drop last cue block
check("truncated delivered SRT caught HARD", validate_srt.validate("T01", verbose=False) >= 1)

# === 10. apply_patch =========================================================
print("[10] apply_patch")
write_batches("T01", GOOD, parts=4); normalize_batch.normalize("T01")
(WORK / "T01.patch.txt").write_text("6|||Esto es excelente.\n", encoding="utf-8")
apply_patch.apply_patch("T01")
check("patch applied to consolidated batch",
      "6|||Esto es excelente." in (WORK / "T01.tgt01.txt").read_text(encoding="utf-8"))
(WORK / "T01.patch.txt").unlink()

# === 11. merged adjacent cues (allow_merge) — END TO END via normalize =======
print("[11] merge mode")
(WORK / "M01.src.srt").write_text(
    "1\n00:00:01,000 --> 00:00:02,000\nGo.\n\n2\n00:00:02,000 --> 00:00:04,000\nNow.\n", encoding="utf-8")
parse_source.parse("M01")
(WORK / "M01.tgt01.txt").write_text("1..2|||Vamos ahora.\n", encoding="utf-8")   # worker merge syntax
normalize_batch.normalize("M01")                                                  # must pass coverage
check("normalize accepts N..M merge syntax", "1..2|||" in (WORK / "M01.tgt01.txt").read_text(encoding="utf-8"))
_o, mk, md = build_srt.build("M01")
mc = srt_utils.parse_srt((WORK / "M01.es-419.srt").read_text(encoding="utf-8"))[0]
check("merged cue spans start(1)..end(2)", mc[0]["ts"] == "00:00:01,000 --> 00:00:04,000")
check("merge validates clean", validate_srt.validate("M01", verbose=False) == 0)
# reconstruct a merged title must NOT crash and must round-trip the N..M head
(WORK / "M01.tgt01.txt").write_text("1|||CLOBBERED\n", encoding="utf-8")
reconstruct.reconstruct("M01")
check("reconstruct handles a merged title (no KeyError)",
      "1..2|||" in (WORK / "M01.tgt01.txt").read_text(encoding="utf-8"))
# merge across DIFFERENT speakers must be rejected
(WORK / "SPK.src.srt").write_text(
    "1\n00:00:01,000 --> 00:00:02,000\nRICK: Go.\n\n2\n00:00:02,000 --> 00:00:04,000\nMORTY: Now.\n",
    encoding="utf-8")
parse_source.parse("SPK")
(WORK / "SPK.tgt01.txt").write_text("1..2|||Vamos ya.\n", encoding="utf-8")
normalize_batch.normalize("SPK")
expect_raises("merge across different speakers rejected", lambda: build_srt.build("SPK"))

# === 12. reconstruct duplicate-timestamp fail-loud (no manifest) =============
print("[12] reconstruct dup-ts fail-loud")
(WORK / "DUP.src.srt").write_text(
    "1\n00:00:01,000 --> 00:00:02,000\nA\n\n2\n00:00:01,000 --> 00:00:02,000\nB\n", encoding="utf-8")
parse_source.parse("DUP")
(WORK / "DUP.es-419.srt").write_text(
    "1\n00:00:01,000 --> 00:00:02,000\nA es\n\n2\n00:00:01,000 --> 00:00:02,000\nB es\n", encoding="utf-8")
expect_raises("duplicate timestamps fail-loud without manifest", lambda: reconstruct.reconstruct("DUP"))

# === 13. finalize (full chain -> atomic delivery) ============================
print("[13] finalize_title")
write_batches("T01", GOOD, parts=4); normalize_batch.normalize("T01")
ok = finalize_title.finalize("T01")
dest = MEDIA / "T01.es-419.srt"
check("finalize delivered sidecar next to video", ok and dest.exists())
check("delivered sidecar has no BOM", not dest.read_bytes().startswith(b"\xef\xbb\xbf"))

# === 14. transcribe: ASR draft segmentation =================================
print("[14] transcribe segmentation")
import transcribe        # noqa: E402  (faster_whisper is imported lazily, not needed here)


def _words(spec, t0=0.0, dur=0.30, gap=0.05):
    """spec: list of (word) or (word, gap_before). Build word dicts with timings."""
    out, t = [], t0
    for item in spec:
        w, g = item if isinstance(item, tuple) else (item, gap)
        t += g
        out.append({"word": w, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur
    return out


_loop = transcribe._collapse_loops(_words(["no"] * 8))
check("word-level ASR loop collapsed", 0 < len(_loop) < 8)
check("collapsed loop keeps the word itself", _loop[0]["word"] == "no")
_keep2 = transcribe._collapse_loops(_words(["no", "no"]))
check("a natural double is NOT collapsed", len(_keep2) == 2)
_phrase = transcribe._collapse_loops(_words(["get", "out"] * 6))
check("phrase-level ASR loop collapsed to two repetitions",
      [w["word"] for w in _phrase] == ["get", "out", "get", "out"])
_triple = transcribe._collapse_loops(_words(["get", "out"] * 3))
check("a natural triple repetition is NOT collapsed", len(_triple) == 6)

_READ = {"max_cpl": 42, "max_lines": 2, "max_cps": 17, "min_duration": 0.7}
# distinct words: a repeated token would be eaten by the loop collapser above and
# would never reach the character-budget logic this is meant to exercise.
_NATO = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
         "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
         "quebec", "romeo", "sierra", "tango"]
_long = transcribe.build_cues(_words(_NATO), _READ)
check("no cue exceeds the character budget",
      _long and all(len(c["text"]) <= 42 * 2 for c in _long))
check("over-budget speech split into several cues", len(_long) > 1)
check("segmentation loses no words",
      " ".join(c["text"] for c in _long).split() == _NATO)

_gapped = transcribe.build_cues(
    _words([("Hello", 0.05), ("there", 0.05)]) + _words([("Later", 0.0)], t0=30.0), _READ)
check("a long silence forces a cue boundary", len(_gapped) >= 2)

_cues = transcribe.build_cues(_words(["one", "two", "three", "four", "five"]), _READ)
check("cue timestamps are monotonic and non-overlapping",
      len(_long) > 1 and all(_long[i]["end"] <= _long[i + 1]["start"] + 1e-6
                             for i in range(len(_long) - 1)))
check("every cue clears the minimum duration",
      all((c["end"] - c["start"]) >= 0.7 - 1e-6 for c in _long))
# a genuinely sub-minimum utterance must be stretched, not shipped at 0.2s
_tiny = transcribe.build_cues(
    [{"word": "Go", "start": 5.0, "end": 5.2}], {**_READ, "min_duration": 0.7})
check("a too-short utterance is extended to min_duration",
      len(_tiny) == 1 and (_tiny[0]["end"] - _tiny[0]["start"]) >= 0.7 - 1e-6)
check("no cue starts before zero", all(c["start"] >= 0 for c in _cues))
check("punctuation-only fragments dropped",
      transcribe.build_cues(_words(["...", "?!"]), _READ) == [])
check("ts formatter pads to SRT form",
      transcribe._fmt_ts(3671.5, 3672.25) == "01:01:11,500 --> 01:01:12,250")
check("ts formatter carries a 999.6ms rounding into the next second",
      transcribe._fmt_ts(0.9999, 1.5).startswith("00:00:01,000"))
# regression: rounding must carry through the MINUTE and HOUR boundary too.
# The parser accepts a literal "60" in the seconds field, so a bad carry here
# would ship an invalid timestamp silently rather than failing loud.
check("ts rounding carries across the minute boundary",
      srt_utils.format_ts(59.9999) == "00:01:00,000")
check("ts rounding carries across the hour boundary",
      srt_utils.format_ts(3599.9999) == "01:00:00,000")

# repeated dialogue separated in time is REAL, not an ASR loop
_far = transcribe._collapse_loops([
    {"word": "Run", "start": 1.0, "end": 1.4},
    {"word": "Run", "start": 60.0, "end": 60.4},
    {"word": "Run", "start": 120.0, "end": 120.4}])
check("repeats separated by silence are kept (not treated as a loop)", len(_far) == 3)
_tight = transcribe._collapse_loops(
    [{"word": "Run", "start": 1.0 + i * 0.45, "end": 1.4 + i * 0.45} for i in range(8)])
check("back-to-back repeats ARE collapsed", len(_tight) < 8)

# a single token longer than the line budget must not crash the wrapper
check("wrap_cue survives an unsplittable over-long token",
      srt_utils.wrap_cue("A" * 60, width=42) == "A" * 60)

# the ASR draft must survive the pipeline's own strict parser + parse_source
_asr_rows = [(i, transcribe._fmt_ts(c["start"], c["end"]),
              srt_utils.wrap_cue(c["text"], width=42)) for i, c in enumerate(_cues, 1)]
srt_utils.write_srt(_asr_rows, WORK / "ASR.src.srt")
_reparsed, _probs = srt_utils.parse_srt((WORK / "ASR.src.srt").read_text(encoding="utf-8"),
                                        strict=True)
check("ASR draft re-parses cleanly under the strict parser",
      len(_reparsed) == len(_asr_rows) and not _probs)

# === 15. srt_qa: source-side structural + readability QA =====================
print("[15] srt_qa")
import srt_qa            # noqa: E402

_CLEAN = ("1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"
          "2\n00:00:03,500 --> 00:00:06,000\nGood to see you.\n")
(WORK / "QA1.srt").write_text(_CLEAN, encoding="utf-8")
_h, _s = srt_qa.qa(WORK / "QA1.srt", verbose=False)
check("clean SRT passes QA with no HARD findings", _h == 0)

(WORK / "QA2.srt").write_text("\ufeff" + _CLEAN, encoding="utf-8")
check("BOM is a HARD finding", srt_qa.qa(WORK / "QA2.srt", verbose=False)[0] > 0)

(WORK / "QA3.srt").write_text(
    "1\n00:00:01,000 --> 00:00:03,000\n<i>Unclosed.\n", encoding="utf-8")
check("unbalanced <i> is a HARD finding", srt_qa.qa(WORK / "QA3.srt", verbose=False)[0] > 0)

(WORK / "QA4.srt").write_text(
    "1\n00:00:03,000 --> 00:00:01,000\nBackwards.\n", encoding="utf-8")
check("inverted timestamps are a HARD finding", srt_qa.qa(WORK / "QA4.srt", verbose=False)[0] > 0)

(WORK / "QA5.srt").write_text(
    "1\n00:00:01,000 --> 00:00:03,000\nSame line.\n\n"
    "2\n00:00:03,500 --> 00:00:06,000\nSame line.\n", encoding="utf-8")
check("adjacent duplicate text is a soft ASR-loop finding",
      srt_qa.qa(WORK / "QA5.srt", verbose=False)[1] > 0)

(WORK / "QA6.srt").write_text(
    "2\n00:00:01,000 --> 00:00:03,000\nMisnumbered.\n", encoding="utf-8")
check("numbering break is a HARD finding", srt_qa.qa(WORK / "QA6.srt", verbose=False)[0] > 0)

# a cue that starts BEFORE the previous one is a structural defect, not a soft note
(WORK / "QA7.srt").write_text(
    "1\n00:00:10,000 --> 00:00:11,000\nLater.\n\n"
    "2\n00:00:01,000 --> 00:00:02,000\nEarlier.\n", encoding="utf-8")
check("non-monotonic cue start is a HARD finding",
      srt_qa.qa(WORK / "QA7.srt", verbose=False)[0] > 0)

# findings must not depend on how many examples are DISPLAYED
(WORK / "QA8.srt").write_text("\n\n".join(
    f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},300\n{'x' * 60}" for i in range(1, 9)),
    encoding="utf-8")
check("soft finding count is independent of --top",
      srt_qa.qa(WORK / "QA8.srt", top=1, verbose=False)[1]
      == srt_qa.qa(WORK / "QA8.srt", top=99, verbose=False)[1])
check("a cue over target CPS is reported, not just counted",
      srt_qa.qa(WORK / "QA8.srt", verbose=False)[1] > 0)

# === 16. srt_polish: deterministic clean-up of a source SRT ==================
print("[16] srt_polish")
import srt_polish        # noqa: E402

(WORK / "P1.srt").write_text(
    "1\n00:00:01,000 --> 00:00:03,000\nSame line.\n\n"
    "2\n00:00:03,100 --> 00:00:05,000\nSame line.\n\n"
    "3\n00:00:05,100 --> 00:00:07,000\nA different line.\n", encoding="utf-8")
_rows, _st = srt_polish.polish(WORK / "P1.srt", out_path=WORK / "P1.out.srt", verbose=False)
check("consecutive identical cues collapsed", len(_rows) == 2 and _st["collapsed_loops"] == 1)
check("collapsed cue spans the whole run",
      _rows[0][1].endswith("00:00:05,000"))

(WORK / "P2.srt").write_text(
    "1\n00:00:01,000 --> 00:00:03,000\n...\n\n"
    "2\n00:00:03,100 --> 00:00:05,000\nReal dialogue.\n", encoding="utf-8")
_rows, _st = srt_polish.polish(WORK / "P2.srt", out_path=WORK / "P2.out.srt", verbose=False)
check("punctuation-only cue dropped", len(_rows) == 1 and _st["dropped_empty"] == 1)
check("surviving cues are renumbered from 1", _rows[0][0] == 1)

_LONG = "This is a single very long spoken line that must be wrapped onto two lines."
(WORK / "P3.srt").write_text(
    f"1\n00:00:01,000 --> 00:00:06,000\n{_LONG}\n", encoding="utf-8")
_rows, _ = srt_polish.polish(WORK / "P3.srt", out_path=WORK / "P3.out.srt", verbose=False)
check("over-long line re-wrapped within max_cpl",
      all(len(l) <= 42 for l in _rows[0][2].split("\n")))
check("re-wrapping preserves the words", _rows[0][2].replace("\n", " ") == _LONG)

# a two-speaker dialogue cue must keep its "- " layout
(WORK / "P4.srt").write_text(
    "1\n00:00:01,000 --> 00:00:04,000\n- Yes.\n- No.\n", encoding="utf-8")
_rows, _ = srt_polish.polish(WORK / "P4.srt", out_path=WORK / "P4.out.srt", verbose=False)
check("two-speaker dialogue layout preserved", _rows[0][2] == "- Yes.\n- No.")

# overlapping + too-short cues get fixed without reordering. Cue 3 sits close
# behind cue 2 so that min-duration extension would COLLIDE with it if unguarded.
(WORK / "P5.srt").write_text(
    "1\n00:00:01,000 --> 00:00:05,000\nFirst.\n\n"
    "2\n00:00:03,000 --> 00:00:03,200\nSecond.\n\n"
    "3\n00:00:03,500 --> 00:00:03,600\nThird.\n\n"
    "4\n00:00:20,000 --> 00:00:20,100\nFourth.\n", encoding="utf-8")
_rows, _st = srt_polish.polish(WORK / "P5.srt", out_path=WORK / "P5.out.srt", verbose=False)
_b = [srt_utils.cue_bounds(ts) for _, ts, _ in _rows]
check("overlap removed", all(_b[i][1] <= _b[i + 1][0] + 1e-6 for i in range(len(_b) - 1)))
check("cue order preserved", _b == sorted(_b))
check("no cue was silently deleted", len(_rows) == 4)
check("trailing short cue extended to min duration", (_b[-1][1] - _b[-1][0]) >= 0.7 - 1e-6)
check("a short cue boxed in by the next one is NOT extended past it",
      _b[1][1] <= _b[2][0] + 1e-6 and (_b[1][1] - _b[1][0]) < 0.7)

# identical text far apart is REAL repeated dialogue and must not be merged
(WORK / "P6.srt").write_text(
    "1\n00:00:01,000 --> 00:00:02,000\nRun!\n\n"
    "2\n00:02:00,000 --> 00:02:01,000\nRun!\n", encoding="utf-8")
_rows, _st = srt_polish.polish(WORK / "P6.srt", out_path=WORK / "P6.out.srt", verbose=False)
check("identical cues far apart are NOT collapsed",
      len(_rows) == 2 and _st["collapsed_loops"] == 0)

# a min-duration extension must still clear the floor AFTER the SRT round-trip.
# Raw float arithmetic lands a hair under (10.0 + 0.7 -> 0.6999999999999993), so
# an in-memory-only assertion would pass while QA still flagged the cue.
(WORK / "P7.srt").write_text(
    "1\n00:00:10,000 --> 00:00:10,100\nHi.\n", encoding="utf-8")
srt_polish.polish(WORK / "P7.srt", out_path=WORK / "P7.out.srt", verbose=False)
_rt, _ = srt_utils.parse_srt((WORK / "P7.out.srt").read_text(encoding="utf-8"), strict=True)
_st7, _en7 = srt_utils.cue_bounds(_rt[0]["ts"])
check("extended cue clears min_duration in whole milliseconds",
      srt_utils.ms(_en7) - srt_utils.ms(_st7) >= srt_utils.ms(0.7))
check("polished cue is not re-flagged as sub-minimum by QA",
      srt_qa.qa(WORK / "P7.out.srt", verbose=False)[1] == 0)

# polished output must survive the strict parser and QA cleanly
_h, _s = srt_qa.qa(WORK / "P5.out.srt", verbose=False)
check("polished output passes QA with no HARD findings", _h == 0)

(WORK / "BAD.srt").write_text(
    "00:00:01,000 --> 00:00:02,000\nMissing its index line.\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\nFine.\n", encoding="utf-8")
expect_raises("polish refuses a malformed SRT rather than guessing",
              lambda: srt_polish.polish(WORK / "BAD.srt", verbose=False))

print("[17] asr_artifacts (boilerplate hallucinations)")
import asr_artifacts      # noqa: E402
_pats = asr_artifacts.compile_patterns()

# --- detection: the recognizable boilerplate families ---
for _t in ("Thanks for watching!", "Thank you for watching.",
           "thanks for watching", "<i>Thanks for watching!</i>",
           "(Thanks for watching!)", "Thanks for watching this video!",
           "Please subscribe to my channel", "Don't forget to subscribe",
           "Like and subscribe", "Subtitles by Amara.org", "Subtitles: J. Doe",
           "See you in the next video!", "Visit www.example.com",
           "\u00a9 2010 Some Studio", "(c) 2010 Some Studio"):
    check(f"detected boilerplate {_t!r}", asr_artifacts.match_family(_t, _pats) is not None)

# --- the dangerous direction: real dialogue must survive ---
# Every string here was deleted by an earlier, looser pattern set that three
# independent code reviews broke. They are the regression suite for the feature:
# if a pattern is ever widened, one of these fails before it can reach a delivery.
_REAL_DIALOGUE = [
    "Thanks for watching my back out there.",
    "He said thanks for watching, then he shot him.",
    "I don't know.", "Help!", "Thanks.", "Thank you.", "Thanks for the ride.",
    "Watching. Just watching.",
    "Subscribe.", "Subscribe to the theory that we're all doomed.",
    "See you next time.", "See you next week.", "I'll see you next time.",
    "See you guys next week.",
    "Titles by that director are all garbage.",
    "Subtitles by themselves do not tell the whole story.",
    # short + lowercase: these are the cases that actually prove the credit tail
    # requires a NAME. The long sentence above passes on token count alone, so it
    # stayed green while `re.I` was quietly defeating the capitalization anchor.
    "Subtitles by hand take forever.",
    "Subtitles by themselves are useless.",
    "Caption by hand is slow.",
    "Translation by machine is bad.",
    "Subtitles by Monday we need them.",
    "Translations by machines never sound right.",
    "Translations from the Greek are hard.",
    "Translations from machines are often wrong.",
    "Copyright law will not save you",
    "Copyright is a complicated subject in law.",
    "Copyright 2024 is going to be terrible.",
    "The contract says all rights reserved for the studio.",
    "Are you sure all rights reserved is the default?",
    "I found it on amara.org.",
    "They uploaded the video to amara.org yesterday.",
    "Thanks for listening, see you at dinner",
    "Go to www.police.gov to report it.",
]
for _t in _REAL_DIALOGUE:
    check(f"kept real dialogue {_t!r}", asr_artifacts.match_family(_t, _pats) is None)

# ...and they must survive even when REPEATED, since recurrence is what unlocks
# deletion. Three different real lines must never delete each other.
_drop_r, _, _ = asr_artifacts.find(_REAL_DIALOGUE * 3, _pats, min_repeats=3)
check("real dialogue repeated 3x is still never deleted", _drop_r == set())

# --- the repeat threshold ---
_drop, _matches, _counts = asr_artifacts.find(
    ["Thanks for watching!", "Hello.", "Goodbye."], _pats, min_repeats=3)
check("a single boilerplate utterance is NOT deleted", _drop == set())
check("...but it is still reported", set(_matches) == {0})

_drop2, _, _ = asr_artifacts.find(
    ["Thanks for watching!", "Hi.", "thanks for watching", "Bye.",
     "Thanks for watching."], _pats, min_repeats=3)
check("a recurring utterance IS deleted (case/punctuation-blind)", _drop2 == {0, 2, 4})

# counting is per EXACT text, not per pattern family: three DIFFERENT boilerplate
# lines that share one pattern must not pool their counts and delete each other
_drop_p, _matches_p, _ = asr_artifacts.find(
    ["Please subscribe to my channel", "Don't forget to subscribe",
     "Like and subscribe"], _pats, min_repeats=3)
check("distinct utterances do not pool counts across a pattern family",
      _drop_p == set() and len(_matches_p) == 3)

_drop3, _, _ = asr_artifacts.find(["Please subscribe"] * 3, _pats, min_repeats=4)
check("min_repeats is honoured", _drop3 == set())

# --- malformed config must fail loud, never delete something odd -------------
expect_raises("a bare string for extra patterns is rejected",
              lambda: asr_artifacts.compile_patterns("Thanks for watching"))
expect_raises("a non-string extra pattern is rejected",
              lambda: asr_artifacts.compile_patterns(["ok", 7]))
expect_raises("an invalid regex is rejected with a clear error",
              lambda: asr_artifacts.compile_patterns(["(unclosed"]))


class _FakeCfg:
    def __init__(self, asr):
        self.asr = asr


expect_raises("a non-mapping asr: section is rejected",
              lambda: asr_artifacts.patterns_from_config(_FakeCfg(True)))
expect_raises("a non-integer min_repeats is rejected",
              lambda: asr_artifacts.min_repeats_from_config(_FakeCfg(
                  {"hallucination_min_repeats": "three"})))
check("a missing asr: section falls back to the defaults",
      asr_artifacts.enabled_for_config(_FakeCfg(None)) is True
      and asr_artifacts.min_repeats_from_config(_FakeCfg(None)) == 3)

# --- end to end through polish: the artifact loses only the hallucinations ---
_H_TEXTS = ["Get in the boat!", "Thanks for watching!", "It's coming up fast.",
            "thanks for watching", "Thanks for watching my back out there.",
            "Swim!", "Thanks for watching.", "Behind you!"]
_H_SRT = "".join(
    f"{i}\n00:{i//60:02d}:{i%60:02d},000 --> 00:{i//60:02d}:{i%60:02d},900\n{t}\n\n"
    for i, t in enumerate(_H_TEXTS, start=1))
(WORK / "H1.srt").write_text(_H_SRT, encoding="utf-8")
_hrows, _hstats = srt_polish.polish(WORK / "H1.srt", out_path=WORK / "H1.out.srt",
                                    verbose=False)
_htexts = [r[2].replace("\n", " ") for r in _hrows]
check("polish removed exactly the 3 hallucinated cues",
      _hstats["dropped_hallucination"] == 3 and len(_hrows) == 5)
check("polish kept the look-alike real line",
      any("Thanks for watching my back" in t for t in _htexts))
check("no hallucinated cue survived polish",
      not any(asr_artifacts.match_family(t, _pats) is not None for t in _htexts))
check("surviving cues renumbered from 1",
      [r[0] for r in _hrows] == list(range(1, len(_hrows) + 1)))
check("polished hallucination-free output re-parses and passes QA",
      srt_qa.qa(WORK / "H1.out.srt", verbose=False)[0] == 0)

# QA must SEE them before polish removes them, and say so IN the findings
_qh, _qs = srt_qa.qa(WORK / "H1.srt", verbose=False)
check("QA reports hallucinations as soft findings, not HARD", _qh == 0 and _qs >= 3)
_qbuf = io.StringIO()
with redirect_stdout(_qbuf):
    srt_qa.qa(WORK / "H1.srt", top=50)
check("QA names the hallucinated cues in its report",
      _qbuf.getvalue().count("ASR boilerplate hallucination") == 3)

# A deletion must not re-time what survives. The precise invariant is not "same as
# the input" (polish legitimately adjusts timing), it is "same as polishing the very
# same file with removal turned OFF" — removal must change WHICH cues are present
# and nothing else.
srt_polish.CFG.asr = {"strip_hallucinations": False}
_norows, _ = srt_polish.polish(WORK / "H1.srt", out_path=WORK / "H1.nostrip.srt",
                               verbose=False)
srt_polish.CFG.asr = {"strip_hallucinations": True}
_expect = [(ts, tx) for _n, ts, tx in _norows
           if asr_artifacts.match_family(tx, _pats) is None]
check("removal changes which cues survive and nothing else "
      "(timing identical to a no-removal polish)",
      [(ts, tx) for _n, ts, tx in _hrows] == _expect)
check("...and it really did remove something", len(_norows) - len(_hrows) == 3)

# a hallucination between two identical cues must not let them collapse together
_C_SRT = ("1\n00:00:01,000 --> 00:00:01,900\nGo!\n\n"
          "2\n00:00:02,000 --> 00:00:02,900\nThanks for watching!\n\n"
          "3\n00:00:03,000 --> 00:00:03,900\nGo!\n\n"
          "4\n00:00:04,000 --> 00:00:04,900\nThanks for watching!\n\n"
          "5\n00:00:05,000 --> 00:00:05,900\nRun!\n\n"
          "6\n00:00:06,000 --> 00:00:06,900\nThanks for watching!\n")
(WORK / "H2.srt").write_text(_C_SRT, encoding="utf-8")
_crows, _cstats = srt_polish.polish(WORK / "H2.srt", out_path=WORK / "H2.out.srt",
                                    verbose=False)
check("a removed cue still blocks the two cues around it from collapsing",
      _cstats["collapsed_loops"] == 0 and len(_crows) == 3)
check("the twice-repeated real cue survived both times",
      [r[2] for r in _crows] == ["Go!", "Go!", "Run!"])
check("the blocked pair kept their original timestamps",
      [r[1] for r in _crows[:2]] == ["00:00:01,000 --> 00:00:01,900",
                                     "00:00:03,000 --> 00:00:03,900"])

# opt-out is honoured
_prev_asr = config.load().asr
srt_polish.CFG.asr = {"strip_hallucinations": False}
_orows, _ostats = srt_polish.polish(WORK / "H1.srt", out_path=WORK / "H1.keep.srt",
                                    verbose=False)
check("strip_hallucinations:false keeps every cue",
      _ostats["dropped_hallucination"] == 0 and len(_orows) == 8)
srt_polish.CFG.asr = _prev_asr

print("[18] non-UTF-8 subtitles (Latin-1 sidecars)")
# A downloaded Spanish/French sidecar is very often cp1252. Reading it as UTF-8
# raises before a single cue is parsed; reading it with errors="replace" silently
# destroys every accented character. Both were real failures on a real file.
_ES_TEXT = ("1\n00:00:01,000 --> 00:00:03,000\nS\u00ed que suena ra\u00f1o.\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\n\u00bfQu\u00e9 a\u00f1os?\n")
(WORK / "ES.srt").write_bytes(_ES_TEXT.encode("cp1252"))
_txt, _enc = srt_utils.read_text(WORK / "ES.srt")
check("a cp1252 sidecar is decoded, not rejected", _enc == "cp1252")
check("...and its accented characters survive intact", _txt == _ES_TEXT)
check("no U+FFFD replacement characters were introduced", "\ufffd" not in _txt)

(WORK / "UTF.srt").write_text(_ES_TEXT, encoding="utf-8")
check("a real UTF-8 file is reported as utf-8",
      srt_utils.read_text(WORK / "UTF.srt")[1] == "utf-8")
(WORK / "ASCII.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nPlain.\n",
                                encoding="utf-8")
check("pure ASCII is utf-8, so the check never fires spuriously",
      srt_utils.read_text(WORK / "ASCII.srt")[1] == "utf-8")
(WORK / "BOM2.srt").write_bytes(b"\xef\xbb\xbf" + _ES_TEXT.encode("utf-8"))
_btxt, _benc = srt_utils.read_text(WORK / "BOM2.srt")
check("a BOM is preserved for the auditor rather than silently eaten",
      _benc == "utf-8" and _btxt.startswith(srt_utils.BOM))

# QA must call it out as HARD: it corrupts every accented glyph on screen
_eh, _es = srt_qa.qa(WORK / "ES.srt", verbose=False)
check("QA flags a non-UTF-8 subtitle as HARD", _eh >= 1)
_qbuf2 = io.StringIO()
with redirect_stdout(_qbuf2):
    srt_qa.qa(WORK / "ES.srt", top=20)
check("QA names the actual encoding", "cp1252" in _qbuf2.getvalue())
check("QA does not flag an equivalent UTF-8 file",
      srt_qa.qa(WORK / "UTF.srt", verbose=False)[0] == 0)

# polish converts it, and that is the whole fix for such a file
srt_polish.polish(WORK / "ES.srt", out_path=WORK / "ES.out.srt", verbose=False)
_otxt, _oenc = srt_utils.read_text(WORK / "ES.out.srt")
check("polish rewrote the sidecar as UTF-8", _oenc == "utf-8")
check("polish did not corrupt the accented characters",
      "S\u00ed" in _otxt and "\u00bfQu\u00e9 a\u00f1os?" in _otxt
      and "\ufffd" not in _otxt)
check("the converted file has no BOM", not _otxt.startswith(srt_utils.BOM))
check("the converted file now passes QA cleanly",
      srt_qa.qa(WORK / "ES.out.srt", verbose=False)[0] == 0)
_ecues, _ = srt_utils.parse_srt(_otxt, strict=True)
_scues, _ = srt_utils.parse_srt(_ES_TEXT, strict=True)
check("conversion preserved every cue and its timing",
      [c["ts"] for c in _ecues] == [c["ts"] for c in _scues])

# --reencode-only must change the bytes and NOTHING else: a distributor's line
# breaks are often deliberate and its timings are not ours to nudge.
_WRAPPY = ("1\n00:00:01,000 --> 00:00:03,000\nUna l\u00ednea corta\ny otra corta\n\n"
           "2\n00:00:03,500 --> 00:00:03,900\n\u00a1R\u00e1pido!\n\n"
           "3\n00:00:05,000 --> 00:00:07,000\n\u00bfQu\u00e9 a\u00f1os?\n")
(WORK / "RE.srt").write_bytes(_WRAPPY.encode("cp1252"))
_rrows, _ = srt_polish.polish(WORK / "RE.srt", out_path=WORK / "RE.out.srt",
                              verbose=False, reencode_only=True)
_rtxt, _renc = srt_utils.read_text(WORK / "RE.out.srt")
_rin, _ = srt_utils.parse_srt(_WRAPPY, strict=True)
_rout, _ = srt_utils.parse_srt(_rtxt, strict=True)
check("--reencode-only produced UTF-8", _renc == "utf-8")
check("--reencode-only kept every timestamp",
      [c["ts"] for c in _rout] == [c["ts"] for c in _rin])
check("--reencode-only kept every line break",
      [c["text"] for c in _rout] == [c["text"] for c in _rin])
check("--reencode-only kept the cue count and numbering",
      [c["num"] for c in _rout] == [c["num"] for c in _rin])
check("--reencode-only fixed the accented characters",
      "\u00a1R\u00e1pido!" in _rtxt and "\u00bfQu\u00e9 a\u00f1os?" in _rtxt)
# the full pass on the same input SHOULD reflow/retime - proving the modes differ
_frows, _fstats = srt_polish.polish(WORK / "RE.srt", out_path=WORK / "RE.full.srt",
                                    verbose=False)
check("...whereas the full polish does reflow or retime it",
      _fstats["rewrapped"] + _fstats["extended_short"] + _fstats["cps_relieved"] > 0)

# a cue whose text OPENS with a blank line: rebuilding through the writer would
# silently drop that line, so reencode-only must emit the decoded source instead
_ODD = ("1\n00:00:01,000 --> 00:00:03,000\n\nHola\n\n"
        "2\n00:00:04,000 --> 00:00:05,000\nA\u00fan\n\nm\u00e1s\n")
(WORK / "ODD.srt").write_bytes(_ODD.encode("cp1252"))
_oin, _ = srt_utils.parse_srt(_ODD, strict=False)
srt_polish.polish(WORK / "ODD.srt", out_path=WORK / "ODD.out.srt",
                  verbose=False, reencode_only=True)
_otxt2, _oenc2 = srt_utils.read_text(WORK / "ODD.out.srt")
_oout, _ = srt_utils.parse_srt(_otxt2, strict=False)
check("reencode-only preserves a leading blank line inside a cue",
      [c["text"] for c in _oout] == [c["text"] for c in _oin] and _oenc2 == "utf-8")

# an already-UTF-8 file must be left completely alone, .bak included
(WORK / "OK8.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nYa est\u00e1.\n",
                              encoding="utf-8")
_before8 = (WORK / "OK8.srt").read_bytes()
srt_polish.polish(WORK / "OK8.srt", verbose=False, reencode_only=True)
check("reencode-only does not rewrite a file that is already UTF-8",
      (WORK / "OK8.srt").read_bytes() == _before8)
check("...and does not create a .bak for it",
      not (WORK / "OK8.srt.bak").exists())

# BOM-marked encodings are detected outright rather than guessed at
(WORK / "U16.srt").write_bytes(
    "1\n00:00:01,000 --> 00:00:02,000\n\u00bfQu\u00e9?\n".encode("utf-16"))
_utxt, _uenc = srt_utils.read_text(WORK / "U16.srt")
check("a UTF-16 subtitle is decoded by its BOM, not mangled as cp1252",
      _uenc.startswith("utf-16") and "\u00bfQu\u00e9?" in _utxt)
_ucues, _uprob = srt_utils.parse_srt(_utxt, strict=False)
check("...and then parses normally", len(_ucues) == 1 and not _uprob)

# an explicit override wins over the fallback chain (the cp1251 Cyrillic case a
# reviewer found: nothing can tell it from cp1252 by bytes alone)
_CYR = "1\n00:00:01,000 --> 00:00:02,000\n\u041f\u0440\u0438\u0432\u0435\u0442\n"
(WORK / "CYR.srt").write_bytes(_CYR.encode("cp1251"))
check("without help, cp1251 is silently read as cp1252 (documented limit)",
      srt_utils.read_text(WORK / "CYR.srt")[1] == "cp1252")
check("--encoding recovers it exactly",
      srt_utils.read_text(WORK / "CYR.srt", encoding="cp1251")[0] == _CYR)
expect_raises("a bogus --encoding fails loud",
              lambda: srt_utils.read_text(WORK / "CYR.srt", encoding="not-a-codec"))

print("[19] --shift (a sidecar that is uniformly late)")
_SH = ("1\n00:00:10,000 --> 00:00:12,500\nPrimera\nl\u00ednea\n\n"
       "2\n00:00:20,250 --> 00:00:21,000\n\u00a1Ya!\n\n"
       "3\n00:01:00,000 --> 00:01:03,000\n- Uno.\n- Dos.\n")
(WORK / "SH.srt").write_text(_SH, encoding="utf-8")
_shin, _ = srt_utils.parse_srt(_SH, strict=True)
srt_polish.polish(WORK / "SH.srt", out_path=WORK / "SH.out.srt", verbose=False,
                  shift_ms=-500)
_shtxt, _ = srt_utils.read_text(WORK / "SH.out.srt")
_shout, _ = srt_utils.parse_srt(_shtxt, strict=True)
check("shift moved every cue by exactly -500 ms",
      all(srt_utils.ms(srt_utils.cue_bounds(n["ts"])[0])
          == srt_utils.ms(srt_utils.cue_bounds(o["ts"])[0]) - 500
          for o, n in zip(_shin, _shout)))
check("shift moved cue ENDS by the same amount",
      all(srt_utils.ms(srt_utils.cue_bounds(n["ts"])[1])
          == srt_utils.ms(srt_utils.cue_bounds(o["ts"])[1]) - 500
          for o, n in zip(_shin, _shout)))
check("shift preserved every duration",
      [srt_utils.ms(srt_utils.cue_bounds(c["ts"])[1])
       - srt_utils.ms(srt_utils.cue_bounds(c["ts"])[0]) for c in _shout]
      == [srt_utils.ms(srt_utils.cue_bounds(c["ts"])[1])
          - srt_utils.ms(srt_utils.cue_bounds(c["ts"])[0]) for c in _shin])
check("shift preserved text, line breaks and numbering",
      [c["text"] for c in _shout] == [c["text"] for c in _shin]
      and [c["num"] for c in _shout] == [c["num"] for c in _shin])
check("a known cue landed exactly where arithmetic says",
      _shout[1]["ts"] == "00:00:19,750 --> 00:00:20,500")

# positive shift for a subtitle that runs early
srt_polish.polish(WORK / "SH.srt", out_path=WORK / "SH.late.srt", verbose=False,
                  shift_ms=1250)
_lt, _ = srt_utils.parse_srt(srt_utils.read_text(WORK / "SH.late.srt")[0], strict=True)
check("a positive shift moves cues later",
      _lt[0]["ts"] == "00:00:11,250 --> 00:00:13,750")

# clamping: a cue that would land before zero keeps its DURATION
_EARLY = "1\n00:00:00,200 --> 00:00:02,200\nInicio\n\n2\n00:00:30,000 --> 00:00:31,000\nLuego\n"
(WORK / "SHC.srt").write_text(_EARLY, encoding="utf-8")
srt_polish.polish(WORK / "SHC.srt", out_path=WORK / "SHC.out.srt", verbose=False,
                  shift_ms=-500)
_ct, _ = srt_utils.parse_srt(srt_utils.read_text(WORK / "SHC.out.srt")[0], strict=True)
check("a cue that would go negative is clamped to zero",
      _ct[0]["ts"].startswith("00:00:00,000"))
check("...and keeps its full duration rather than being truncated",
      _ct[0]["ts"] == "00:00:00,000 --> 00:00:02,000")
check("...while later cues still get the full shift",
      _ct[1]["ts"] == "00:00:29,500 --> 00:00:30,500")

# a shift must not quietly reflow or drop anything the way a full polish would
_SHW = ("1\n00:00:10,000 --> 00:00:10,300\nUna l\u00ednea corta\ny otra corta\n\n"
        "2\n00:00:20,000 --> 00:00:21,000\nOtra\n")
(WORK / "SHW.srt").write_text(_SHW, encoding="utf-8")
_sw, _ = srt_polish.polish(WORK / "SHW.srt", out_path=WORK / "SHW.out.srt",
                           verbose=False, shift_ms=-500)
_swt, _ = srt_utils.parse_srt(srt_utils.read_text(WORK / "SHW.out.srt")[0], strict=True)
_swi, _ = srt_utils.parse_srt(_SHW, strict=True)
check("shift left a short cue short and a two-line cue two-line",
      [c["text"] for c in _swt] == [c["text"] for c in _swi])

# dry-run writes nothing
_pre = (WORK / "SH.srt").read_bytes()
srt_polish.polish(WORK / "SH.srt", verbose=False, shift_ms=-500, dry_run=True)
check("--shift --dry-run does not touch the file",
      (WORK / "SH.srt").read_bytes() == _pre)

# shift and re-encode compose in one pass
(WORK / "SHE.srt").write_bytes(_SH.encode("cp1252"))
srt_polish.polish(WORK / "SHE.srt", out_path=WORK / "SHE.out.srt", verbose=False,
                  shift_ms=-500)
_set, _senc = srt_utils.read_text(WORK / "SHE.out.srt")
_sec, _ = srt_utils.parse_srt(_set, strict=True)
check("a shift also converts a Latin-1 sidecar to UTF-8",
      _senc == "utf-8" and "\u00a1Ya!" in _set
      and _sec[1]["ts"] == "00:00:19,750 --> 00:00:20,500")

# === summary =================================================================
print(f"\n{PASS} passed, {FAIL} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
