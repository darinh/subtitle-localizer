"""Probe a title's video, pick the right TEXT subtitle stream, and extract it to
work/<key>.src.srt  (then run parse_source.py).

Real releases carry multiple subtitle tracks: SDH / non-SDH / forced-narrative /
commentary / signs-songs, sometimes bitmap (PGS/VOBSUB) which CANNOT be dumped to
text. This selects deliberately and FAILS LOUD rather than localizing the wrong
track:
  * ranks text streams by language match, SDH preference, non-forced, non-commentary;
  * BITMAP-only source  -> stop with OCR guidance (run Subtitle Edit / pgsrip first);
  * >1 viable candidate -> write a preflight report and require a choice
    (streams.index_override in project.yaml, or --index N, or --yes to take the top).

Requires ffprobe + ffmpeg on PATH.
  python extract.py S01E02 [S01E03 ...]      # specific titles
  python extract.py --all                     # every discovered title
  python extract.py S01E02 --index 3          # force stream index 3
"""
import os
import re
import sys
import json
import subprocess

import config

CFG = config.load()
WORK = CFG.work
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
BITMAP_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvdsub", "dvb_subtitle", "pgssub"}


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def probe_streams(video):
    r = _run(["ffprobe", "-v", "error", "-select_streams", "s", "-of", "json",
              "-show_entries",
              "stream=index,codec_name:stream_disposition=default,forced,hearing_impaired:"
              "stream_tags=language,title", str(video)])
    if r.returncode != 0:
        raise SystemExit(f"ffprobe failed on {video}:\n{r.stderr.strip()}")
    data = json.loads(r.stdout or "{}")
    return data.get("streams", [])


def score(stream, prefs):
    tags = stream.get("tags", {}) or {}
    disp = stream.get("disposition", {}) or {}
    lang = (tags.get("language") or "").lower()
    title = (tags.get("title") or "").lower()
    codec = (stream.get("codec_name") or "").lower()
    if codec in BITMAP_CODECS:
        return None  # not extractable to text
    if codec not in TEXT_CODECS:
        return None
    if prefs.get("exclude_commentary", True) and "commentary" in title:
        return None
    forced = disp.get("forced") or ("forced" in title)
    if forced and not prefs.get("allow_forced", False):
        return None
    s = 0
    pl = (prefs.get("prefer_language") or CFG.source_language or "en").lower()
    if lang.startswith(pl) or pl.startswith(lang) and lang:
        s += 100
    elif lang in ("", "und"):
        s += 10            # unknown language: usable but unsure
    is_sdh = bool(disp.get("hearing_impaired")) or "sdh" in title or "hearing" in title
    if prefs.get("prefer_sdh", True) and is_sdh:
        s += 20
    if disp.get("default"):
        s += 2
    return s


def choose(video, key, forced_index=None):
    streams = probe_streams(video)
    if not streams:
        raise SystemExit(f"FAIL: {key}: no subtitle streams in {video.name} — supply an external SRT.")
    override = (CFG.streams.get("index_override") or {}).get(key)
    pick_idx = forced_index if forced_index is not None else override
    report = [f"# stream preflight: {key}  ({video.name})"]
    cand = []
    for st in streams:
        sc = score(st, CFG.streams)
        tags = st.get("tags", {}) or {}
        report.append(f"  idx={st['index']:>2} codec={st.get('codec_name'):<18} "
                      f"lang={tags.get('language','?'):<4} forced={(st.get('disposition') or {}).get('forced',0)} "
                      f"sdh={(st.get('disposition') or {}).get('hearing_impaired',0)} "
                      f"title={tags.get('title','')!r} score={sc}")
        if sc is not None:
            cand.append((sc, st["index"]))
    (WORK).mkdir(parents=True, exist_ok=True)
    (WORK / f"{key}.streams.txt").write_text("\n".join(report), encoding="utf-8")

    if pick_idx is not None:
        return pick_idx
    if not cand:
        bitmap = [s for s in streams if (s.get("codec_name") or "").lower() in BITMAP_CODECS]
        if bitmap:
            raise SystemExit(
                f"FAIL: {key}: only BITMAP subtitles (PGS/VOBSUB) found — ffmpeg cannot dump these to text.\n"
                f"  OCR them first (Subtitle Edit / pgsrip) into an external SRT, place it at "
                f"{WORK / (key + '.src.srt')}, then run parse_source.py {key}.\n  See {WORK / (key + '.streams.txt')}")
        raise SystemExit(f"FAIL: {key}: no usable TEXT subtitle stream. See {WORK / (key + '.streams.txt')}")
    cand.sort(reverse=True)
    top = cand[0]
    tie = [i for sc, i in cand if sc == top[0]]
    if len(tie) > 1 and "--yes" not in sys.argv:
        raise SystemExit(
            f"FAIL: {key}: {len(tie)} equally-ranked subtitle candidates (indices {tie}).\n"
            f"  Choose one: set streams.index_override.{key} in project.yaml, or pass --index N, "
            f"or --yes to take index {top[1]}.\n  See {WORK / (key + '.streams.txt')}")
    return top[1]


def extract(key, forced_index=None):
    video = CFG.video_for(key)
    if not video or not video.exists():
        raise SystemExit(f"FAIL: no video found for {key} (check media_root / layout).")
    idx = choose(video, key, forced_index)
    dst = WORK / f"{key}.src.srt"
    r = _run(["ffmpeg", "-y", "-i", str(video), "-map", f"0:{idx}", str(dst), "-loglevel", "error"])
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise SystemExit(f"FAIL: ffmpeg extract of stream {idx} for {key}:\n{r.stderr.strip()}")
    print(f"{key}: extracted subtitle stream 0:{idx} -> {dst.name}")
    return dst


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    forced = None
    if "--index" in sys.argv:
        forced = int(sys.argv[sys.argv.index("--index") + 1])
    keys = [k for k, _ in CFG.titles()] if "--all" in sys.argv else args
    if not keys:
        raise SystemExit(__doc__)
    for k in keys:
        extract(k, forced)
