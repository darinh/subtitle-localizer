"""Single source of settings for the whole pipeline.

Loads a per-project config (config/project.yaml) and merges the reusable target
language guardrails pack (config/guardrails/<target>.yaml). Compiles the banned /
leftover-source regexes once, resolves paths, and discovers titles (episodic glob
or explicit movie list). Every other script imports `load()` — NO language data or
project specifics are hardcoded anywhere else.

Usage:
    import config
    cfg = config.load()          # reads config/project.yaml (or $SUBLOC_CONFIG)
    cfg.work / "X.src.json"      # Path to the work dir
"""
import os
import re
import functools
from pathlib import Path

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Install with:  pip install pyyaml\n"
        f"(import error: {e})"
    )

ROOT = Path(__file__).resolve().parent.parent          # repo root (parent of scripts/)
DEFAULT_PROJECT = ROOT / "config" / "project.yaml"
EXAMPLE_PROJECT = ROOT / "config" / "project.example.yaml"


def _read_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base, over):
    """Return base updated by over (over wins); dicts merged recursively."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


class Config:
    def __init__(self, project, project_path):
        self.path = Path(project_path)
        self.project = project
        self.source_language = project.get("source_language", "en")
        tgt = project.get("target", {}) or {}
        gname = tgt.get("guardrails", "es-419")
        gpath = ROOT / "config" / "guardrails" / f"{gname}.yaml"
        if not gpath.exists():
            raise SystemExit(f"guardrails pack not found: {gpath}")
        self.guardrails = _read_yaml(gpath)
        self.target_code = tgt.get("code", self.guardrails.get("target_code", gname))
        self.target_name = tgt.get("name", self.guardrails.get("target_name", gname))
        self.srt_suffix = tgt.get("srt_suffix", f".{self.target_code}.srt")

        paths = project.get("paths", {}) or {}
        self.media_root = Path(paths.get("media_root", ".")).expanduser()
        wd = paths.get("work_dir", "work")
        self.work = (Path(wd) if os.path.isabs(wd) else ROOT / wd)
        self.ranges = self.work / "_ranges"

        self.layout = project.get("layout", {}) or {}
        self.slices_per_title = int(project.get("slices_per_title", 4))
        self.streams = project.get("streams", {}) or {}
        self.register = project.get("register", {}) or {}
        self.units = project.get("units", {}) or {}
        self.named_entities = project.get("named_entities_keep_verbatim", []) or []
        self.features = project.get("features", {}) or {}
        self.catchphrases = project.get("catchphrases", []) or []
        self.asr = project.get("asr", {}) or {}
        self.glossary = project.get("glossary", "glossary.md")

        # readability: guardrails defaults overridden by project
        self.readability = _deep_merge(
            self.guardrails.get("readability", {}) or {},
            project.get("readability", {}) or {})

        self.sdh = self.guardrails.get("sdh", {}) or {}
        self.sound_words = set(w.lower() for w in self.sdh.get("sound_words", []))
        self.profanity_map = self.guardrails.get("profanity_map", []) or []
        self.foreign_speech = _deep_merge(
            self.guardrails.get("foreign_speech", {}) or {},
            project.get("foreign_speech", {}) or {})
        self.encoding = self.guardrails.get("encoding", {}) or {}
        self.kept_loanwords = [w.lower() for w in self.guardrails.get("kept_loanwords", [])]

        self._compile()

    # ---- compiled matchers -------------------------------------------------
    def _compile(self):
        b = self.guardrails.get("banned", {}) or {}
        terms = list(b.get("terms", []))
        phrases = list(b.get("phrases", []))
        # always-banned: \b(?:term1|term2|...)\b  +  phrase alternatives
        parts = []
        if terms:
            parts.append(r"\b(?:" + "|".join(terms) + r")\b")
        parts += [r"(?:" + p + r")" for p in phrases]
        self.banned_regex = re.compile("|".join(parts), re.I | re.U) if parts else None
        self.context_exempt = b.get("context_exempt", []) or []
        caution = list(b.get("caution", []) or [])
        self.caution_regex = (
            re.compile(r"\b(?:" + "|".join(caution) + r")\b", re.I | re.U) if caution else None)

        markers = self.guardrails.get("leftover_source_markers", []) or []
        self.leftover_regex = (
            re.compile(r"\b(?:" + "|".join(re.escape(m) for m in markers) + r")\b", re.I)
            if markers else None)

    # ---- title discovery ---------------------------------------------------
    @functools.lru_cache(maxsize=1)
    def titles(self):
        """Return ordered list of (key, video_path:Path). Works for movie or series."""
        kind = (self.layout.get("kind") or "episodic").lower()
        out = []
        if kind == "movie":
            for t in self.layout.get("titles", []) or []:
                out.append((t["key"], self.media_root / t["video"]))
            if not out:
                raise SystemExit("layout.kind=movie but no 'titles' list provided")
            return out
        # episodic: glob videos, derive key from title_regex
        rx = self.layout.get("title_regex")
        if not rx:
            raise SystemExit("episodic layout requires layout.title_regex")
        pat = re.compile(rx)
        globs = self.layout.get("video_globs", ["*.mkv", "*.mp4"])
        seen = {}
        for g in globs:
            for vid in sorted(self.media_root.rglob(g)):
                m = pat.search(vid.name)
                if not m:
                    continue
                gd = m.groupdict()
                if gd.get("season") and gd.get("ep"):
                    key = f"S{int(gd['season']):02d}E{int(gd['ep']):02d}"
                else:
                    key = m.group(0)
                if key in seen and seen[key] != vid:
                    print(f"WARN duplicate title key {key!r} -> keeping {seen[key].name!r}, "
                          f"ignoring {vid.name!r} (alternate rip? set an explicit titles list "
                          f"or rename to disambiguate)")
                    continue
                seen.setdefault(key, vid)   # first match wins
        return sorted(seen.items())

    def video_for(self, key):
        for k, v in self.titles():
            if k == key:
                return v
        return None


def load(path=None):
    path = Path(path or os.environ.get("SUBLOC_CONFIG") or DEFAULT_PROJECT)
    if not path.exists():
        raise SystemExit(
            f"project config not found: {path}\n"
            f"Copy the template:  cp {EXAMPLE_PROJECT} {DEFAULT_PROJECT}  and edit it.")
    return Config(_read_yaml(path), path)


if __name__ == "__main__":
    cfg = load()
    print(f"project       : {cfg.project.get('project_name')}")
    print(f"target        : {cfg.target_code} ({cfg.target_name})  suffix {cfg.srt_suffix}")
    print(f"source lang   : {cfg.source_language}")
    print(f"layout        : {cfg.layout.get('kind')}  slices/title={cfg.slices_per_title}")
    print(f"work dir      : {cfg.work}")
    print(f"readability   : {cfg.readability}")
    print(f"foreign speech: {cfg.foreign_speech}")
    print(f"banned compiled: {'yes' if cfg.banned_regex else 'no'}; "
          f"context-exempt {len(cfg.context_exempt)}; "
          f"leftover markers {'yes' if cfg.leftover_regex else 'no'}")
    try:
        ts = cfg.titles()
        print(f"discovered {len(ts)} title(s); first: {ts[0] if ts else '—'}")
    except SystemExit as e:
        print(f"title discovery: {e}")
