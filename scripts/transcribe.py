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
  * strips ASR artifacts: repetition loops, zero-width/again-and-again fillers,
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

CFG = config.load()
WORK = CFG.work

DEFAULT_MODEL = "large-v3"
GAP_SPLIT = 0.65          # a silence this long is a natural cue boundary
MAX_CUE_SECONDS = 7.0
LEAD_IN = 0.08            # nudge cue start earlier; ASR word starts run late
TAIL = 0.35               # let a cue linger past the last word if silence allows
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

    A real line can repeat a word twice ("no, no"); whisper loops emit the same
    token 5+ times, or the same short phrase over and over. Keep the first two.
    """
    out, run = [], []

    def flush():
        if not run:
            return
        out.extend(run[:2] if len(run) > 4 else run)

    for w in words:
        tok = re.sub(r"[\W_]+", "", w["word"].lower())
        if run and tok and tok == re.sub(r"[\W_]+", "", run[-1]["word"].lower()):
            run.append(w)
            continue
        flush()
        run = [w]
    flush()

    # phrase-level loop: identical consecutive n-grams (n=2..4) repeated 3+ times
    for n in (4, 3, 2):
        i, res = 0, []
        while i < len(out):
            gram = [re.sub(r"[\W_]+", "", x["word"].lower()) for x in out[i:i + n]]
            if len(gram) == n and any(gram):
                reps = 1
                j = i + n
                while j + n <= len(out) and [
                        re.sub(r"[\W_]+", "", x["word"].lower()) for x in out[j:j + n]] == gram:
                    reps += 1
                    j += n
                if reps >= 3:
                    res.extend(out[i:i + n * 2])       # keep two repetitions
                    i = j
                    continue
            res.append(out[i])
            i += 1
        out = res
    return out


def _split_words(words, max_chars):
    """Group word dicts into cue-sized chunks under the readability budget."""
    cues, cur = [], []

    def cur_len():
        return len(" ".join(_clean_word(w["word"]) for w in cur).strip())

    for i, w in enumerate(words):
        prev = cur[-1] if cur else None
        gap = (w["start"] - prev["end"]) if prev else 0.0
        span = (w["end"] - cur[0]["start"]) if cur else 0.0
        nxt = len(_clean_word(w["word"])) + (1 if cur else 0)
        if cur and (cur_len() + nxt > max_chars or gap >= GAP_SPLIT or span > MAX_CUE_SECONDS):
            cues.append(cur)
            cur = []
        cur.append(w)
        txt = " ".join(_clean_word(x["word"]) for x in cur).strip()
        # prefer to break at a sentence end once the cue has real substance
        if SENT_END.search(txt) and len(txt) >= max_chars * 0.45:
            cues.append(cur)
            cur = []
    if cur:
        cues.append(cur)

    # second pass: a cue still over budget is split at its best clause boundary
    final = []
    for c in cues:
        txt = " ".join(_clean_word(w["word"]) for w in c).strip()
        if len(txt) <= max_chars or len(c) < 2:
            final.append(c)
            continue
        best, bi = None, None
        for i in range(1, len(c)):
            head = " ".join(_clean_word(w["word"]) for w in c[:i]).strip()
            if len(head) > max_chars:
                break
            score = (2 if CLAUSE_END.search(head) else 0) + (1 if len(head) >= max_chars * 0.4 else 0)
            if best is None or score >= best:
                best, bi = score, i
        bi = bi or max(1, len(c) // 2)
        final.append(c[:bi])
        final.append(c[bi:])
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
        c["end"] = min(max(c["end"] + TAIL, c["start"] + min_dur), room)
        need = len(c["text"].replace(" ", "")) / max_cps          # seconds for target CPS
        if (c["end"] - c["start"]) < need:
            c["end"] = min(c["start"] + need, room)
    return [c for c in cues if c["end"] > c["start"]]


def transcribe(key, model_name=DEFAULT_MODEL, device="auto", audio=None,
               astream=None, language=None, center=True, vad=False):
    video = CFG.video_for(key)
    if audio is None:
        if not video or not video.exists():
            raise SystemExit(f"FAIL: no video found for {key} (check media_root / layout).")
    lang = (language or CFG.source_language or "en").lower()
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
    def one(t):
        t = max(t, 0.0)
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int(round((t - int(t)) * 1000))
        if ms == 1000:
            ms, s = 0, s + 1
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{one(a)} --> {one(b)}"


if __name__ == "__main__":
    argv = sys.argv[1:]

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    keys = [a for a in argv if not a.startswith("--")]
    skip = set()
    for flag in ("--model", "--device", "--audio", "--astream", "--language"):
        if flag in argv:
            skip.add(argv[argv.index(flag) + 1])
    keys = [k for k in keys if k not in skip]
    if not keys:
        raise SystemExit(__doc__)
    astream = opt("--astream")
    for k in keys:
        transcribe(k, model_name=opt("--model", DEFAULT_MODEL),
                   device=opt("--device", "auto"), audio=opt("--audio"),
                   astream=int(astream) if astream is not None else None,
                   language=opt("--language"), center="--no-center" not in argv)
