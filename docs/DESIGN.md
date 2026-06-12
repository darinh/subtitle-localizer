# subtitle-localizer — Design & Plan

A reusable, production-grade pipeline that turns a film's or series' **embedded
source-language subtitles** into high-quality **Latin American Spanish (es-419)**
SRT sidecars — localized like a professional would, not machine-translated line by
line. It is content-agnostic: drop in any movie or show, supply a small per-project
config + character bible, and run.

> Defining principle: **translate the story, not the strings.** Comprehend the whole
> work, ground ambiguous lines in the picture, lock conventions in a glossary, then
> translate, validate, and pass through adversarial multi-LLM review to consensus.

This document is the **design that gets reviewed at Gate A** before any code is
written. It captures architecture, the config-driven generalization, the orchestration
model, the review gates, and testing.

---

## 1. Goals & non-goals

**Goals**
- One pipeline that localizes **any** source title to es-419 with consistent quality.
- **Zero hardcoded title-specific data** in code. Everything title-specific lives in a
  per-project config + glossary the user (or an agent) fills in.
- Resumable, auditable, parallelizable across hundreds of episodes or a single film.
- Fail-loud data integrity: never silently drop or misalign a cue.
- The hard-won correctness of a real large run, distilled into reusable scripts + a
  master playbook + template prompts.

**Non-goals**
- Not an OCR/translation model itself — it orchestrates LLM translation workers and
  reviewer agents, and does the deterministic plumbing/validation around them.
- Not locked to es-419 in architecture: the target is a config value (suffix + a
  guardrails pack), so other targets can be added later — but es-419 ships complete and
  is the supported target.
- Not a GUI. CLI + config + prompts.

---

## 2. Repository layout

```
subtitle-localizer/
├── README.md                     # what it is + 10-minute quickstart
├── PLAYBOOK.md                   # the master methodology (the reusable "prompt/system")
├── LICENSE                       # MIT
├── .gitignore                    # ignore work/, *.srt outputs, venv, caches
├── config/
│   ├── project.example.yaml      # per-project config TEMPLATE (copy -> project.yaml)
│   └── guardrails/
│       └── es-419.yaml           # reusable es-419 language pack (banned terms, profanity map, lexicon hints)
├── prompts/
│   ├── worker.md                 # translation-worker template ({{placeholders}})
│   ├── reviewer.md               # adversarial-reviewer template (+ integrity self-heal block)
│   └── gate_plan_review.md       # Gate-A plan/glossary review prompt
├── templates/
│   └── glossary.template.md      # the character/﻿register/lexicon bible structure
├── scripts/
│   ├── config.py                 # loads project.yaml + guardrails; single source of settings
│   ├── srt_utils.py              # centralized robust SRT parse/write/wrap (generic)
│   ├── extract.py                # ffprobe stream pick + extract embedded subs -> source JSON
│   ├── parse_source.py           # SRT -> <title>.src.json [{num,ts,text,sound,bleep,inaudible,speaker}]
│   ├── slice_ranges.py           # split source JSON into N contiguous worker slices + coverage assert
│   ├── normalize_batch.py        # consolidate worker batch files; self-heal; overlap guard
│   ├── autofix.py                # config-driven deterministic locked-lexicon fixes
│   ├── build_srt.py              # assemble target SRT; <DROP>/dropok; reattach exact ts; re-parse proof
│   ├── validate_srt.py           # config-driven HARD/soft checks + source cross-check
│   ├── align_check.py            # length-correlation off-by-one drift detector (generic)
│   ├── reconstruct.py            # rebuild consolidated batch from delivered SRT + source JSON (recovery)
│   ├── finalize_title.py         # orchestration gate-chain: normalize->autofix->build->validate->align->deliver
│   └── state_db.py               # generic SQLite state (titles table) + helpers
├── tests/
│   └── test_pipeline.py          # self-tests for every integrity guarantee
└── docs/
    ├── DESIGN.md                 # this file
    ├── ORCHESTRATION.md          # waves, concurrency caps, harvest timing, model routing, recovery
    └── LEARNINGS.md              # the generalized hard-won learnings (the "why")
```

