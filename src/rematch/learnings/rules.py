"""Learnings as versioned rules with evidence (DESIGN D9, Phase 3 B5).

Two scopes, each a JSON file that is the source of truth plus a generated markdown view:
- compiler: `docs/learnings/<compiler>.json` in rematch (public, target-agnostic), shared by
  every target built with that compiler family (target.toml `[worker] compiler`)
- target: `<TARGET_REPO>/.rematch/learnings.json`

Rules from `learnings curate` carry an evidence pair: two C snippets whose generated code
differs the way the rule says (`expect` is a regex that must match a line present in only one
of them). rematch re-compiles the evidence before accepting a rule and whenever `learnings
verify` runs. Rules a human accepts with `learnings review` have no evidence until a curation
pass adds it.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

REMATCH_DOCS = Path(__file__).resolve().parents[3] / "docs" / "learnings"
MAX_LINES = 150


@dataclass
class Evidence:
    a: str  # C source using types.h
    b: str
    expect: str  # regex over the instruction lines that differ


@dataclass
class Rule:
    id: str
    text: str
    evidence: Evidence | None
    sources: list[int] = field(default_factory=list)  # learning_candidates ids
    verified: str | None = None  # ISO date of the last passing verification


@dataclass
class RuleFile:
    path: Path
    title: str
    rules: list[Rule]

    @classmethod
    def load(cls, path: Path, title: str) -> RuleFile:
        if not path.is_file():
            return cls(path, title, [])
        data = json.loads(path.read_text(encoding="utf-8"))
        rules = [
            Rule(
                r["id"],
                r["text"],
                Evidence(**r["evidence"]) if r.get("evidence") else None,
                r.get("sources", []),
                r.get("verified"),
            )
            for r in data.get("rules", [])
        ]
        return cls(path, data.get("title", title), rules)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"title": self.title, "rules": [asdict(r) for r in self.rules]}
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.path.with_suffix(".md").write_text(self.markdown(), encoding="utf-8")

    def next_id(self, prefix: str) -> str:
        n = len(prefix)
        used = {int(r.id[n:]) for r in self.rules if r.id.startswith(prefix) and r.id[n:].isdigit()}
        return f"{prefix}{max(used, default=0) + 1}"

    def markdown(self) -> str:
        lines = [f"# {self.title}", ""]
        if not self.rules:
            lines.append("(No curated rules yet.)")
        lines += [f"- {r.text}" for r in self.rules]
        return "\n".join(lines) + "\n"


def compiler_file(compiler: str) -> RuleFile:
    return RuleFile.load(REMATCH_DOCS / f"{compiler}.json", f"Compiler quirks: {compiler}")


def target_file(target_dir: Path) -> RuleFile:
    return RuleFile.load(target_dir / "learnings.json", "Target notes")


def sandbox_text(files: list[RuleFile]) -> str:
    """LEARNINGS.md for a sandbox: every scope's rules, compiler first."""
    return "\n".join(f.markdown() for f in files)


# --- choosing rules for TASK.md (SPEC §13.1) --------------------------------------------

_WORD = re.compile(r"[a-z_][a-z0-9_]*")
# Words of rule text that name a code shape; a target "has" one when its assembly shows it.
_SHAPES = {
    "switch": r"\badd\s+pc\b|\.short\b",
    "stack": r"\bsp\b",
    "bitfield": r"\blsls\b.*\n.*\blsrs\b",
    "signed": r"\bldrs[bh]\b|\basrs\b",
    "struct": r"\bldm|\bstm",
}


def target_features(target_s: str) -> set[str]:
    """Mnemonics and code shapes in a target's assembly (`loop`: a backward branch)."""
    words = {m.split(".")[0] for m in re.findall(r"^\s*([a-z][a-z0-9.]*)\b", target_s, re.M)}
    shapes = {name for name, pat in _SHAPES.items() if re.search(pat, target_s, re.M)}
    seen: set[str] = set()
    for line in target_s.splitlines():
        label = re.match(r"^(\.L\d+):", line)
        if label:
            seen.add(label.group(1))
        branch = re.search(r"\bb[a-z]*\s+(\.L\d+)", line)
        if branch and branch.group(1) in seen:
            shapes.add("loop")
            break
    return words | shapes


def relevance(rule: Rule, features: set[str], idf: dict[str, float]) -> float:
    """Shared words weighted by how rare they are among the rules, plus code shapes."""
    text = rule.text.lower()
    words = set(_WORD.findall(text))
    score = sum(idf.get(w, 0.0) for w in words & features)
    score += sum(2.0 for shape in (*_SHAPES, "loop") if shape in features and shape in text)
    return score


def selected_text(files: list[RuleFile], target_s: str, k: int) -> str:
    """The `k` rules most relevant to this target, every scope, for TASK.md; the full set
    stays in LEARNINGS.md."""
    import math

    features = target_features(target_s)
    rules = [r for f in files for r in f.rules]
    df: dict[str, int] = {}
    for r in rules:
        for w in set(_WORD.findall(r.text.lower())):
            df[w] = df.get(w, 0) + 1
    idf = {w: math.log((1 + len(rules)) / (1 + n)) for w, n in df.items()}
    ranked = sorted(
        ((relevance(r, features, idf), -i, r) for i, r in enumerate(rules)),
        key=lambda t: (t[0], t[1]),
        reverse=True,
    )
    total = sum(len(f.rules) for f in files)
    chosen = [r for score, _, r in ranked[:k] if score > 0]
    if not chosen:
        return "(No rules match this function's shape; LEARNINGS.md has all of them.)"
    lines = [f"- {r.text}" for r in chosen]
    if total > len(chosen):
        lines.append(f"\n({total - len(chosen)} more rules in LEARNINGS.md: grep it by topic.)")
    return "\n".join(lines)
