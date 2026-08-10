"""Create source-language subtitles by ASR when a title has NO text subtitle track.

`extract.py` deliberately fails loud when a release ships only bitmap (PGS/VOBSUB)
or foreign-language subtitles. This is the other escape hatch: transcribe the
title's own source-language audio with faster-whisper and emit the SAME artifact
`extract.py` would have produced — ``work/<key>.src.srt`` — so every downstream
stage (parse_source -> slice_ranges -> build -> validate -> align) is unchanged.

Why this is not just "dump whisper's segments to SRT": an ASR segment is a
breath-group, not a subtitle. This re-segments from WORD timestamps under the
project's own readability budget (max_cpl / max_lines / max_cps), so the result
obeys the same rules the validator later enforces:

  * splits at sentence end, then at clause punctuation, then on a silence gap,
    and only then on the character budget;
  * never emits a cue longer than max_lines * max_cpl characters;
  * grows a too-fast cue into the following silence (never past the next cue)
    to pull CPS down, and enforces min_duration;
  * strips ASR artifacts: repetition loops, boilerplate hallucinations ("Thanks for
    watching!" emitted over music/silence), zero-width/again-and-again fillers,
    and cues that are pure punctuation;
  * guarantees strictly ordered, non-overlapping timestamps.

Timestamps produced here are ASR estimates, not a distributor's - they are the
one part of the pipeline that is NOT sacred, so review them before delivery.

  python transcribe.py the-film-2010                 # extracts audio itself
  python transcribe.py the-film-2010 --audio a.wav   # reuse a prepared 16k wav
  python transcribe.py the-film-2010 --no-center     # do not isolate the 5.1 center
  python transcribe.py the-film-2010 --vad           # re-enable the VAD pre-pass
  python transcribe.py the-film-2010 --model large-v3 --device cuda
"""
import os
import re
import sys
import json
import subprocess

import config
import srt_utils
import asr_artifacts

CFG = config.load()
WORK = CFG.work

DEFAULT_MODEL = "large-v3"
GAP_SPLIT = 0.65          # a silence this long is a natural cue boundary
MAX_CUE_SECONDS = 7.0
LEAD_IN = 0.08            # nudge cue start earlier; ASR word starts run late
TAIL = 0.35               # let a cue linger past the last word if silence allows
LOOP_GAP = 0.45           # repetition beyond this silence is real dialogue, not a loop
# even out whispered vs shouted delivery so quiet dialogue survives the encode
DYNAUDNORM = "dynaudnorm=f=150:g=15"
SILENT_DBFS = -50.0       # below this mean level a rendered channel carries no dialogue
SENT_END = re.compile(r"[.!?\u2026][\"'\u201d\u2019)]*$")
CLAUSE_END = re.compile(r"[,;:\u2014-][\"'\u201d\u2019)]*$")
_PUNCT_ONLY = re.compile(r"^[\W_]+$", re.U)


def _add_cuda_dll_dirs():
    """pip's nvidia-* wheels ship the cuBLAS/cuDNN DLLs inside site-packages; on
    Windows they are not on PATH, so CTranslate2 fails to load CUDA. Register them."""
    if os.name != "nt":
        return
    try:
        import nvidia
    except ImportError:
        return
    for root in nvidia.__path__:
        for dirpath, dirnames, filenames in os.walk(root):
            if os.path.basename(dirpath) == "bin" and any(f.endswith(".dll") for f in filenames):
                os.add_dll_directory(dirpath)
                os.environ["PATH"] = dirpath + os.pathsep + os.environ.get("PATH", "")


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def pick_audio_stream(video, lang):
    """Index of the best source-language audio stream (fail loud if none)."""
    r = _run(["ffprobe", "-v", "error", "-select_streams", "a", "-of", "json",
              "-show_entries", "stream=index,codec_name,channels:"
              "stream_disposition=default,comment:stream_tags=language,title", str(video)])
    if r.returncode != 0:
        raise SystemExit(f"ffprobe failed on {video}:\n{r.stderr.strip()}")
    streams = (json.loads(r.stdout or "{}")).get("streams", [])
    if not streams:
        raise SystemExit(f"FAIL: no audio streams in {video.name}")
    best, best_score = None, None
    for st in streams:
        tags = st.get("tags", {}) or {}
        disp = st.get("disposition", {}) or {}
        slang = (tags.get("language") or "").lower()
        title = (tags.get("title") or "").lower()
        if disp.get("comment") or "commentary" in title or "descriptive" in title:
            continue
        s = 0
        if slang.startswith(lang) or (slang and lang.startswith(slang)):
            s += 100
        elif slang in ("", "und"):
            s += 10
        if disp.get("default"):
            s += 5
        s += min(int(st.get("channels") or 0), 8)      # prefer the full mix
        if best_score is None or s > best_score:
            best, best_score = st, s
    if best is None or best_score < 10:
        raise SystemExit(
            f"FAIL: no '{lang}' audio stream in {video.name} — this release may be "
            f"foreign-dub only. Pass --astream N to force one.")
    return best["index"]


