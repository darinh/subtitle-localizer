"""Apply a reviewer's structured patch to the consolidated batch
(work/<key>.tgt01.txt) deterministically — auditable and recoverable (vs ad-hoc
in-place edits that can be lost after a clobber).

Patch file: work/<key>.patch.txt in the batch format — one or more
  NUM|||new full cue text
entries (a 2nd display line goes on its own next line, no prefix). Each NUM must
already exist in the batch. Reports applied cue numbers; re-finalize re-validates.

  python apply_patch.py S01E02
"""
import os
import re
import sys

import config
import srt_utils

CFG = config.load()
WORK = CFG.work
CUE = re.compile(r"^(\d+)(?:\s*\.\.\s*(\d+))?\|\|\|(.*)$")


def _parse(path):
    items, cur = {}, None
    for raw in srt_utils.normalize(open(path, encoding="utf-8").read()).split("\n"):
        m = CUE.match(raw)
        if m:
            cur = m.group(1) + (".." + m.group(2) if m.group(2) else "")
            items[cur] = [m.group(3)]
        elif cur is not None:
            items[cur].append(raw)
    return {k: "\n".join(v).rstrip("\n") for k, v in items.items()}


def apply_patch(key):
    batch = WORK / f"{key}.tgt01.txt"
    patch = WORK / f"{key}.patch.txt"
    if not batch.exists():
        raise SystemExit(f"FAIL: {batch.name} missing (normalize/reconstruct first)")
    if not patch.exists():
        print(f"{key}: no patch file ({patch.name}); nothing to apply")
        return 0
    fixes = _parse(patch)
    # index existing cues by their head key (N or N..M)
    lines = batch.read_text(encoding="utf-8").split("\n")
    blocks, order, cur = {}, [], None
    for raw in lines:
        m = CUE.match(raw)
        if m:
            cur = m.group(1) + (".." + m.group(2) if m.group(2) else "")
            blocks[cur] = [raw]
            order.append(cur)
        elif cur is not None and raw != "":
            blocks[cur].append(raw)
    applied, missing = [], []
    for head, text in fixes.items():
        if head not in blocks:
            missing.append(head)
            continue
        blocks[head] = [f"{head}|||{text}".split("\n")[0]] + text.split("\n")[1:] \
            if "\n" in text else [f"{head}|||{text}"]
        applied.append(head)
    if missing:
        raise SystemExit(f"FAIL: patch references unknown cues: {missing} (re-check numbers)")
    out = []
    for head in order:
        out.extend(blocks[head])
    batch.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print(f"{key}: applied {len(applied)} patch fixes: {applied}")
    return len(applied)


if __name__ == "__main__":
    for k in sys.argv[1:]:
        apply_patch(k)
