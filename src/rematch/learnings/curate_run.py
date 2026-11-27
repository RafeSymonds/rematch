"""`rematch learnings curate`: an agent curates candidates; rematch verifies before applying.

The agent proposes accept/merge/reject per candidate (plus rewrites and retirements) with
evidence pairs it tested with `./try`. rematch re-runs every accepted pair (`trial.py`) and
moves compiler-scope rules that name the target's code into target scope. Nothing the agent
claims is applied unverified.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rematch import prompts
from rematch.context.header_index import HeaderIndex
from rematch.learnings.curate import decide
from rematch.learnings.rules import Evidence, Rule, RuleFile, compiler_file, target_file
from rematch.learnings.trial import TYPES_H, Compiler, evidence_holds, modal_entry, run_trial
from rematch.session import Session
from rematch.target.definition import target_dir
from rematch.toolchain.compile_db import CompileDB
from rematch.worker.attempt import deny_roots
from rematch.worker.invoke import InvokeConfig, run_worker
from rematch.worker.task import CompileSpec

Log = Callable[[str], None]
CURATE_TOOLS = (
    "Read",
    "Edit(evidence/**)",
    "Write(evidence/**)",
    "Edit(decisions.json)",
    "Write(decisions.json)",
    "Bash(./try:*)",
)
_C_WORDS = frozenset(
    [
        "int",
        "char",
        "short",
        "long",
        "unsigned",
        "signed",
        "void",
        "struct",
        "union",
        "enum",
        "typedef",
        "static",
        "const",
        "volatile",
        "return",
        "if",
        "else",
        "for",
        "while",
        "do",
        "switch",
        "case",
        "break",
        "continue",
        "sizeof",
    ]
)


def compiler_for(s: Session) -> Compiler:
    """The target's main C compile argv (primary version) and trial header as a compiler."""
    build = s.definition.require(s.definition.build_info, "build")
    db = CompileDB.load(
        s.definition.build_dir(s.worktree, s.primary_version) / build.compile_commands, s.worktree
    )
    entry = modal_entry(list(db._entries))
    return Compiler(
        entry, s.runner, s.settings.objdump, s.settings.compile_timeout_sec, trial_header(s)
    )


def trial_header(s: Session) -> str:
    name = s.definition.worker.trial_header
    path = target_dir(s.settings.target_repo) / name if name else TYPES_H
    return path.read_text(encoding="utf-8")


def compiler_name(s: Session) -> str:
    return s.definition.worker.compiler or "default"


def rule_files(s: Session) -> tuple[RuleFile, RuleFile]:
    return compiler_file(compiler_name(s)), target_file(target_dir(s.settings.target_repo))


def target_names(s: Session, conn: sqlite3.Connection) -> set[str]:
    """Identifiers that belong to the target: function names and header declarations."""
    names = {str(n) for (n,) in conn.execute("SELECT name FROM functions")}
    if s.header_index_path.is_file():
        index = HeaderIndex.from_json(s.header_index_path.read_text(encoding="utf-8"))
        names |= set(index._by_name)
    generic = set(re.findall(r"\b\w+\b", trial_header(s)))
    return names - generic - _C_WORDS


_INCLUDE = re.compile(r"^\s*#\s*include\b.*$", re.MULTILINE)


def _distinctive(word: str) -> bool:
    """Plain lowercase words ("loop", "data") are never evidence of naming the target."""
    return "_" in word or any(c.isdigit() or c.isupper() for c in word)


def names_target(text: str, names: set[str]) -> list[str]:
    words = re.findall(r"\b[A-Za-z_]\w{3,}\b", _INCLUDE.sub("", text))
    return sorted({w for w in words if w in names and _distinctive(w)})


# --- sandbox ---------------------------------------------------------------------------