def _mean_volume(path):
    """Mean dBFS of a wav via ffmpeg volumedetect (silence -> very negative)."""
    r = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
              "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", r.stderr or "")
    return float(m.group(1)) if m else None


def _encode(video, idx, dst, afilter):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-map", f"0:{idx}", "-vn"]
    if afilter:
        cmd += ["-af", afilter]
    cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]
    return _run(cmd)


def extract_audio(video, key, lang, astream=None, center=True):
    """Render the source-language audio to the 16 kHz mono wav the ASR wants.

    On a 5.1/7.1 mix the dialogue sits almost entirely in the FRONT CENTER channel;
    a naive full downmix buries it under score and effects and measurably costs
    recognition (A/B on one 4-min action-film window: 197 words from the downmix
    vs 311 from the isolated, level-evened center — +58%). So isolate FC and even
    the level out. Some releases ship a silent or absent FC, which would quietly
    yield an empty transcript, so prove the render carries signal and fall back.
    """
    dst = WORK / f"{key}.asr.wav"
    idx = astream if astream is not None else pick_audio_stream(video, lang)
    WORK.mkdir(parents=True, exist_ok=True)

    channels = 0
    r = _run(["ffprobe", "-v", "error", "-select_streams", str(idx),
              "-show_entries", "stream=channels", "-of", "default=nw=1:nk=1", str(video)])
    if r.returncode == 0 and (r.stdout or "").strip().isdigit():
        channels = int(r.stdout.strip())

    use_center = center and channels >= 6
    res = _encode(video, idx, dst, ("pan=mono|c0=FC," if use_center else "") + DYNAUDNORM)
    if res.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise SystemExit(f"FAIL: ffmpeg could not extract audio stream {idx}:\n{res.stderr.strip()}")

    if use_center:
        mv = _mean_volume(dst)
        if mv is None or mv < SILENT_DBFS:
            print(f"   WARN center channel is effectively silent (mean {mv} dBFS) — this "
                  f"release does not carry dialogue in FC; falling back to the full downmix")
            res = _encode(video, idx, dst, DYNAUDNORM)
            if res.returncode != 0:
                raise SystemExit(f"FAIL: ffmpeg downmix fallback failed:\n{res.stderr.strip()}")
            use_center = False
    print(f"{key}: extracted audio 0:{idx} "
          f"({'center channel' if use_center else str(channels or '?') + 'ch downmix'}) -> {dst.name}")
    return dst


# ---------------------------------------------------------------- segmentation
def _clean_word(w):
    return w.strip()


def _collapse_loops(words):
    """Drop ASR repetition loops: the same token repeated far past natural usage.

    Repetition is only a loop when it is TEMPORALLY CONTIGUOUS. A character can
    shout the same word again a scene later, and that is real dialogue; a decoder
    loop emits the same token back-to-back with no silence between. So every
    comparison below is gated on the words being within LOOP_GAP of each other,
    and a silence resets the run. A real line can repeat a word twice ("no, no"),
    so only runs longer than four are trimmed, and then back to two.
    """
    out, run = [], []

    def flush():
        if not run:
            return
        out.extend(run[:2] if len(run) > 4 else run)

    for w in words:
        tok = re.sub(r"[\W_]+", "", w["word"].lower())
        if run and tok and tok == re.sub(r"[\W_]+", "", run[-1]["word"].lower()) \
                and (w["start"] - run[-1]["end"]) <= LOOP_GAP:
            run.append(w)
            continue
        flush()
        run = [w]
    flush()

    # phrase-level loop: identical consecutive n-grams (n=2..4) repeated 3+ times
    # back-to-back. The same gate applies: a gap between repetitions means the
    # actor said it again, not that the decoder stuttered.
    def _toks(ws):
        return [re.sub(r"[\W_]+", "", x["word"].lower()) for x in ws]

    for n in (4, 3, 2):
        i, res = 0, []
        while i < len(out):
            gram = _toks(out[i:i + n])
            if len(gram) == n and any(gram):
                reps, j = 1, i + n
                while j + n <= len(out) and _toks(out[j:j + n]) == gram \
                        and (out[j]["start"] - out[j - 1]["end"]) <= LOOP_GAP:
                    reps += 1
                    j += n
                if reps >= 4:
                    res.extend(out[i:i + n * 2])       # keep two repetitions
                    i = j
                    continue
            res.append(out[i])
            i += 1
        out = res
    return out


