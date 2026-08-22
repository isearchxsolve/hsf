#!/usr/bin/env python3
"""
Tier 2 — EditTool exhaustive adversarial suite.

Scope: fence stripping on new_text (both replace_all and single-occurrence),
the post-splice AST gate, ambiguous-match handling, replace_all correctness,
and encoding edge cases specific to read-modify-write.
"""
import sys, ast, subprocess
from pathlib import Path


def run(neon, sandbox: Path, check):
    EditTool = neon.EditTool
    et = EditTool(sandbox)

    def w(name, content):
        (sandbox / name).write_text(content)

    # ---------------------------------------------------------------
    # Group A: fence stripping on new_text
    # ---------------------------------------------------------------
    w("a01.py", "x = 1\n")
    r = et.execute(path="a01.py", old_text="x = 1", new_text="```python\nx = 2\n```")
    final = (sandbox/"a01.py").read_text()
    check("A01 single-occurrence edit strips fence from new_text",
          "```" not in final, f"final={final!r}")

    w("a02.py", "x = 1\nx = 1\n")
    r = et.execute(path="a02.py", old_text="x = 1", new_text="```python\ny = 2\n```", replace_all=True)
    final = (sandbox/"a02.py").read_text()
    check("A02 replace_all also strips fence from new_text",
          "```" not in final, f"final={final!r}")

    # ---------------------------------------------------------------
    # Group B: post-splice AST validation (the round-2 fix)
    # ---------------------------------------------------------------
    w("b01.py", "def foo():\n    return 1\n")
    r = et.execute(path="b01.py", old_text="return 1", new_text="return 1\ndef broken(:\n    pass")
    final = (sandbox/"b01.py").read_text()
    check("B01 edit that injects a syntax error is refused, file stays valid",
          _parses(final) and r.is_error, f"final={final!r} is_error={r.is_error}")

    w("b02.py", "class Foo:\n    def bar(self):\n        if True:\n            return 1\n")
    r = et.execute(path="b02.py", old_text="        if True:\n            return 1", new_text="return 1")
    final = (sandbox/"b02.py").read_text()
    check("B02 dedent-breaking edit (orphaned statement) refused, file stays valid",
          _parses(final) and r.is_error, f"final={final!r} is_error={r.is_error}")

    w("b03.py", "x = 1\n")
    r = et.execute(path="b03.py", old_text="x = 1", new_text="x = 1\ny = 2\n")
    final = (sandbox/"b03.py").read_text()
    check("B03 legitimate multi-line valid replacement still succeeds",
          _parses(final) and not r.is_error and "y = 2" in final)

    w("b04.md", "# Title\ncontent here\n")
    r = et.execute(path="b04.md", old_text="content here", new_text="```python\ndef broken(:\n```")
    final = (sandbox/"b04.md").read_text()
    check("B04 non-.py file NOT AST-gated (by design), fence still stripped or preserved sanely",
          not r.is_error, f"final={final!r}")

    w("b05.py", "def a():\n    return 1\n\ndef b():\n    return 2\n")
    r = et.execute(path="b05.py", old_text="def a():\n    return 1",
                    new_text="def a():\n    return 1\n\ndef c():\n    return 3")
    final = (sandbox/"b05.py").read_text()
    check("B05 edit that inserts a valid new function between existing ones succeeds",
          _parses(final) and not r.is_error and "def c" in final)

    # ---------------------------------------------------------------
    # Group C: ambiguous match / replace_all correctness
    # ---------------------------------------------------------------
    w("c01.py", "x = 1\nx = 1\nx = 1\n")
    r = et.execute(path="c01.py", old_text="x = 1", new_text="x = 2")
    final = (sandbox/"c01.py").read_text()
    check("C01 non-replace_all edit with ambiguous match only replaces first occurrence + warns",
          final.count("x = 2") == 1 and "WARNING" in r.output,
          f"final={final!r} output={r.output!r}")

    w("c02.py", "x = 1\nx = 1\nx = 1\n")
    r = et.execute(path="c02.py", old_text="x = 1", new_text="x = 2", replace_all=True)
    final = (sandbox/"c02.py").read_text()
    check("C02 replace_all replaces every occurrence",
          final.count("x = 2") == 3, f"final={final!r}")

    r = et.execute(path="c03_missing.py", old_text="doesnt exist", new_text="x")
    check("C03 edit on nonexistent file refused cleanly", r.is_error)

    w("c04.py", "x = 1\n")
    r = et.execute(path="c04.py", old_text="y = 999", new_text="z = 1")
    check("C04 old_text not found in file refused with a diagnostic hint",
          r.is_error and len(r.error) > 0)

    # ---------------------------------------------------------------
    # Group D: encoding edge cases on read-modify-write
    # ---------------------------------------------------------------
    p = sandbox / "d01.py"
    p.write_bytes(b"x = 1  # note \xff here\ny = 2\n")
    r = et.execute(path="d01.py", old_text="y = 2", new_text="y = 3")
    final_bytes = p.read_bytes()
    check("D01 [KNOWN GAP — same class as WriteTool E01] editing a file with an invalid "
          "UTF-8 byte: byte survives unmangled, OR edit is refused outright",
          (b"\xff" in final_bytes) or r.is_error,
          f"is_error={r.is_error} final_bytes={final_bytes!r}",
          )

    w("d02.py", "x = 'plain'\n")
    r = et.execute(path="d02.py", old_text="'plain'", new_text="'héllo 日本語 🎉'")
    final = (sandbox/"d02.py").read_text()
    check("D02 multi-byte UTF-8 replacement content handled correctly",
          not r.is_error and "🎉" in final)

    # ---------------------------------------------------------------
    # Group E: empty / degenerate new_text
    # ---------------------------------------------------------------
    w("e01.py", "x = 1\ny = 2\n")
    r = et.execute(path="e01.py", old_text="x = 1\n", new_text="")
    final = (sandbox/"e01.py").read_text()
    check("E01 replacing text with empty string (deletion) handled, result still valid or refused",
          _parses(final) or r.is_error, f"final={final!r}")

    w("e02.py", "x = 1\n")
    r = et.execute(path="e02.py", old_text="", new_text="y = 2\n")
    check("E02 empty old_text handled without crash (either refused or matches everywhere)",
          True, f"result={r}")


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    import importlib.util
    agent_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "neon_architect_v4_7_mobile_env.py"
    spec = importlib.util.spec_from_file_location("neon", str(agent_path))
    neon = importlib.util.module_from_spec(spec)
    sys.modules["neon"] = neon
    spec.loader.exec_module(neon)

    sandbox = Path(__file__).parent / "sandbox_tier2"
    if sandbox.exists():
        import shutil
        shutil.rmtree(sandbox)
    sandbox.mkdir()

    FAILS = []
    def check(name, cond, detail="", informational=False):
        tag = "INFO" if informational else ("PASS" if cond else "FAIL")
        print(f"[{tag}] {name}" + (f"  -- {detail}" if detail else ""))
        if not cond and not informational:
            FAILS.append(name)

    run(neon, sandbox, check)
    print(f"\nTier 2 FAILS: {len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1 if FAILS else 0)
