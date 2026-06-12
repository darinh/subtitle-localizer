# LEARNINGS — the hard-won "why"

Distilled from a full large-scale run. These are the reasons behind the rules; read them
before changing anything. None reference a specific title — they generalize.

## Translation quality
1. **Comprehend before translating.** Whole-work context prevents wrong addressee /
   gender / terminology — the failure users hate most.
2. **The picture resolves what text cannot.** Screenshot ambiguous gender / addressee /
   action, then write the finding into the glossary so text-only reviewers inherit it.
3. **Register is directional, asymmetric, and time-varying.** Track tú/usted per directed
   pair with explicit switch cues; verify both sides of each switch.
4. **Agreement follows the grammatical referent, in gender AND number.** 1st person =
   speaker, 2nd person = addressee, 3rd = referent; a directed insult/adjective agrees
   with the ADDRESSEE (re-derive from context — don't default to the speaker).
5. **A unisex/feminine-sounding name can be male (and vice-versa).** Derive gender from
   SOURCE pronouns/relationships, not the name or a web list. When two reviewers reach
   OPPOSITE conclusions about a name, that contradiction is the alarm that your glossary
   is wrong — settle it once against the source, fix the glossary, sweep delivered titles.
6. **Workers systematically soften/drop profanity.** They drop intensifiers ("the fuck"),
   render the locked word with a softer synonym, and INVERT positive slang
   ("fucking great" → a pejorative). Put the exact mappings in the worker prompt AND make
   the reviewer hunt them by name — structural checks can't see any of it.
7. **Specific recurring false-friends/calques** to encode: exclamatory "fuck me" →
   "me lleva el carajo" (NEVER the banned oath); culinary "entree" = main course (NEVER
   "entrada" = appetizer); positive "fucking X" → "maldito X"/"del carajo" (NEVER
   "de mierda"); "fucking X" must not become a `-mente` adverb calque; mild exclamations
   ("holy smokes") must not gain profanity.
8. **High CPS is usually the source's fault.** Compare to source CPS; only condense cues
   the target actually inflated. Inject a per-cue character budget so workers condense up
   front; keep CPS a soft flag. (Optional adjacent-same-speaker merge exists for the rare
   case where short rapid cues genuinely cannot fit — off by default to keep timing safe.)

## Data integrity (deterministic plumbing)
9. **Timestamps are sacred.** Never re-time; reattach byte-for-byte. The builder re-parses
   its own output to prove it.
10. **UTF-8 with NO BOM.** A BOM corrupts the first cue in many players — and a tool
    round-trip (an editor re-saving a work file) can RE-introduce one, so self-heal it in
    the normalizer, don't just read with utf-8-sig.
11. **Centralize SRT parsing; make it fail loud.** A naive `split("\n\n")` silently drops
    any cue containing an internal blank line — split only at a blank line FOLLOWED BY an
    index+timestamp header, and cross-check header-count == parsed-count. One parser
    everywhere.
12. **Account for every source cue.** Map each to a translation OR an explicit `<DROP>`
    (allowed ONLY on pure sound cues or a reviewed `dropok` entry, so dialogue can't
    vanish). One canonical drop policy shared by builder, validator, reconstruct, tests.
13. **The validator must cross-check the SOURCE, not just structure** — output timestamps
    an ordered subsequence of the source (or, better, verified against an immutable
    delivery manifest); only droppable cues may be missing; a missing source file is a
    HARD failure.
14. **Structural validity ≠ semantic alignment.** A worker can output every cue once with
    valid timestamps while the text is shifted/swapped. Add a length-correlation
    off-by-one detector AND per-cue anchor checks (numbers, named entities, tag counts) —
    but the multi-LLM review is the real semantic backstop; it must sample flagged windows.
15. **Reconstruct must be closed under duplicate timestamps.** Rebuild a clobbered batch
    from an immutable manifest (out-cue → source nums); use a timestamp two-pointer walk
    only as a degraded fallback that FAILS LOUD on duplicate/ambiguous timestamps rather
    than misassociating text.
16. **Guard against stale/overlapping partials.** The same cue number appearing in two
    different batch files is a HARD overlap error; quarantine stray slices before every
    re-finalize. A consolidator that deletes its inputs must run this guard BEFORE merging.
17. **Auto-wrap at build time; keep line-length a soft flag after.** Workers ignore line
    limits; wrap single-speaker cues into ≤2 balanced lines, preserving "- " two-speaker
    cues.
18. **Self-heal recurring worker output bugs** in the normalizer: a literal `\n`, a
    PowerShell backtick escape, a BOM, and double-encoded mojibake — none legitimately
    appear in dialogue, so the rewrite is safe; add a unit test for each.

## Source / extraction
19. **Mind the subtitle codec.** Image-based subs (PGS/VOBSUB) need OCR; if there is no
    usable text track, stop and ask — never localize the wrong track silently.
20. **Embedded subs are usually synced by construction** for a given rip — extract the
    muxed-in track rather than downloading (downloaded subs almost always need re-sync).
    Still sanity-check duration/cue density.
21. **Define and enforce an SDH policy.** Pure sound cues → `<DROP>`; `[inaudible]` →
    kept marker (never dropped); strip `NAME:` speaker prefixes from display but keep them
    as attribution. Sound detection must handle speaker-prefixed, dashed two-part, and
    UNCLOSED trailing bracket runs — but gate the unclosed-strip on a sound-word
    vocabulary so a typo'd bracket in real dialogue is never wiped.
22. **"Uncensored" video can still have censored SUBTITLES.** Detect `[bleep]`; recover
    the word from audio or use a neutral strong expletive; never translate the literal
    tag, never invent a word the audio doesn't support; the reviewer flags mismatches.
23. **Burned-in (hardcoded) captions leave GAPS in the text track.** For content that
    burns captions into the video, detect timeline gaps, OCR the frames, have a vision
    agent confirm, and insert cues — the text track alone is incomplete.

## Process / orchestration
24. **Adversarially review EVERYTHING — including the scripts.** In a batch pipeline one
    silent bug corrupts hundreds of files; a multi-LLM code review routinely finds
    data-integrity bugs that self-tests then lock in.
25. **Scope every allowlist to a specific cue — never strip globally.** Key preserved-
    English / drop allowlists by OUTPUT cue number; a global substitution can mask real
    untranslated text elsewhere or cannibalize substrings.
26. **Validate review findings against project convention** before applying — a confident
    reviewer can flag a deliberate, already-shipped convention as a defect and regress
    consistency. Reject only with cited evidence.
27. **A review-and-fix agent can abort early or edit-in-place destructively.** Verify each
    review actually completed (sane duration + a findings table + a verdict); capture
    fixes as a structured patch so they're recoverable; the re-finalize gate re-validates
    so a corrupting edit is caught.
28. **Pick worker models on reliability, not just quality**, and keep concurrency bounded:
    overcommitting causes stalls and degraded (diacritic-stripped) output. The reviewer
    must be a different model than the worker.
29. **Make consensus a deterministic state machine** (issue ledger, severity, states,
    round cap → escalate-to-human) so review gates can't loop forever.
30. **Persist everything** (glossary, batches, manifests, scripts, screenshots, state DB)
    so the work is resumable and auditable after a crash or compaction.