def _split_words(words, max_chars):
    """Group word dicts into cue-sized chunks under the readability budget.

    A single pass that hard-breaks the instant the budget is hit guarantees the
    budget but chops mid-clause. So when the budget forces a break, look BACK over
    the words already accumulated for the last clause boundary and break there
    instead, carrying the remainder into the next cue. The budget stays a hard
    guarantee; the break just lands where a reader expects one.
    """
    cues, cur = [], []

    def text_of(ws):
        return " ".join(_clean_word(w["word"]) for w in ws).strip()

    def flush_at_clause():
        """Emit cur, breaking at its last useful clause boundary; return the rest."""
        if len(cur) > 2:
            for i in range(len(cur) - 1, 0, -1):
                head = text_of(cur[:i])
                if CLAUSE_END.search(head) and len(head) >= max_chars * 0.35:
                    cues.append(cur[:i])
                    return cur[i:]
        cues.append(list(cur))
        return []

    for w in words:
        prev = cur[-1] if cur else None
        gap = (w["start"] - prev["end"]) if prev else 0.0
        span = (w["end"] - cur[0]["start"]) if cur else 0.0
        nxt = len(_clean_word(w["word"])) + (1 if cur else 0)
        if cur and len(text_of(cur)) + nxt > max_chars:
            cur = flush_at_clause()
        elif cur and (gap >= GAP_SPLIT or span > MAX_CUE_SECONDS):
            cues.append(list(cur))
            cur = []
        cur.append(w)
        txt = text_of(cur)
        # prefer to break at a sentence end once the cue has real substance
        if SENT_END.search(txt) and len(txt) >= max_chars * 0.45:
            cues.append(list(cur))
            cur = []
    if cur:
        cues.append(list(cur))

    # safety net: a clause remainder carried forward can itself exceed the budget,
    # so keep splitting any surviving over-budget group until every cue fits.
    final = []
    for c in cues:
        queue = [c]
        while queue:
            g = queue.pop(0)
            if len(text_of(g)) <= max_chars or len(g) < 2:
                final.append(g)
                continue
            best, bi = None, None
            for i in range(1, len(g)):
                head = text_of(g[:i])
                if len(head) > max_chars:
                    break
                score = (2 if CLAUSE_END.search(head) else 0) + \
                        (1 if len(head) >= max_chars * 0.4 else 0)
                if best is None or score >= best:
                    best, bi = score, i
            bi = bi or max(1, len(g) // 2)
            final.append(g[:bi])
            queue.insert(0, g[bi:])
    return [c for c in final if c]


def build_cues(words, cfg_read):
    max_cpl = int(cfg_read.get("max_cpl", 42))
    max_lines = int(cfg_read.get("max_lines", 2))
    max_cps = float(cfg_read.get("max_cps", 17))
    min_dur = float(cfg_read.get("min_duration", 0.7))
    max_chars = max_cpl * max_lines

    groups = _split_words(_collapse_loops(words), max_chars)
    cues = []
    for g in groups:
        text = " ".join(_clean_word(w["word"]) for w in g).strip()
        text = re.sub(r"\s+", " ", text)
        if not text or _PUNCT_ONLY.match(text):
            continue
        cues.append({"start": g[0]["start"], "end": g[-1]["end"], "text": text})

    # --- timing hygiene: order, lead-in, min duration, CPS relief, no overlap ---
    cues.sort(key=lambda c: (c["start"], c["end"]))
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else c["end"] + 5.0
        prev_end = cues[i - 1]["end"] if i else 0.0
        c["start"] = max(prev_end + 0.001, c["start"] - LEAD_IN, 0.0)
        room = max(nxt - 0.04, c["start"] + 0.05)
        floor = (srt_utils.ms(c["start"]) + srt_utils.ms(min_dur)) / 1000.0
        c["end"] = min(max(c["end"] + TAIL, floor), room)
        need = len(c["text"].replace(" ", "")) / max_cps          # seconds for target CPS
        if (c["end"] - c["start"]) < need:
            c["end"] = min(c["start"] + need, room)
    return [c for c in cues if c["end"] > c["start"]]


def _load_model(model_name, device):
    _add_cuda_dll_dirs()
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("FAIL: faster-whisper is not installed.  pip install faster-whisper")
    if device == "auto":
        device = "cuda"
    compute = "float16" if device == "cuda" else "int8"
    try:
        return WhisperModel(model_name, device=device, compute_type=compute), device, compute
    except Exception as e:                                # noqa: BLE001 - fall back loudly
        if device != "cuda":
            raise
        print(f"   WARN cuda unavailable ({type(e).__name__}: {e}); falling back to CPU int8")
        return WhisperModel(model_name, device="cpu", compute_type="int8"), "cpu", "int8"


DECODE = dict(task="transcribe", beam_size=5, best_of=5,
              temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
              condition_on_previous_text=False, word_timestamps=True,
              no_speech_threshold=0.6, log_prob_threshold=-1.0,
              compression_ratio_threshold=2.4)


def fill_gaps(key, reference, model_name=DEFAULT_MODEL, device="auto", audio=None,
              tolerance=3.0, pad=2.0):
    """Re-transcribe only the stretches a REFERENCE track says carry dialogue but
    our draft has nothing near, and merge in whatever that finds.

    Whisper decodes long audio in 30-second windows; a window dominated by score,
    screaming or overlapping speech can lose a segment that the very same model
    recovers when handed just that stretch. A professionally cued track for the
    same title is an excellent map of WHERE those losses are.

    The reference is used ONLY for cue TIMING - where dialogue exists. None of its
    text is read, compared or copied; every word merged in is transcribed from the
    title's own audio, so the draft stays an original transcript.
    """
    draft_path = WORK / f"{key}.src.srt"
    if not draft_path.exists():
        raise SystemExit(f"FAIL: no draft at {draft_path} — run transcribe first")
    wav = audio or (WORK / f"{key}.asr.wav")
    if not os.path.exists(str(wav)):
        raise SystemExit(f"FAIL: no audio at {wav} — keep the .asr.wav or pass --audio")

    draft, _ = srt_utils.parse_srt(draft_path.read_text(encoding="utf-8"), strict=True)
    have = sorted(srt_utils.cue_bounds(c["ts"]) for c in draft)
    ref_cues, _ = srt_utils.parse_srt(
        open(reference, encoding="utf-8-sig", errors="replace").read(), strict=False)
    ref = sorted(srt_utils.cue_bounds(c["ts"]) for c in ref_cues)
    if not ref:
        raise SystemExit(f"FAIL: no cues parsed from reference {reference}")

    missing = [(a, b) for a, b in ref
               if not any(not (y <= a - tolerance or x >= b + tolerance) for x, y in have)]
    windows = []
    for a, b in missing:
        if windows and a - windows[-1][1] < 6.0:
            windows[-1][1] = max(windows[-1][1], b)
        else:
            windows.append([a, b])
    windows = [(max(0.0, a - pad), b + pad) for a, b in windows]
    print(f"{key}: reference says {len(missing)} cue(s) have no nearby draft cue "
          f"-> {len(windows)} window(s) to re-scan")
    if not windows:
        return draft_path

    model, device, compute = _load_model(model_name, device)
    lang = (CFG.source_language or "en").lower()
    max_cpl = int(CFG.readability.get("max_cpl", 42))
    clip = WORK / f"{key}._gap.wav"
    added, scanned = [], 0
    for (a, b) in windows:
        r = _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{a:.3f}",
                  "-t", f"{max(b - a, 1.0):.3f}", "-i", str(wav),
                  "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(clip)])
        if r.returncode != 0 or not clip.exists():
            continue
        scanned += 1
        segs, _info = model.transcribe(str(clip), language=lang, vad_filter=False, **DECODE)
        words = []
        for s in segs:
            for w in (s.words or []):
                if w.word is None or w.start is None or w.end is None:
                    continue
                words.append({"word": w.word, "start": float(w.start) + a,
                              "end": float(w.end) + a})
        for c in build_cues(words, CFG.readability):
            # only keep what genuinely fills a hole in the draft
            if any(not (y <= c["start"] or x >= c["end"]) for x, y in have):
                continue
            added.append(c)
    if clip.exists():
        clip.unlink()
    if not added:
        print(f"   re-scanned {scanned} window(s); nothing new recovered")
        return draft_path

    rows = [{"start": s, "end": e, "text": "\n".join(c["text"])}
            for c, (s, e) in zip(draft, have)]
    rows += [{"start": c["start"], "end": c["end"], "text": c["text"]} for c in added]
    rows.sort(key=lambda x: (x["start"], x["end"]))
    # A gap window is exactly the kind of stretch (score, screaming, near-silence)
    # that provokes boilerplate, so re-run the whole-file check over the MERGED
    # track — the recurrence count has to see the finished artifact, not one window.
    before_merge = len(rows)
    rows = _strip_hallucinations(rows, "the merged draft")
    n_halluc = before_merge - len(rows)
    for i, it in enumerate(rows):           # keep the merged track non-overlapping
        if i and it["start"] < rows[i - 1]["end"]:
            it["start"] = rows[i - 1]["end"] + 0.001
        if it["end"] <= it["start"]:
            it["end"] = it["start"] + 0.05
    out = [(i, srt_utils.format_span(it["start"], it["end"]),
            srt_utils.wrap_cue(it["text"], width=max_cpl))
           for i, it in enumerate(rows, 1)]
    srt_utils.write_srt(out, draft_path)
    reparsed, _ = srt_utils.parse_srt(draft_path.read_text(encoding="utf-8"), strict=True)
    if len(reparsed) != len(out):
        raise SystemExit(f"FAIL: merged draft did not re-parse ({len(reparsed)} vs {len(out)})")
    print(f"{key}: recovered {len(added)} cue(s) from {scanned} window(s) "
          f"-> {len(draft)} + {len(added)}"
          f"{f' - {n_halluc} hallucinated' if n_halluc else ''} = {len(out)} cues")
    return draft_path


def transcribe(key, model_name=DEFAULT_MODEL, device="auto", audio=None,
               astream=None, language=None, center=True, vad=False):
    video = CFG.video_for(key)
    if audio is None:
        if not video or not video.exists():
            raise SystemExit(f"FAIL: no video found for {key} (check media_root / layout).")
    lang = (language or CFG.source_language or "en").lower()
    WORK.mkdir(parents=True, exist_ok=True)   # --audio skips extract_audio's mkdir
    wav = audio or extract_audio(video, key, lang, astream, center=center)

    _add_cuda_dll_dirs()
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("FAIL: faster-whisper is not installed.  pip install faster-whisper")

    if device == "auto":
        device = "cuda"
    compute = "float16" if device == "cuda" else "int8"
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute)
    except Exception as e:                                    # noqa: BLE001 - fall back loudly
        if device != "cuda":
            raise
        print(f"   WARN cuda unavailable ({type(e).__name__}: {e}); falling back to CPU int8")
        device, compute = "cpu", "int8"
        model = WhisperModel(model_name, device=device, compute_type=compute)

    print(f"{key}: transcribing with {model_name} on {device}/{compute} (lang={lang})")
    segments, info = model.transcribe(
        str(wav), language=lang, task="transcribe",
        beam_size=5, best_of=5, temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        condition_on_previous_text=False,          # the #1 cause of ASR loop drift
        word_timestamps=True,
        # A Silero VAD pre-pass is the usual advice and it is WRONG for film audio:
        # it scores shouted dialogue buried under score/effects as non-speech and
        # drops it wholesale. Measured on this pipeline against a distributor
        # reference track, over one 4-minute attack sequence: VAD@0.5 covered 33%
        # of reference cues, VAD@0.2 67%, and no VAD 98%. So run VAD-less and
        # suppress the hallucinations VAD was incidentally preventing with the
        # thresholds below, which judge the decoded text rather than the waveform.
        vad_filter=vad,
        vad_parameters={"threshold": 0.2, "min_silence_duration_ms": 250,
                        "speech_pad_ms": 250} if vad else None,
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,           # catches degenerate repeated text
        hallucination_silence_threshold=2.0,       # skip long silences (needs word ts)
    )

    words, nseg = [], 0
    total = float(getattr(info, "duration", 0.0) or 0.0)
    for seg in segments:
        nseg += 1
        for w in (seg.words or []):
            if w.word is None or w.start is None or w.end is None:
                continue
            words.append({"word": w.word, "start": float(w.start), "end": float(w.end)})
        if nseg % 100 == 0:
            pct = (seg.end / total * 100) if total else 0
            print(f"   ... {nseg} segments, {len(words)} words, {seg.end/60:.1f} min ({pct:.0f}%)",
                  flush=True)
    if not words:
        raise SystemExit(f"FAIL: {key}: ASR produced no words — wrong audio stream or silent track?")

    cues = build_cues(words, CFG.readability)
    cues = _strip_hallucinations(cues, "the transcript")
    if not cues:
        raise SystemExit(f"FAIL: {key}: no cues survived segmentation")

    max_cpl = int(CFG.readability.get("max_cpl", 42))
    rows = [(i, _fmt_ts(c["start"], c["end"]), srt_utils.wrap_cue(c["text"], width=max_cpl))
            for i, c in enumerate(cues, 1)]
    dst = WORK / f"{key}.src.srt"
    srt_utils.write_srt(rows, dst)

    # prove the artifact is well-formed under the pipeline's own strict parser
    reparsed, _ = srt_utils.parse_srt(open(dst, encoding="utf-8").read(), strict=True)
    if len(reparsed) != len(rows):
        raise SystemExit(f"FAIL: wrote {len(rows)} cues but re-parsed {len(reparsed)}")
    mins = cues[-1]["end"] / 60.0
    print(f"{key}: ASR -> {len(rows)} cues covering {mins:.1f} min "
          f"(from {len(words)} words / {nseg} segments) -> {dst.name}")
    print(f"   NOTE timestamps are ASR estimates — review before delivery.")
    return dst


