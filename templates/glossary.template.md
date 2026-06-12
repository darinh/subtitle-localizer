# Glossary / Character Bible — TEMPLATE

Copy to your project's `glossary.md` (path set by `glossary:` in `project.yaml`) and
fill it in. This is the single authority every worker and reviewer reads. Everything in
`<…>` is a placeholder. Delete the guidance lines once filled.

---

## 1. Synopsis, arcs, twists  (comprehend the WHOLE work first)
<2–6 sentences: premise, the twist(s), each main character's arc. For a long series,
keep a core summary + per-season notes. This prevents wrong-addressee / ruined-reveal
translations — the #1 failure mode.>

## 2. Characters (gender drives agreement — resolve from SOURCE pronouns, not the name)
| Name | Aliases / nicknames | Gender | Pronouns in source | Notes (role, relationships) |
|------|---------------------|--------|--------------------|-----------------------------|
| <Name> | <"Nick", AKA> | M / F / NB | he/his · she/her · they | <e.g. unisex name — VERIFIED male via "his lamb" cue 412> |

> For EVERY unisex/ambiguous name (Alex, Jordan, Ariel, Charlie…) cite the source
> evidence that fixed the gender. A glossary gender is a hypothesis; an explicit source
> pronoun/relationship is fact. Record genders for ALL recurring characters (speakers AND
> the people they address) so that both 1st-person ("I'm tired" → speaker) and 2nd-person
> ("are you ready?" → addressee) adjectives resolve correctly.

## 3. Register matrix (tú / usted — directed and time-varying)
| Speaker → Addressee | Register | Switch cue (if it changes) |
|---------------------|----------|----------------------------|
| <Captor → Victim>   | tú (condescending) | flips to usted at cue <N> |
| <Stranger ↔ Stranger> | usted | — |
| <Family / close peers> | tú | — |

Default second person for unlisted pairs: <tú | usted> (also set in project.yaml).

## 4. Locked lexicon (domain terms → fixed target rendering)
Use these EXACT renderings everywhere for consistency. (Add high-confidence ones to
`catchphrases:` in project.yaml so autofix enforces them.)
| Source term | Locked target | Note |
|-------------|---------------|------|
| <term> | <rendering> | <why / disambiguation> |

## 5. Named entities — preserve verbatim
<Cities, brands, flight/plate codes, product names. Drug/chemical names adapted only by
standard target-language spelling. List anything that must NOT be translated.>

## 6. Foreign (third-language) in-film speech
<Which languages appear, and the policy per the guardrails: translate + italics, or keep
in-language + italics. Cite cue ranges if known.>

## 7. Profanity / register notes specific to this title
<Any title-specific intensity choices or catchphrases. The neutral target profanity map
lives in the guardrails pack; note only deviations or recurring lines here.>

## 8. Units & on-screen text
<Unit policy (road distance vs felt quantities). Any recurring on-screen cards / signs /
song refrains that should be preserved or handled a certain way.>

## 9. Visual-grounding findings (filled during work)
<When the picture resolves an ambiguous gender/addressee/action — e.g. via
`python scripts/shot.py <KEY> <CUE>` — record the finding here so text-only reviewers
inherit it. "Cue 612: the person strapping on the vest is the captor (male) → tú".>
