"""m2c first drafts (Phase 3, B2). m2c's `arm` target handles Thumb; mwcc mode.

The draft comes from the same normalized lines the worker sees in target.s, re-emitted as GNU
assembly with branch labels and literal pools. A failing m2c means no draft, never an error.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from rematch.diff.objdump_backend import Line

_ANON = re.compile(r"<(?:str|data|bss|anon):[^>]*>")
_OUT = re.compile(r"<out:[^>]*>")


def m2c_asm(symbol: str, lines: list[Line]) -> str:
    """GNU assembly for m2c: labels restored, anonymous targets given placeholder names."""
    anon: dict[str, str] = {}

    def name(m: re.Match[str]) -> str:
        return anon.setdefault(m.group(0), f"anon_{len(anon)}")

    out = [".syntax unified", ".thumb", "", f"{symbol}:"]
    for ln in lines:
        if ln.label:
            out.append(f"{ln.label}:")
        ops = _ANON.sub(name, ln.operands.replace("<self>", symbol))
        ops = _OUT.sub(".Lout", ops)
        out.append(
            f"\t{ln.mnemonic.split('.')[0] if not ln.is_data else ln.mnemonic} {ops}".rstrip()
        )
    return "\n".join(out) + "\n"


def run_m2c(m2c: Path, asm: str, context: Path | None, timeout: float = 60) -> str | None:
    """m2c's C for the function, or None. Tries with the context first, then without."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "target.s"
        src.write_text(asm, encoding="utf-8")
        attempts = [["--context", str(context)]] if context is not None else []
        attempts.append([])
        for extra in attempts:
            try:
                result = subprocess.run(
                    ["python3", str(m2c), "--target", "arm-mwcc-c", *extra, str(src)],
                    cwd=m2c.parent,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                continue
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
    return None


_PROTO = re.compile(
    r"^(?P<head>[^;{}]*?\b(?P<name>\w+)\s*\((?P<params>[^;{}]*)\))\s*;", re.MULTILINE
)


def apply_prototype(function: str, symbol: str, declarations: str) -> str:
    """Give m2c's function the real prototype, renaming its argN parameters by position."""
    plain = "\n".join(ln for ln in declarations.splitlines() if not ln.lstrip().startswith("//"))
    proto = next((m for m in _PROTO.finditer(plain) if m.group("name") == symbol), None)
    if proto is None:
        return function
    brace = function.find("{")
    if brace < 0:
        return function
    params = [p.strip() for p in proto.group("params").split(",") if p.strip()]
    names = [re.findall(r"\w+", p)[-1] if p != "void" else None for p in params]
    body = function[brace:]
    for i, real in enumerate(names):
        if real:
            body = re.sub(rf"\barg{i}\b", real, body)
    return " ".join(proto.group("head").split()) + " " + body