def _fmt_ts(a, b):
    return srt_utils.format_span(a, b)


def _strip_hallucinations(cues, label):
    """Drop ASR boilerplate hallucinations from a WHOLE-FILE cue list.

    Deliberately not inside build_cues: build_cues is also called per gap window,
    and the recurrence test that makes deletion safe only means anything when it
    can see the complete artifact. Callers apply it once, to the finished list.
    """
    if not cues or not asr_artifacts.enabled_for_config(CFG):
        return cues
    drop, _matches, _counts = asr_artifacts.find(
        [c["text"] for c in cues],
        asr_artifacts.patterns_from_config(CFG),
        asr_artifacts.min_repeats_from_config(CFG))
    if not drop:
        return cues
    print(f"   dropped {len(drop)} ASR boilerplate hallucination(s) from {label}:")
    for i in sorted(drop)[:10]:
        print(f"      {srt_utils.format_ts(cues[i]['start'])}  "
              f"{asr_artifacts.plain(cues[i]['text'])!r}")
    if len(drop) > 10:
        print(f"      ... +{len(drop)-10} more")
    return [c for i, c in enumerate(cues) if i not in drop]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Create source-language subtitles by ASR when a title has no "
                    "usable text subtitle track.")
    ap.add_argument("keys", nargs="+", help="title key(s) from the project config")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--audio", help="reuse a prepared 16 kHz mono wav")
    ap.add_argument("--astream", type=int, help="force a source audio stream index")
    ap.add_argument("--language", help="override the source language code")
    ap.add_argument("--no-center", dest="center", action="store_false",
                    help="do not isolate the 5.1 front-center channel")
    ap.add_argument("--vad", action="store_true",
                    help="re-enable the Silero VAD pre-pass (drops shouted dialogue)")
    ap.add_argument("--fill-gaps", metavar="REF.srt",
                    help="after transcribing, re-scan stretches a reference track "
                         "cues but the draft has nothing near (timing only)")
    ap.add_argument("--only-fill", action="store_true",
                    help="skip transcription; run the gap fill on an existing draft")
    a = ap.parse_args()
    for k in a.keys:
        if not a.only_fill:
            transcribe(k, model_name=a.model, device=a.device, audio=a.audio,
                       astream=a.astream, language=a.language, center=a.center, vad=a.vad)
        if a.fill_gaps:
            fill_gaps(k, a.fill_gaps, model_name=a.model, device=a.device, audio=a.audio)
