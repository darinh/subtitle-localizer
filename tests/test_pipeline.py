"""Self-tests for every integrity guarantee. Creates an isolated temp project +
work dir, points SUBLOC_CONFIG at it, then exercises the pipeline on synthetic data
(no ffmpeg/media needed except a dummy video file for the delivery step).

  python tests/test_pipeline.py        # exits non-zero on any failure
"""
import os
import sys
import json
import shutil
import tempfile
import traceback
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
target: {{ guardrails: es-419, srt_suffix: ".es-419.srt" }}
paths: {{ media_root: "{MEDIA.as_posix()}", work_dir: "{WORK.as_posix()}" }}
layout:
  kind: movie
  titles:
    - {{ key: "T01", video: "T01.mkv" }}
    - {{ key: "M01", video: "M01.mkv" }}
    - {{ key: "DUP", video: "DUP.mkv" }}
slices_per_title: 4
features: {{ allow_merge: true }}
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


def expect_raises(name, fn):
    global PASS, FAIL
    try:
        fn()
        FAIL += 1
        print(f"  FAIL {name} (expected an error)")
    except SystemExit:
        PASS += 1
        print(f"  ok   {name} (raised)")
    except Exception as e:  # noqa
        PASS += 1
        print(f"  ok   {name} (raised {type(e).__name__})")


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
              lambda: srt_utils.parse_srt("1\nnot-a-timestamp\nX\n", strict=True))

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

# === summary =================================================================
print(f"\n{PASS} passed, {FAIL} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
