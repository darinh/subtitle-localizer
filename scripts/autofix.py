"""Deterministic, config-driven autofix applied to the consolidated batch
(work/<key>.tgt01.txt) BEFORE the build, so recurring high-confidence issues never
reach a reviewer. Rules come from:
  * guardrails.<target>.yaml  ->  autofix:        (reusable target-level safe nets)
  * project.yaml              ->  catchphrases:    (project locked lexicon)
Each rule: {type: word|regex, find, replace}. Only safe, high-confidence rewrites;
ambiguous calls are left for the adversarial review. Applied to cue TEXT only
(never the NUM||| prefix or a <DROP> token). Reports what changed.

  python autofix.py S01E02 [S01E03 ...]
"""
import os
import re
import sys

import config

CFG = config.load()
WORK = CFG.work
CUE = re.compile(r"^(\d+(?:\s*\.\.\s*\d+)?)\|\|\|(.*)$")   # head = N or N..M (merge-aware)


def _compile_rules():
    rules = []
    for src in (CFG.guardrails.get("autofix", []) or []), (CFG.catchphrases or []):
        for r in src:
            typ = (r.get("type") or r.get("match_type") or "word").lower()
            find, repl = r.get("find"), r.get("replace")
            if find is None or repl is None:
                continue
            if typ == "word":
                # whole-word, case-insensitive; preserve simple Capitalized form
                pat = re.compile(r"\b" + re.escape(find) + r"\b", re.I | re.U)
                rules.append((f"word:{find}", pat, repl, True))
            else:
                rules.append((f"regex:{find}", re.compile(find, re.U), repl, False))
    return rules


RULES = _compile_rules()


def _apply_case(repl, matched):
    if matched[:1].isupper() and repl[:1].islower():
        return repl[:1].upper() + repl[1:]
    return repl


def fix_text(s, counts):
    for label, pat, repl, caseaware in RULES:
        if caseaware:
            def _sub(m):
                return _apply_case(repl, m.group(0))
            s, n = pat.subn(_sub, s)
        else:
            s, n = pat.subn(repl, s)
        if n:
            counts[label] = counts.get(label, 0) + n
    return s


def autofix(key):
    path = WORK / f"{key}.tgt01.txt"
    if not path.exists():
        print(f"{key}: no consolidated batch (run normalize first)")
        return {}
    counts, out = {}, []
    for line in path.read_text(encoding="utf-8").replace("\r", "").split("\n"):
        m = CUE.match(line)
        if m:
            text = m.group(2)
            if text.strip() != "<DROP>":
                text = fix_text(text, counts)
            out.append(f"{m.group(1)}|||{text}")
        else:
            out.append(fix_text(line, counts))
    path.write_text("\n".join(out), encoding="utf-8", newline="\n")
    print(f"{key}: autofix applied {sum(counts.values())} changes: {counts}" if counts
          else f"{key}: autofix — nothing to change")
    return counts


if __name__ == "__main__":
    for k in sys.argv[1:]:
        autofix(k)