`work/` (created at runtime, git-ignored) holds per-title intermediates:
`<title>.src.json`, `_ranges/<title>.rK.src.json`, worker outputs `<title>.tgtNN.txt`,
the consolidated `<title>.tgt01.txt`, the built `<title><srt_suffix>`, the delivery
`<title>.manifest.json`, and per-title allowlists `<title>.dropok.txt` /
`<title>.engok.txt` / reviewer `<title>.patch.txt`.

---

## 3. Config-driven architecture (the core generalization)

Everything that was title-specific in the original run becomes data in two files.

### 3.1 `config/project.yaml` (per project; copied from the example)
```yaml
project_name: "My Show (2025)"          # free text, used in prompts only
source_language: en                      # ISO code of the embedded subs
target:
  code: es-419
  srt_suffix: ".es-419.srt"             # delivered sidecar suffix
  guardrails: es-419                      # which pack under config/guardrails/
paths:
  media_root: "D:/Media/My Show"          # where videos live (delivery target)
  # delivery: sidecar written next to each video as <videostem><srt_suffix>
layout:
  # how to find the video for a title key, and how to name title keys
  title_regex: 'S(?P<season>\d{2})E(?P<ep>\d{2})'   # or a movie: single title
  slices_per_title: 4                     # range slices per title for parallel workers
register:
  default_second_person: "tú"            # tú | usted | usted-formal
  notes: "Aggressors may use condescending tú; officials usted."
glossary: "glossary.md"                   # the per-project bible (characters+genders, etc.)
catchphrases:                             # OPTIONAL per-project locked renderings (regex -> fix)
  - { when: '\bdowntown\b', fix: "el centro" }          # example; empty by default
```

### 3.2 `config/guardrails/es-419.yaml` (reusable across all projects)
The es-419 language pack — the part that is the SAME for every show:
```yaml
banned:                                   # Spain-isms + over-local slang (regex, case-insensitive)
  spainisms: [vosotros, vosotras, "jod\\w*", gilipollas, capullo, "co\\u00f1o", hostia, ordenador, "m\\u00f3vil", entrantes]
  mx_hyperlocal: ["g\\u00fcey", "\\u00f3rale", "no\\s+mames", chido, pinche, pendejo, cabr\u00f3n]
  calques: [malditamente, condenadamente]
  oaths: ["puta\\s+madre"]                # banned even though hijo/a de puta is allowed
  context_exempt:                          # homographs that are fine in family/innocent senses
    - { term: "t\\u00edo|t\\u00eda", allow_when: "family|preceded-by-possessive" }
    - { term: vale, allow_when: "valer-collocation" }   # vale la pena, etc.
profanity_map:                             # source -> locked neutral es-419 rendering (reviewer hunts these)
  "fuck me": "\u00a1me lleva el carajo!"  # NEVER "puta madre"
  "fucking hell": "\u00a1maldita sea!"
  "shit": "mierda"
  "bullshit": "estupideces"
  "bitch(insult)": "imb\u00e9cil|perra"
  positive_fucking: "del carajo|maldito X"  # NEVER "de mierda" (semantic inversion)
leftover_source_markers: [the, you, your, what, this, that, they, here, there, because, going, gonna, yeah, please, sorry, fuck, shit, okay, ok]
sdh:
  inaudible_render: "[ininteligible]"     # kept, flagged; never dropped
  drop_pure_sound: true                    # [music]/[applause] -> <DROP>
locked_lexicon_examples: {}                # project supplies domain terms in its glossary
```

The validator and autofix read these — **no language data is hardcoded in Python**.

---

## 4. The pipeline (per title)

```
0. probe & extract   extract.py     -> pick text sub stream (not PGS/forced), dump SRT
1. parse             parse_source.py-> <title>.src.json: [{num,ts,text[],sound,bleep,inaudible,speaker}]
2. comprehend        (human/agent)   -> read whole work; fill glossary.md (plot, arcs, genders)
   GATE A: review the PLAN + glossary with multiple LLMs -> consensus
3. sample (optional) (agent)         -> ~30 hardest cues; GATE B adversarial review; lock style
4. slice             slice_ranges.py -> _ranges/<title>.rK.src.json (+ coverage assertion)
5. translate         N worker agents -> <title>.esK.txt   (1 slice each; NUM|||text; <DROP> sound)
6. harvest           (orchestrator)  -> only after ALL workers' completion notifications + byte-stable
7. finalize          finalize_title.py:
      normalize_batch -> autofix -> build_srt -> validate_srt -> align_check -> deliver
8. review            1 reviewer agent (different LLM) edits the consolidated batch in place
9. re-finalize       finalize_title.py again -> deliver
   GATE C: for first title of a season + a stratified sample elsewhere, full adversarial panel
10. done             sidecar delivered next to the video; state_db marks done
```

