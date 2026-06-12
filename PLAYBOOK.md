# PLAYBOOK — Subtitle Localization to es-419

The master methodology. Hand this (with the repo) to an agent, or follow it yourself.
It encodes hard-won practice from a large real run. Treat every numbered step as a
checkpoint; do not skip the comprehension, glossary, or review gates — that is where
quality is won or lost. **Translate the story, not the strings.**

---

## Pipeline at a glance
```
0. Probe & extract   identify the right TEXT sub stream (not bitmap/forced/commentary), dump SRT
1. Parse source      -> <key>.src.json [{num,ts,text,speaker,sound,bleep,inaudible}]
2. Comprehend        read the WHOLE work; fill glossary.md (plot, arcs, genders, register)
3. Visual grounding  screenshot ambiguous gender/addressee/action; write findings into the glossary
── GATE A: review the PLAN + glossary with ≥3 different LLMs → consensus ──
4. Sample + GATE B   translate ~30 hardest cues; adversarial review; LOCK the style (default-on)
5. Slice             split source into N contiguous worker slices (coverage-asserted)
6. Translate         one worker per slice (prompts/worker.md), loading the SAME glossary
7. Harvest           only after ALL workers signal done AND files byte-stable AND last-cue matches
8. Finalize          normalize → autofix → build → validate → align → deliver (atomic)
9. Review + GATE C   one reviewer (different model) per title; full multi-LLM panel on first-of-season
10. Re-finalize      deliver; mark done in state
```

## Step 0 — Probe & extract
Use `scripts/extract.py`. It inventories subtitle streams, ranks candidates (language
match, SDH preference, non-forced, non-commentary, text codec) and:
- **bitmap source (PGS/VOBSUB)** → stops with OCR guidance — OCR first (Subtitle Edit /
  pgsrip) into an external SRT at `work/<key>.src.srt`, then `parse_source.py`.
- **multiple viable candidates** → writes a preflight report and requires a choice
  (`streams.index_override`, `--index N`, or `--yes`). Never localize the wrong track.

## Step 1 — Parse to a structured source of truth
`scripts/parse_source.py` → `work/<key>.src.json`, preserving timestamps EXACTLY and
flagging `sound` / `inaudible` / `bleep` / `speaker`. Verify the count and contiguity.

## Step 2 — Comprehend the whole work FIRST  (most important step)
Read the entire script in a few large chunks. In the glossary record: plot + twists;
each character's arc; who each ambiguous "you"/pronoun refers to; running motifs/jokes.
For a long series, build a core glossary + per-season overlay (cast WITH genders, new
terms) and amortize comprehension across episodes — don't re-read everything each time.

## Step 3 — Visual grounding
For any cue whose **gender, addressee, or on-screen action** is ambiguous, grab the
frame (`scripts/shot.py <key> <cue>`) and look. Then **write the finding into the
glossary** so text-only reviewers inherit it (the "blind reviewer" fix).

## STOP and confirm with the user (these are preferences, not facts)
Target variant (neutral es-419 vs a country flavor); register scheme (default vs a
deliberate asymmetric tú/usted); profanity intensity; foreign in-film speech (translate
vs leave intentionally-unsubtitled); units; deliverable (sidecar vs muxed; default/forced).

## GATE A — review the PLAN + glossary
Before translating a line, have **≥3 different LLMs + a rubber-duck** critique the plan
and glossary (`prompts/gate_plan_review.md`). Resolve every CRITICAL/MAJOR; proceed only
on consensus. Track findings in an issue ledger (stable ids; states open / fixed /
rejected-with-evidence / user-deferred); cap ~2 rounds, then escalate unresolved items
to the user — never loop forever.

## Step 4 + GATE B — sample, then lock the style
Translate ~30 cues hand-picked to cover EVERY hard category (each register pair,
gendered insults, profanity, italics/foreign speech, named entities, two-speaker cues,
high-CPS condensation). Adversarially review the sample; iterate to consensus. The
approved sample becomes the **style reference** — reuse its exact renderings. (Default-on
for new projects; waive only with an explicit, recorded reason.)

## Step 5 — Slice
`scripts/slice_ranges.py <key>` → `work/_ranges/<key>.rK.src.json`, asserting the slices
cover the full ordered cue list exactly once. Each slice's first/last cue number is the
worker's contract.

