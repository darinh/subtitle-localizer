# subtitle-localizer

Turn a film's or series' **embedded source-language subtitles** into professional
**Latin American Spanish (es-419)** `.srt` sidecars — localized like a human would,
not machine-translated line by line. Content-agnostic: point it at any movie or show,
supply a small config + character bible, and run.

It combines deterministic, fail-loud Python plumbing (extraction, slicing, building,
validation, alignment, recovery) with LLM translation-worker and adversarial-reviewer
agents, gated by a multi-LLM review process. Every language rule and project specific
lives in **config** — nothing about any particular title is hardcoded.

> Defining principle: **translate the story, not the strings.** Comprehend the whole
> work, ground ambiguous lines in the picture, lock conventions in a glossary, then
> translate, validate, and review to consensus.

See **[PLAYBOOK.md](PLAYBOOK.md)** for the full methodology, **[docs/DESIGN.md](docs/DESIGN.md)**
for architecture, **[docs/ORCHESTRATION.md](docs/ORCHESTRATION.md)** for running waves at
scale, and **[docs/LEARNINGS.md](docs/LEARNINGS.md)** for the hard-won "why".

## Requirements
- `ffmpeg` / `ffprobe` on PATH (extraction, frame grabs).
- Python 3.9+ and `pip install -r requirements.txt` (PyYAML).
- Access to LLM agents for translation + review (different models for worker vs reviewer).
- For image-based (PGS/VOBSUB) subtitles: an OCR step first (e.g. Subtitle Edit / pgsrip).
- Optional, only if a release has **no** usable source subtitles at all:
  `pip install faster-whisper` (+ a CUDA GPU) for the ASR fallback below.

## Quickstart
```bash
pip install -r requirements.txt

# 1. Configure your project
cp config/project.example.yaml config/project.yaml
$EDITOR config/project.yaml          # media_root, layout (movie|episodic), etc.
cp templates/glossary.template.md glossary.md
$EDITOR glossary.md                  # characters+genders, register matrix, lexicon

python scripts/config.py             # sanity-check config + title discovery

# 2. Per title: extract + parse the source subtitles
python scripts/extract.py --all      # or:  extract.py S01E01 S01E02
python scripts/parse_source.py S01E01

# 3. Slice for parallel workers, then dispatch translation workers
python scripts/slice_ranges.py S01E01
#   -> give each worker prompts/worker.md with its slice + glossary + guardrails;
#      each writes work/S01E01.tgtNN.txt

# 4. Finalize (consolidate -> autofix -> build -> validate -> align -> deliver)
python scripts/finalize_title.py S01E01

# 5. Adversarial review (different model), then re-finalize
#   -> give a reviewer prompts/reviewer.md; it edits work/S01E01.tgt01.txt
python scripts/finalize_title.py S01E01
```
The delivered sidecar lands next to the video as
`<video-stem><target.srt_suffix>` (default `.es-419.srt`).

> **Movie?** Set `layout.kind: movie` with a `titles:` list and use your title's `key`
> (e.g. `the-film-2026`) in place of `S01E01` — every command is identical. The first
> `finalize_title` delivers a validated draft; after the review pass, re-running it
> delivers the reviewed FINAL (both are gated; a sidecar is never a fragment).

## No usable source subtitles? (ASR fallback)
`extract.py` fails loud when a release ships only bitmap (PGS/VOBSUB) or
foreign-language tracks. When OCR isn't an option either, transcribe the title's own
source-language audio — it emits the **same** `work/<key>.src.srt` artifact, so every
later stage is unchanged.

