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
python tests/test_pipeline.py        # 34 self-tests of every integrity guarantee
```

## Repo layout
```
config/      project.example.yaml + guardrails/es-419.yaml (reusable language pack)
prompts/     worker.md, reviewer.md, gate_plan_review.md  (LLM templates)
templates/   glossary.template.md
scripts/     config, srt_utils, extract, parse_source, slice_ranges, normalize_batch,
             autofix, build_srt, validate_srt, align_check, reconstruct, apply_patch,
             finalize_title, shot, state_db
tests/       test_pipeline.py
docs/        DESIGN.md, ORCHESTRATION.md, LEARNINGS.md
```

Adding another target language later = add `config/guardrails/<code>.yaml` and point
`target.guardrails` at it; the scripts are target-agnostic.

## License
MIT — see [LICENSE](LICENSE).