Key integrity guarantees (ported verbatim from the proven run, made generic):
- **Centralized parser** (`srt_utils.parse_srt`) splits only at a blank line *followed by*
  an index+timestamp header, so an internal blank line never silently drops a cue; it
  cross-checks header-count == parsed-count and fails loudly.
- **Builder** reattaches **byte-identical** source timestamps, allows `<DROP>` only on
  `sound==true` cues or a reviewed `dropok` allowlist, rejects blank lines inside a cue,
  auto-wraps to ≤2 lines, then **re-parses its own output** and asserts kept-count and
  timestamp subsequence.
- **Validator** cross-checks the output timestamps are an ordered **subsequence** of the
  source (no invented/reordered cues); only sound cues may be missing; flags banned terms,
  leftover source-language, BOM, mojibake, CPS — **all term lists from config**.
- **Alignment check** compares per-cue ES/EN length correlation at offsets −1/0/+1 over a
  sliding window to catch localized off-by-one drift that structural checks can't see.
- **Normalizer** self-heals BOM / literal `\n` / backtick escapes / mojibake, merges
  mis-prefixed continuation lines, and treats the same cue number in two different batch
  files as a HARD overlap error (stale-partial guard).
- **Reconstruct** rebuilds the consolidated batch from an already-delivered SRT + source
  JSON via a timestamp two-pointer walk (recovery when a late/zombie worker clobbers a
  batch file after harvest).

---

## 5. Prompt templates (filled from config + glossary)

- `prompts/worker.md` — one slice per worker. Placeholders: `{{PROJECT}}`, `{{TITLE}}`,
  `{{RANGE}}`, `{{SLICE_PATH}}`, `{{OUT_PATH}}`, `{{GLOSSARY_PATH}}`, `{{GUARDRAILS}}`.
  Encodes: output format (`NUM|||text`), `<DROP>` rule, 1:1 alignment, the profanity map,
  banned list, register matrix, "keep diacritics & ¡¿", self-check.
- `prompts/reviewer.md` — adversarial QA of one title. Includes the **integrity self-heal
  block** (verify consolidated batch cue-count/last-cue == expected; if collapsed,
  `reconstruct.py` then review) and a checklist targeting the known recurring MT defects.
  Verdict: `CONSENSUS-READY` / `NEEDS-HUMAN` + severity-tagged, cue-numbered findings.
- `prompts/gate_plan_review.md` — Gate-A review of the plan + glossary before translation.

Prompts are **model-agnostic** and reference only config/glossary, never a specific show.

---

## 6. Orchestration model (docs/ORCHESTRATION.md)

- **Parallelize** with worker sub-agents (one slice each) loading the SAME glossary; the
  orchestrator validates and gates; integrate serially to avoid shared-file conflicts.
- **Concurrency cap**: keep **≤ ~12 concurrent translation workers** (e.g. one wave of 3
  titles × 4 slices). Overcommitting causes worker **stalls/freezes** and degraded output
  (a model can emit a whole range with stripped diacritics). Harvest a wave before starting
  the next.
- **Model routing**: pick workers by **reliability**, not just nominal quality. If a model
  hangs/refuses, re-dispatch that slice on a reliable model; the glossary keeps style
  consistent across models. Reviewers should be a **different** model from the worker.
- **Harvest timing**: harvest a title only after **all** its workers' completion
  notifications AND the slice files are byte-stable AND the last cue number matches the
  slice contract (defends against a late worker re-writing its slice).
- **Recovery**: detect a stalled worker (tool-call count frozen for minutes) → re-dispatch
  that slice; detect a clobbered consolidated batch (cue-count below expected) → reconstruct
  + re-apply the reviewer's logged fixes. A delivered SRT is immutable once written, so the
  finalize coverage-gate is the real guard — race the finalize and move on.