## Step 6 — Translate (scene-aware workers)
Dispatch one worker per slice with `prompts/worker.md` (placeholders filled). Workers
load the SAME glossary + guardrails. Output `work/<key>.tgtNN.txt` as `NUM|||text`,
`<DROP>` only on sound cues, 1:1 alignment, addressee-based gender, the profanity map,
diacritics + `¡¿` intact. Pick worker models on **reliability**, not just nominal quality.

## Step 7 — Harvest (timing matters)
Harvest a title only after **all** its workers' completion signals arrive AND the slice
files are byte-stable AND each slice's last cue number matches its contract. A worker
that looks "done on disk" can still re-write its slice at the very end — harvesting in
that window clobbers the consolidated file.

## Step 8 — Finalize (the gate chain)
`scripts/finalize_title.py <key>` runs: `normalize_batch` (self-heal + overlap guard) →
`autofix` (config-driven safe fixes) → `build_srt` (assemble + write the delivery
manifest; fail-loud) → `validate_srt` (HARD must be 0) → `align_check` (no drift) →
**atomic** delivery beside the video. Any failure stops BEFORE delivery and records why.
Common HARD fixes: a banned term → fix in the source-numbered batch; a preserved
proper-noun/venue tripping leftover-language → add a per-cue `work/<key>.engok.txt`
(`OUTPUTCUE|||phrase`); a sound cue the flagger missed → add its source number to
`work/<key>.dropok.txt` with a reason.

## Step 9 + GATE C — adversarial review of the full title
One reviewer per title on a **different model** (`prompts/reviewer.md`); it runs the
integrity self-heal check, hunts the recurring MT defects, edits the source-numbered
batch in place, and emits a complete findings table (keep it until the title is final —
it is the recovery record). Run a **full multi-LLM panel** on the first title of each
season plus a stratified ≥5% cue sample elsewhere. Consensus = no unresolved
CRITICAL/MAJOR; reject a finding only with cited evidence (source cue / SDH / glossary).

## Step 10 — Re-finalize & deliver
Re-run `finalize_title.py` (it RE-VALIDATES, catching any corrupting reviewer edit).
The sidecar is delivered next to the video; state marks the title `done`.

---

## Language guardrails (es-419) — summary (full data in config/guardrails/es-419.yaml)
- **Register is directional, asymmetric, time-varying.** tú/usted per directed pair with
  explicit switch cues.
- **Gendered agreement matches the grammatical referent in gender AND number:** 1st
  person ("I'm tired") = the speaker; 2nd person ("are you ready?") = the addressee; 3rd
  = the referent; an insult aimed at someone agrees with that addressee. Re-derive per cue.
- **Neutral pan-regional profanity**; never both Spain-isms and hyper-Mexican slang.
  Match source intensity; never drop an intensifier; positive "fucking X" → "maldito X /
  del carajo" (NEVER "de mierda"). "fuck me" → "me lleva el carajo" (NEVER "puta madre").
- **Banned:** vosotros/-áis/-éis, vale(=OK), coger(=take/grab), joder, gilipollas,
  tío(=dude), ordenador, móvil, hostia, güey, órale, no mames, pinche, pendejo, cabrón,
  and `-mente` calques for "fucking X".
- **Preserve named entities verbatim**; honorifics by context; keep deliberate
  malapropisms. **Italics** only for genuine foreign in-film speech.
- **UTF-8, NO BOM**; ≤2 lines; CPS target ≤17 (flag >26, compare to source — most
  overflow is inherited from short source durations, not translation bloat).

## Scaling to a whole series
One hierarchical glossary (core + per-season overlay) built once; a resumable state DB
(`scripts/state_db.py`); parallel workers loading the SAME glossary; a per-season term
pre-flight; translation memory for recaps. Gates adapted for scale: 100% automated
validation on every title; full adversarial panel on the first title of each season + a
stratified sample elsewhere; re-lint completed titles after any glossary change.

See **docs/ORCHESTRATION.md** for waves, concurrency caps, model routing, and recovery,
and **docs/LEARNINGS.md** for the reasoning behind every rule.