> **Exhaust extraction first — ASR is the last resort, not the convenient one.** A
> distributor's own subtitles beat any transcript. Before falling back, check, in order:
> 1. every subtitle stream in **every** copy of the title you hold (a REMUX usually
>    carries the disc's English PGS even when a smaller re-encode dropped it);
> 2. the *actual language of the text*, not the `language` tag — mislabelled tracks are
>    common, so extract the track and look at it;
> 3. burned-in (hardcoded) captions in the image — sample frames at timestamps where you
>    know dialogue occurs, not at arbitrary points;
> 4. an incomplete download is **not** a dead end, but check the bytes you actually have:
>    a sparse file's *length* is pre-allocation, so it can read as many GB while holding
>    almost nothing (`fsutil sparse queryrange` on Windows). Re-check it later rather than
>    writing it off once.

```bash
python scripts/transcribe.py the-film-2010              # -> work/the-film-2010.src.srt
python scripts/srt_polish.py work/the-film-2010.src.srt # structural + timing cleanup
python scripts/srt_qa.py work/the-film-2010.src.srt     # structural + readability report
python scripts/parse_source.py the-film-2010            # ...then the normal flow
```
On a 5.1/7.1 mix it isolates the **front-center channel** (where the dialogue lives)
and evens out the level before recognition — on one action-film sample that lifted the
word yield ~58% over a naive downmix — and falls back to the full downmix if a release
turns out to carry a silent center. Cues are re-segmented from *word* timestamps under
your `readability` budget, so the draft already obeys the CPS/CPL/line rules the
validator later enforces.

> ⚠️ ASR timestamps are estimates, not a distributor's — they are the one part of the
> pipeline that is **not** sacred. Review them (and the transcript) before delivery.

### Filling the gaps a reference track exposes
Whisper decodes long audio in 30-second windows, and a window dominated by score,
screaming or overlapping speech can lose a segment the *same model* recovers when handed
just that stretch. Any professionally cued track for the same title — even one in another
language — is an excellent map of **where** those losses are:

```bash
python scripts/transcribe.py the-film-2010 --only-fill --fill-gaps /path/to/official.srt
```
This re-scans only the stretches the reference cues and the draft has nothing near, then
merges in whatever it finds. On one title that moved content gaps from **10.7% → 1.7%** of
the reference's cues.

The reference supplies **timing only** — where dialogue occurs. None of its text is read,
compared or copied, and every recovered word is transcribed from the title's own audio, so
the draft stays an original transcript rather than a derivative of someone else's subtitle.

> Comparing coverage against a reference is also the fastest way to catch a *systemic* ASR
> problem: it is what exposed that a VAD pre-pass was silently discarding shouted dialogue.

`srt_polish.py` fixes only structural and timing defects — it drops empty and
punctuation-only cues, removes ASR boilerplate hallucinations, collapses genuine stutter
loops (never repeats separated in time), re-wraps to the line budget, enforces minimum
duration, and removes overlaps. It **never edits wording**: cues that need condensing are
reported, not truncated.

### ASR boilerplate hallucinations
Starved of speech — under score, screaming or near-silence — a recognizer falls back on
phrasing that saturated its training data and emits it as a lone, well-formed cue:
*"Thanks for watching!"*, *"Please subscribe"*, *"Subtitles by …"*. On one 88-minute film
that was **27 of 964 cues (2.8%)**, while the two professional subtitle tracks for the
same title contained the phrase **zero** times in 1908 cues.

Nothing else in the pipeline sees them. The stutter-loop guards are gated on *adjacency*
and these are scattered minutes apart; the reference-coverage check misses them too,
because they sit *inside* dialogue scenes where the reference does have nearby cues.

`scripts/asr_artifacts.py` is the single policy both the transcriber and the clean-up pass
read. Because it deletes, the rule is deliberately narrow — a cue is dropped only when
**its whole text** is boilerplate (a cue *containing* the phrase is real dialogue wrapped
around it) **and** that exact text **recurs** in the same file. So a character who
genuinely says it once keeps the line. Detection is reported at any count, deletion needs
the repeat threshold, every deleted cue is printed, and removal happens after all timing
decisions so a surviving cue's timing never depends on what was deleted beside it. Tune
under `asr:` in the project config (`strip_hallucinations`, `hallucination_min_repeats`,
`hallucination_extra_patterns`).

> The patterns are narrow on purpose, and kept that way by an adversarial regression set:
> every line of ordinary dialogue that a code review managed to get deleted is now a test
> case. Two traps worth knowing if you add a pattern — a trailing `.{0,N}` reads as "this
> word followed by anything", which is just a sentence; and a `[A-Z]` anchor inside a
> pattern compiled with `re.I` matches lowercase too, so wrap it in `(?-i:…)`.

`srt_qa.py` complements `validate_srt.py`: the validator judges a *delivered target*
against its source and the target-language guardrails, while `srt_qa.py` judges **any**
SRT on its own terms — useful for an ASR draft or any incoming subtitle file.

### Incoming sidecars are often not UTF-8
A downloaded Spanish, French or Portuguese `.srt` is very frequently **cp1252/Latin-1**.
Every accented character then displays as mojibake in a player that assumes UTF-8, which
looks like a broken subtitle even though the cues themselves are perfectly fine. Reading
such a file with `encoding="utf-8"` raises before the first cue; reading it with
`errors="replace"` is worse, because it silently turns every accent into `U+FFFD` and the
damage then looks like the source's fault.

`srt_utils.read_text()` decodes UTF-8 → cp1252 → Latin-1 and **reports which it used**, so
`srt_qa.py` can flag a non-UTF-8 file as **HARD** (it corrupts what the viewer sees) and
`srt_polish.py` can fix it:

```bash
python scripts/srt_qa.py     downloaded.es.srt          # HARD: file is cp1252, not UTF-8
python scripts/srt_polish.py downloaded.es.srt --reencode-only
```

`--reencode-only` rewrites the bytes as UTF-8 without a BOM and changes **nothing else** —
every cue's text, line breaks and timings are preserved, and the pass verifies that before
it leaves the file in place. Use it rather than a full polish when a sidecar's only fault
is its encoding: a distributor's line breaks are usually deliberate, and its timings are
not yours to nudge.

### A sidecar that is uniformly early or late
```bash
python scripts/srt_polish.py downloaded.es.srt --shift -500   # 500 ms earlier
```
`--shift` moves every cue by a fixed number of milliseconds and changes nothing else. It
rewrites **only the timestamp lines** of the decoded source, so cue text, line breaks,
numbering and any trailing position coordinates come through untouched; before writing it
checks that every cue moved by exactly that amount and that **no duration changed**. A cue
that would land before `00:00:00` is clamped there *keeping its duration*.

> Measure before you shift. Match cues to a reference track that is in sync with the video
> — an embedded track is synced by construction — and take the **median** offset over
> confident pairs; two tracks that split lines differently produce a lot of nearest-cue
> noise around it. On one real sidecar that was median −388 ms (IQR −517…−210), and
> `--shift -500` moved it to +56 ms: centred, and a touch early, which is the right side
> of the audio to be on.

## What you get / guarantees
- **Timestamps are sacred** — reattached byte-for-byte; the builder re-parses its own
  output to prove it.
- **No silent cue loss** — one robust parser, a fail-loud builder, a source cross-check
  via an immutable delivery **manifest**, and an off-by-one **alignment check**.
- **Canonical drop policy** — a source cue may be absent only if it is a pure sound cue
  or an explicitly reviewed `dropok` entry.
- **Config-driven es-419 quality** — banned Spain-isms / hyper-local slang, a neutral
  profanity map, addressee-based gender agreement, readability limits, formatting-tag
  preservation — all in `config/guardrails/es-419.yaml` + your glossary.
- **Resumable & recoverable** — SQLite state + `reconstruct.py` (rebuilds a clobbered
  batch from the delivered SRT + manifest) + `apply_patch.py` (re-apply reviewer fixes).

## Tests
```bash
python tests/test_pipeline.py        # 68 self-tests of every integrity guarantee
```

## Repo layout
```
config/      project.example.yaml + guardrails/es-419.yaml (reusable language pack)
prompts/     worker.md, reviewer.md, gate_plan_review.md  (LLM templates)
templates/   glossary.template.md
scripts/     config, srt_utils, asr_artifacts, extract, transcribe, srt_qa, srt_polish,
             parse_source, slice_ranges, normalize_batch, autofix, build_srt,
             validate_srt, align_check, reconstruct, apply_patch, finalize_title,
             shot, state_db
tests/       test_pipeline.py
docs/        DESIGN.md, ORCHESTRATION.md, LEARNINGS.md
```

Adding another target language later = add `config/guardrails/<code>.yaml` and point
`target.guardrails` at it; the scripts are target-agnostic.

## License
MIT — see [LICENSE](LICENSE).