---

## 7. Review gates (the quality engine)

- **Gate A — plan + glossary** reviewed by **≥3 different LLMs** + a rubber-duck before any
  translation. Resolve every CRITICAL/MAJOR; proceed only on consensus.
- **Gate B — sample** (~30 hardest cues) adversarially reviewed; the approved sample becomes
  the style reference reused verbatim.
- **Gate C — full title** review by a different LLM per title (edit-in-place), plus a full
  multi-LLM panel on the first title of each season and a stratified ≥5% cue sample.
- **Consensus rule**: proceed only when no reviewer holds an *unresolved* CRITICAL/MAJOR.
  "Resolved" = fixed, or rejected **with cited evidence** (source cue / SDH / screenshot /
  glossary). Cap each gate at ~2 rounds; if subjective disagreement persists, summarize
  options and ask the user.
- **Validate findings against project convention** before applying — a confident reviewer
  can flag a deliberate, already-shipped convention as a defect.

---

## 8. Testing (tests/test_pipeline.py)

Self-tests that lock every integrity guarantee (run in CI / pre-commit):
1. Parser: internal blank line preserved (not split); embedded-header detection; header-count
   cross-check; `ts_seconds` hour handling.
2. Builder: `<DROP>` only on sound cues; dropok allowlist honored; blank-line-in-cue rejected;
   timestamps byte-identical after re-parse; missing/extra cue → loud failure.
3. Normalizer sanitizer: BOM / `\n` / backtick / mojibake healed; mis-prefixed continuations
   merged; same cue in two files → overlap error.
4. Slicer: concatenation of slices == full ordered cue list (coverage, no overlap/gap).
5. Align check: a synthetic off-by-one shift is flagged; aligned content passes.
6. Validator (config-driven): a banned term from the pack is caught; an exempt homograph in
   family context passes; leftover source-language caught; engok scope is per-cue only.
7. Reconstruct: delivered SRT + source JSON round-trips to the consolidated batch (sound
   cues → `<DROP>`).

---

## 9. Work plan & gates for THIS deliverable

1. **Gate A** — adversarial review of *this* DESIGN.md by 3+ different LLMs + rubber-duck;
   reconcile to consensus. (No build before consensus.)
2. Build config + scripts + prompts + templates + docs + tests.
3. **Gate C** — code-review agent + general-purpose agents on different LLMs review all
   scripts/prompts/docs; run the test suite; reconcile to consensus; fix everything flagged.
4. Initialize git, commit, create `github.com/darinh/subtitle-localizer`, push, verify.

## 10. Explicit generalization checklist (no leakage of the origin project)
- No show names, season casts, dish/venue names, host catchphrases, or media paths in code,
  prompts, docs, or tests. The only example values appear in `*.example.*` config and the
  glossary **template** (clearly marked as placeholders).
- All language/lexicon data lives in `config/guardrails/es-419.yaml` (reusable) and the
  per-project glossary; Python reads them at runtime.

---

## 11. Gate-A revisions (reconciled from 3-LLM adversarial review — supersede where in conflict)

Reviewed by GPT-5.5, Gemini 3.1 Pro, and a rubber-duck critic; all returned NEEDS-REVISION.
Every finding is tracked in the `gate_findings` ledger. Resolutions folded into the build:

- **[A] Extraction is real-media-aware.** `extract.py` runs `ffprobe`, builds a **stream
  inventory**, ranks candidates (language match, text-codec vs bitmap, SDH preference,
  forced allowed/blocked, default disposition, title metadata), writes a **preflight report**,
  and **requires confirmation when >1 candidate**. Bitmap subs (PGS/VOBSUB) → **fail loud**
  with OCR guidance (no silent wrong-track localization). Stream prefs live in config.