def build_sandbox(
    s: Session, root: Path, candidates: list[tuple[int, str]], files: tuple[RuleFile, ...]
) -> Path:
    control = root / "control"
    sandbox = root / "sandbox"
    (sandbox / "evidence").mkdir(parents=True)
    control.mkdir(parents=True)
    c = compiler_for(s)
    (sandbox / "types.h").write_text(c.header, encoding="utf-8")
    (sandbox / "CANDIDATES.md").write_text(
        "# Candidates\n\n" + "\n".join(f"{cid}: {text}" for cid, text in candidates) + "\n",
        encoding="utf-8",
    )
    rules = []
    for f in files:
        rules.append(f"## {f.title}")
        rules += [f"{r.id}: {r.text}" for r in f.rules] or ["(none)"]
    (sandbox / "RULES.md").write_text("# Rules\n\n" + "\n".join(rules) + "\n", encoding="utf-8")
    (sandbox / "decisions.json").write_text("{}\n", encoding="utf-8")
    (control / "trial.json").write_text(
        json.dumps(
            {
                "compile": CompileSpec.of(c.entry).model_dump(),
                "toolchain_exec": s.settings.toolchain_exec,
                "objdump": c.objdump,
                "timeout": c.timeout,
                "header": c.header,
            }
        ),
        encoding="utf-8",
    )
    rematch = Path(sys.executable).parent / "rematch"
    tool = sandbox / "try"
    tool.write_text(
        f'#!/bin/sh\nexec "{rematch}" try --control "{control}" "$@"\n', encoding="utf-8"
    )
    tool.chmod(0o755)
    return sandbox


# --- applying decisions ----------------------------------------------------------------


@dataclass
class Applied:
    accepted: list[str] = field(default_factory=list)
    merged: list[int] = field(default_factory=list)
    rejected: list[tuple[int, str]] = field(default_factory=list)
    # accepted by the agent but failed rematch's evidence check
    dropped: list[tuple[int, str]] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    moved_to_target: list[str] = field(default_factory=list)


def _items(decisions: dict[str, object], key: str) -> list[object]:
    value = decisions.get(key)
    return value if isinstance(value, list) else []


def apply_decisions(
    s: Session,
    conn: sqlite3.Connection,
    sandbox: Path,
    decisions: dict[str, object],
    pending_ids: set[int],
    log: Log,
) -> Applied:
    comp, tgt = rule_files(s)
    compiler = compiler_for(s)
    names = target_names(s, conn)
    out = Applied()
    today = datetime.now(UTC).date().isoformat()
    work = s.settings.rematch_var / "learnings" / "verify"

    def mark(cid: int, accepted: bool) -> None:
        decide(conn, cid, accept=accepted)

    for d in _items(decisions, "decisions"):
        if not isinstance(d, dict):
            continue
        cid = d.get("candidate")
        if not isinstance(cid, int) or cid not in pending_ids:
            continue
        action = d.get("action")
        if action == "merge":
            mark(cid, accepted=True)
            out.merged.append(cid)
        elif action == "accept":
            ev = d.get("evidence") or {}
            try:
                a = (sandbox / str(ev["a"])).read_text(encoding="utf-8")
                b = (sandbox / str(ev["b"])).read_text(encoding="utf-8")
                expect = str(ev["expect"])
            except (KeyError, OSError, TypeError):
                mark(cid, accepted=False)
                out.dropped.append((cid, "missing evidence"))
                continue
            ok, why = evidence_holds(run_trial(compiler, a, b, work / str(cid)), expect)
            if not ok:
                mark(cid, accepted=False)
                out.dropped.append((cid, why))
                continue
            text = " ".join(str(d.get("text", "")).split())
            scope = "target" if d.get("scope") == "target" else "compiler"
            leaked = names_target("\n".join((text, a, b)), names) if scope == "compiler" else []
            if leaked:
                scope = "target"
            dest = tgt if scope == "target" else comp
            rid = dest.next_id("T" if scope == "target" else "C")
            dest.rules.append(Rule(rid, text, Evidence(a, b, expect), [cid], today))
            if leaked:
                out.moved_to_target.append(f"{rid} ({', '.join(leaked[:3])})")
            mark(cid, accepted=True)
            out.accepted.append(rid)
        else:
            mark(cid, accepted=False)
            out.rejected.append((cid, str(d.get("reason", ""))[:200]))

    by_id = {r.id: (f, r) for f in (comp, tgt) for r in f.rules}
    for item in _items(decisions, "rewrite"):
        if isinstance(item, dict) and item.get("rule") in by_id and item.get("text"):
            _, rule = by_id[str(item["rule"])]
            rule.text = " ".join(str(item["text"]).split())
            out.rewritten.append(rule.id)
    for item in _items(decisions, "retire"):
        if isinstance(item, dict) and item.get("rule") in by_id:
            f, rule = by_id[str(item["rule"])]
            f.rules.remove(rule)
            out.retired.append(rule.id)
    comp.save()
    tgt.save()
    log(
        f"applied: {len(out.accepted)} accepted, {len(out.dropped)} dropped by the check, "
        f"{len(out.merged)} merged, {len(out.rejected)} rejected"
    )
    return out


