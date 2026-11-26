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