- **[B][D][O][P] Safe artifact/state model (replaces "race the finalize").** Workers write
  uniquely-named outputs; harvest requires all completion signals **and** byte-stability
  **and** last-cue == slice contract. Finalize writes an **immutable delivery manifest**
  (`work/<title>.manifest.json`: out-index → source num, ts, source-hash, target-hash, drop
  reason, schema/config version). `finalize_title` delivers a **validated** sidecar
  atomically (temp → `os.replace`); the recommended flow then runs the review and
  **re-finalizes** to deliver the FINAL — both deliveries are validated by the full gate
  chain, so a delivered sidecar is never a fragment (it is replaced in place by the
  reviewed version). State is a transactional SQLite schema with **per-slice rows**
  (attempt id, status, checksum, size/mtime, model, `superseded_by`); `harvest.py` records
  slice readiness (present + last-cue == contract + byte-stable across runs) so a crash
  mid-wave resumes correctly. Reviewer edits are captured as a **structured patch log**
  (`<title>.patch.txt`, cue → corrected text) applied by `apply_patch.py`; the re-finalize
  gate **re-validates** so a corrupting edit is caught, and the log enables recovery.
- **[C] One canonical drop policy** shared by builder/validator/reconstruct/tests: a source
  cue may be absent from the output **iff** (`source.sound==true` AND
  `guardrails.sdh.drop_pure_sound`) **or** its number is in `<title>.dropok.txt` with a
  reason. The validator asserts the delivered timestamp set == source minus approved-drops.
- **[D] Reconstruct is closed under duplicate timestamps.** It rebuilds from the **manifest**
  first; the timestamp two-pointer walk is a **degraded fallback** that **fails loud** on any
  duplicate/ambiguous timestamp instead of mis-associating text.
- **[E][F][S] Expanded config + glossary schema.** `project.yaml`/glossary carry: characters
  `{name, aliases, gender, pronouns}`; a **dyadic register matrix** (speaker→addressee
  tú/usted, with switch cues); **units** policy; **foreign-speech** policy; **named entities**
  to preserve; and readability limits `max_cps`, `max_cpl`, `min_duration`, `min_gap`.
  Workers resolve the **addressee** per cue (gender AND number). The validator HARD-checks
  **formatting-tag balance** and that the target's `<i>/<b>` tag set matches the source per cue.
- **[G] Movie vs series mode.** `layout.kind: movie | episodic`. Movie mode uses an explicit
  `titles: [{key, video}]` list (no `SxxEyy` assumption); Gate C branches by kind (full-title
  review for movies; first-of-season + stratified sample for series).
- **[H] Structural ≠ semantic — stated explicitly.** Deterministic checks prevent structural
  loss, not all semantic misalignment. Added per-cue **anchor checks** (digits, ALL-CAPS
  named-entity tokens, tag counts) and the reviewer must sample low-correlation alignment
  windows. Workers translate against **immutable cue ids**.
- **[I] CPS without unsafe merging.** 1:1 + byte-identical timestamps stays the **safe
  default**; a per-cue **character budget** `(end−start)·max_cps` is injected into the worker
  prompt for up-front condensation; CPS is a **soft** flag (compare to source CPS). An
  **opt-in** `allow_merge` mode lets a worker merge *adjacent same-speaker* cues
  (`N..M|||text`); the builder spans `start(N)..end(M)` and the manifest records it. Default off.
- **[J] Bleep/censored policy.** Parser keeps a `bleep` flag. Policy (in prompts): never
  invent a bleeped word; if the audio is uncensored, recover it (ASR/neutral expletive) and
  flag; never translate the literal `[bleep]` tag. Reviewer flags audio/text mismatches.
- **[K] Burned-in caption recovery** — optional documented module (`docs/ORCHESTRATION.md`):
  find gaps in the subtitle timeline, OCR sampled frames, vision-agent confirm, insert cues.
- **[L] Visual grounding operationalized** via `shot.py` (frame grab at a cue timestamp);
  findings written back into the glossary so text-only reviewers inherit them.
- **[M] Gate B (sample-lock) default-ON** for new projects (explicit config waiver allowed).
- **[N] Deterministic consensus** via the issue-ledger state machine (stable IDs, severity,
  state ∈ {open, fixed, rejected-with-evidence, user-deferred}); hard round cap → `NEEDS-HUMAN`
  (never an infinite loop); only the orchestrator may downgrade a finding, and only with cited
  evidence.
- **[Q] Tests** add: crash-resume / stale-supersession, duplicate-timestamp reconstruct
  fail-loud, movie-layout config, multi-candidate stream preflight, gate round-cap, and the
  canonical drop policy.
- **[R] Optional mux** helper + `ffprobe` verification (documented; sidecar is the default).
