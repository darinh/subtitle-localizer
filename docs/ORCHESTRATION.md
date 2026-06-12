# ORCHESTRATION — running waves at scale

How to drive many titles (or one film) through the pipeline reliably with parallel LLM
workers and reviewers. The deterministic guarantees live in the scripts; this is the
human/orchestrator layer.

## Waves & concurrency
- Dispatch **one wave of ~3 titles × 4 slices ≈ 12 concurrent translation workers**.
- **Do NOT exceed ~12 concurrent workers.** Overcommitting (e.g. two full waves at once)
  reliably causes workers to **stall/freeze** and to emit **degraded output** (a model
  can write a whole range with stripped diacritics). Harvest a wave before starting the
  next. Reviewers are cheap and don't count toward the worker budget.
- Keep the per-title state in `pipeline.db` (`scripts/state_db.py`) so a crash mid-wave
  resumes correctly.

## Harvest timing (avoid the late-write clobber)
Harvest a title only when ALL three hold:
1. every worker for the title has signalled completion;
2. the slice files are **byte-stable** (unchanged for ~60–90s);
3. each slice's **last cue number == its contract**.
A worker that looks "done on disk" can re-write its slice at the very end; harvesting in
that window clobbers the consolidated file.

## Model routing
- Choose worker models on **reliability** (completes, doesn't refuse, keeps 1:1
  alignment), not just nominal quality. The glossary keeps style consistent across models.
- The **reviewer must be a different model** than the worker(s) for a title.
- If a model refuses a legitimate owned-media localization, retry / switch models
  (refusals are stochastic when the same model also completes the task) — the worker
  prompt carries the personal-use/accessibility authorization.

## Stall / zombie detection & recovery
- **Stall:** a worker's tool-call count is frozen for many minutes and its output file
  never appears. Re-dispatch THAT slice on a reliable model (`<key>.tgtNN.txt`); first
  reduce concurrency, or the replacement stalls too. Some stalls self-recover — give a
  slice a little time before re-dispatching, and prefer fewer concurrent workers.
- **Zombie clobber:** an orphaned/late worker re-writes its slice AFTER harvest/review,
  collapsing the consolidated batch. Defenses at EVERY re-finalize:
  - quarantine stray `*.tgtNN.txt` slices before re-finalizing;
  - assert the consolidated batch still has the expected last cue / cue count;
  - if clobbered: `python scripts/reconstruct.py <key>` (rebuilds from the delivered SRT
    + manifest) then re-apply the reviewer's fixes with `apply_patch.py` (keep the
    reviewer's findings table as the recovery record).
- A delivered sidecar is **immutable once re-finalized** (atomic replace), and the
  finalize coverage-gate refuses a fragment — so a late clobber of the *work* file cannot
  corrupt a delivered title.

## Counting reliably
Cross-check cue counts with more than one method (a Python `re.match` count and a max-cue
check); a single quick count taken mid-write can mislead while a worker is flushing. The
finalize coverage-gate is the authoritative guard.

## Consensus as a state machine (don't loop forever)
Track every review finding in an issue ledger with a stable id, severity, and state:
`open → fixed | rejected-with-evidence | user-deferred`. Proceed only when no
CRITICAL/MAJOR is `open`. To reject a finding, cite concrete evidence (source cue / SDH /
screenshot / glossary). Hard-cap each gate at ~2 rounds; after the cap, any unresolved
CRITICAL/MAJOR becomes `NEEDS-HUMAN` (escalate to the user) — not another review loop.

## Validate review findings against project convention
A confident reviewer (even two agreeing) will sometimes flag a deliberate, already-
shipped convention as a defect. Before applying a "fix", check it against the glossary /
guardrails and prior delivered output; applying it blindly REGRESSES consistency.

## Optional: burned-in / hardcoded caption recovery
Some releases burn captions into the *video* during hard-to-hear moments, leaving a GAP
in the embedded subtitle track. If `features.burnin_recovery` is on: find gaps in the
source timeline (> ~2.5s with no cue), sample frames there, OCR the bottom-center caption
box, have a **vision agent confirm** (reject name/location lower-thirds), translate, and
insert new cues clamped so they never overlap — then renumber. (This module is a
documented extension; wire it before slicing so the inserted cues flow through normally.)

## Optional: mux instead of a sidecar
The default deliverable is a sidecar next to the video. To mux, map streams explicitly
(don't assume the new track index) and verify with `ffprobe`; ask the user whether the
target track should be default and/or forced.
```
ffmpeg -y -i in.mkv -i out.es-419.srt -map 0:v -map 0:a -map 0:s -map 1:0 -c copy \
  -metadata:s:s:1 language=spa -metadata:s:s:1 "title=Español (Latinoamérica)" out.es419.mkv
```