def verify_all(s: Session, retire: bool, log: Log) -> list[tuple[str, bool | None, str]]:
    """Re-run every rule's evidence (no tokens). Optionally retire rules that no longer hold.

    Rules without evidence (human-accepted) report None and are kept."""
    compiler = compiler_for(s)
    results: list[tuple[str, bool | None, str]] = []
    today = datetime.now(UTC).date().isoformat()
    for f in rule_files(s):
        keep: list[Rule] = []
        for r in f.rules:
            if r.evidence is None:
                results.append((r.id, None, "no evidence (human-accepted)"))
                keep.append(r)
                continue
            t = run_trial(
                compiler,
                r.evidence.a,
                r.evidence.b,
                s.settings.rematch_var / "learnings" / "verify" / r.id,
            )
            ok, why = evidence_holds(t, r.evidence.expect)
            results.append((r.id, ok, why))
            if ok:
                r.verified = today
            if ok or not retire:
                keep.append(r)
            else:
                log(f"retired {r.id}: {why}")
        f.rules = keep
        f.save()
    return results


class LearningsError(RuntimeError):
    pass


def digest(s: Session) -> str:
    """Identifies what workers are told and how it was checked: rules, trial header, pin."""
    files = rule_files(s)
    rules = [
        [r.id, r.text, asdict(r.evidence) if r.evidence else None] for f in files for r in f.rules
    ]
    blob = json.dumps([rules, trial_header(s), s.settings.target_pin], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def ensure_verified(s: Session, log: Log) -> str:
    """The check before every run: each rule's evidence must still hold (cached by digest)."""
    key = digest(s)
    if not any(r.evidence for f in rule_files(s) for r in f.rules):
        return key
    marker = s.settings.rematch_var / "learnings" / "verified" / key
    if marker.is_file():
        return key
    failed = [(rid, why) for rid, ok, why in verify_all(s, False, log) if ok is False]
    if failed:
        raise LearningsError(
            "learnings rules fail their evidence: "
            + "; ".join(f"{rid}: {why}" for rid, why in failed)
            + ". Run `rematch learnings verify --retire` or fix the rules."
        )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return digest(s)  # verify_all refreshed `verified` dates, which the digest ignores


def pending_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM learning_candidates WHERE status = 'pending'")
    return int(row.fetchone()[0])


def run_curation(
    s: Session,
    conn: sqlite3.Connection,
    batch: int,
    model: str,
    effort: str,
    log: Log,
) -> tuple[Applied, float]:
    rows = conn.execute(
        "SELECT id, text FROM learning_candidates WHERE status = 'pending' ORDER BY id LIMIT ?",
        (batch,),
    ).fetchall()
    candidates = [(int(i), str(t)) for i, t in rows]
    if not candidates:
        return Applied(), 0.0
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    root = s.settings.rematch_workdir.expanduser() / "curate" / stamp
    sandbox = build_sandbox(s, root, candidates, rule_files(s))
    system = s.settings.rematch_var / "learnings" / f"curate-{stamp}.md"
    system.parent.mkdir(parents=True, exist_ok=True)
    system.write_text(prompts.render("curate.md"), encoding="utf-8")
    cfg = InvokeConfig(
        claude_bin=s.settings.claude_bin,
        model=model,
        effort=effort,
        auth_mode=s.settings.worker_auth_mode,
        max_turns=s.settings.max_turns * 2,
        timeout_sec=s.settings.attempt_timeout_sec * 2,
        max_budget_usd=s.settings.max_budget_usd_per_attempt,
        system_prompt_file=system,
        deny_read_roots=deny_roots(s),
        api_key=s.settings.anthropic_api_key.get_secret_value(),
        allowed_tools=CURATE_TOOLS,
    )
    prompt = (
        f"Curate these {len(candidates)} candidates (CANDIDATES.md) against RULES.md, "
        "then write decisions.json."
    )
    stream = s.settings.rematch_var / "logs" / "curate" / f"{stamp}.jsonl"
    result = run_worker(cfg, sandbox, prompt, stream)
    log(f"curation agent: {result.subtype}, {result.num_turns} turns, ${result.cost_usd}")
    try:
        decisions = json.loads((sandbox / "decisions.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"decisions.json unreadable: {exc}")
        decisions = {}
    applied = apply_decisions(
        s,
        conn,
        sandbox,
        decisions if isinstance(decisions, dict) else {},
        {cid for cid, _ in candidates},
        log,
    )
    return applied, result.cost_usd or 0.0
