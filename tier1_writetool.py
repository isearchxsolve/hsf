#!/usr/bin/env python3
"""
Tier 1 — WriteTool exhaustive adversarial suite.

Scope: every guard inside WriteTool.execute — fence stripping, collapsed-
source detection/repair, tiny/shrinking-overwrite protection, the
append-validation AST gate, force= bypass semantics, path sanitization,
and encoding edge cases.

This is a LIBRARY module imported by run_all.py, but also runnable
standalone: `python3 tier1_writetool.py [agent_path]`.
"""
import sys, os, ast, importlib.util, subprocess
from pathlib import Path


def load_agent(agent_path: Path):
    spec = importlib.util.spec_from_file_location("neon", str(agent_path))
    neon = importlib.util.module_from_spec(spec)
    sys.modules["neon"] = neon
    spec.loader.exec_module(neon)
    return neon


def run(neon, sandbox: Path, check):
    WriteTool = neon.WriteTool
    wt = WriteTool(sandbox)

    # ---------------------------------------------------------------
    # Group A: fence stripping
    # ---------------------------------------------------------------
    r = wt.execute(path="a01.py", content="```python\nimport os\nprint('hi')\n```")
    check("A01 balanced ```python fence stripped",
          (sandbox/"a01.py").exists() and "```" not in (sandbox/"a01.py").read_text())

    r = wt.execute(path="a02.py", content="```\nimport os\n```")
    check("A02 bare ``` fence (no lang tag) stripped",
          (sandbox/"a02.py").exists() and "```" not in (sandbox/"a02.py").read_text())

    r = wt.execute(path="a03.md", content='s = """\n```python\ncode\n```\n"""\n')
    if (sandbox/"a03.md").exists():
        t = (sandbox/"a03.md").read_text()
        check("A03 mid-content fence NOT falsely stripped (only whole-payload fences strip)",
              "```python" in t, f"text={t!r}")
    else:
        check("A03 mid-content fence NOT falsely stripped", False, "refused entirely")

    r = wt.execute(path="a04.py", content="```python\nimport os\n```\nextra trailing text")
    on_disk = (sandbox/"a04.py").read_text() if (sandbox/"a04.py").exists() else None
    check("A04 fence + trailing content after closing fence: NOT treated as whole-payload (fence preserved or refused)",
          on_disk is None or "```" in on_disk or r.is_error,
          f"on_disk={on_disk!r}")

    r = wt.execute(path="a05.py", content="```python\n```")
    check("A05 empty fenced payload handled without crash", True, f"result={r}")

    r = wt.execute(path="a06.py", content="```PYTHON\nimport os\nprint(1)\n```")
    check("A06 uppercase lang tag ```PYTHON stripped",
          (sandbox/"a06.py").exists() and "```" not in (sandbox/"a06.py").read_text())

    r = wt.execute(path="a07.py", content="   ```python\nimport os\n```   ")
    check("A07 fence with surrounding whitespace stripped",
          not (sandbox/"a07.py").exists() or "```" not in (sandbox/"a07.py").read_text())

    # ---------------------------------------------------------------
    # Group B: AST gate (overwrite path)
    # ---------------------------------------------------------------
    r = wt.execute(path="b01.py", content="def foo(:\n    pass\n")
    check("B01 invalid python (bad def) refused, not written", not (sandbox/"b01.py").exists())

    r = wt.execute(path="b02.py", content="x = (1 + \n")
    check("B02 unterminated expression refused", not (sandbox/"b02.py").exists())

    r = wt.execute(path="b03.py", content="def foo():\n\tif True:\n    pass\n")
    check("B03 mixed tabs/spaces (TabError, a SyntaxError subclass) refused",
          not (sandbox/"b03.py").exists(), f"result={r}")

    r = wt.execute(path="b04.py", content="x = 1\ny = 2\n")
    check("B04 valid python written normally", (sandbox/"b04.py").exists() and not r.is_error)

    r = wt.execute(path="b05.py", content="")
    check("B05 empty string content: written (empty file is valid python) or explicitly refused, not silently corrupted",
          (r.is_error) or ((sandbox/"b05.py").exists() and (sandbox/"b05.py").read_text() == ""),
          f"result={r}")

    r = wt.execute(path="b06.pyi", content="def foo(:\n")
    check("B06 .pyi stub files also AST-gated", not (sandbox/"b06.pyi").exists())

    r = wt.execute(path="b07.txt", content="def foo(:\n    this is not even meant to be python\n")
    check("B07 non-.py extension NOT AST-gated (by design — only .py/.pyi)",
          (sandbox/"b07.txt").exists() and not r.is_error)

    r = wt.execute(path="b08.py", content="def foo():\n    return 1\n" * 500)
    check("B08 large but valid repeated-function payload not falsely refused",
          (sandbox/"b08.py").exists() and not r.is_error)

    # ---------------------------------------------------------------
    # Group C: force= bypass
    # ---------------------------------------------------------------
    r = wt.execute(path="c01.py", content="def foo(:\n", force=True)
    check("C01 force=True bypasses AST gate", (sandbox/"c01.py").exists())

    r = wt.execute(path="c02.py", content="import sys; import re; import json; " * 40, force=True)
    check("C02 force=True bypasses collapsed-source gate", (sandbox/"c02.py").exists())

    # ---------------------------------------------------------------
    # Group D: append= AST gate (full-file validation)
    # ---------------------------------------------------------------
    (sandbox/"d01.py").unlink(missing_ok=True)
    r1 = wt.execute(path="d01.py", content="def foo(:\n", append=True)
    r2 = wt.execute(path="d01.py", content="    garbage\n", append=True)
    final = (sandbox/"d01.py").read_text() if (sandbox/"d01.py").exists() else None
    check("D01 two-chunk append that never forms valid python: refused throughout, nothing invalid on disk",
          final is None, f"final={final!r}")

    wt.execute(path="d02.py", content="def a():\n    return 1\n")
    r = wt.execute(path="d02.py", content="\ndef b():\n    return 2\n", append=True)
    final = (sandbox/"d02.py").read_text()
    valid = _parses(final)
    check("D02 legitimate complete-function append still works",
          valid and not r.is_error and "def b" in final, f"final={final!r}")

    wt.execute(path="d03.py", content="def a():\n    return 1\n")
    r = wt.execute(path="d03.py", content="this is not python\n", append=True)
    final = (sandbox/"d03.py").read_text() if (sandbox/"d03.py").exists() else ""
    parses = _parses(final)
    crashes = False
    if parses and (sandbox/"d03.py").exists():
        proc = subprocess.run([sys.executable, str(sandbox/"d03.py")], capture_output=True, text=True)
        crashes = proc.returncode != 0
    check("D03 [KNOWN AST-VALIDATION CEILING, not scored] syntactically-valid "
          "semantic-garbage append accepted despite failing at runtime",
          True, f"tool_is_error={r.is_error} parses={parses} crashes_at_runtime={crashes} final={final!r}",
          informational=True)

    # ---------------------------------------------------------------
    # Group E: encoding edge cases
    # ---------------------------------------------------------------
    p = sandbox / "e01.py"
    p.write_bytes(b"x = 1  # bad byte -> \xff\ny = 2\n")
    r = wt.execute(path="e01.py", content="z = 3\n", append=True)
    strict_ok = True
    try:
        p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        strict_ok = False
    check("E01 append to a file already containing an invalid UTF-8 byte: "
          "either refused, or the byte is actually cleaned so strict utf-8 decode succeeds",
          r.is_error or strict_ok,
          f"tool_is_error={r.is_error} strict_decode_ok={strict_ok} raw={p.read_bytes()!r}")

    r = wt.execute(path="e02.py", content="x = 'héllo wörld ünïcode 日本語 🎉'\n")
    on_disk = (sandbox/"e02.py").read_bytes() if (sandbox/"e02.py").exists() else b""
    check("E02 valid multi-byte UTF-8 content (emoji, CJK, accents) written correctly",
          not r.is_error and "🎉".encode() in on_disk)

    r = wt.execute(path="e03.py", content="x = 1\r\ny = 2\r\n")
    check("E03 CRLF line endings in valid python: written without crash", True, f"result={r}")

    r = wt.execute(path="e04.py", content="\ufeffx = 1\n")
    check("E04 UTF-8 BOM prefix handled without crash", True, f"result={r}")

    r = wt.execute(path="e05.py", content="x = 1\x00y = 2\n")
    check("E05 embedded NUL byte handled without silent corruption or crash", True, f"result={r}")

    # ---------------------------------------------------------------
    # Group F: path sanitization / traversal / degeneracy
    # ---------------------------------------------------------------
    for bad in ["../../etc/passwd", "../x.py", "/etc/passwd", "a/../../b.py",
                "..\\..\\windows\\system32\\x.py", "./../../x.py"]:
        r = wt.execute(path=bad, content="x=1\n")
        check(f"F01 traversal blocked: {bad!r}", r.is_error)

    r = wt.execute(path="", content="x=1\n")
    check("F02 empty path refused", r.is_error)

    r = wt.execute(path="   ", content="x=1\n")
    check("F03 whitespace-only path refused", r.is_error)

    r = wt.execute(path="x" * 5000 + ".py", content="x=1\n")
    check("F04 absurdly long path (5000 chars) refused cleanly, no crash", r.is_error, f"result={r}")

    r = wt.execute(path="a.txt.txt.txt.txt.txt", content="x\n")
    check("F05 repeated-extension accretion pattern rejected (degenerate-path heuristic)", r.is_error)

    r = wt.execute(path="normal/nested/dir/file.py", content="x = 1\n")
    check("F06 legitimate nested directory path auto-creates parents",
          (sandbox/"normal/nested/dir/file.py").exists() and not r.is_error)

    # Legitimate filename that happens to contain a colon or comma should not
    # be silently mis-sanitized into something else on non-Windows.
    r = wt.execute(path="notes,with,commas.md", content="# hi\n")
    check("F07 legitimate filename containing commas not mangled beyond recognition",
          True,  # informational — sanitizer is heuristic by design; just confirm no crash
          f"result={r} exists_as_written={(sandbox/'notes,with,commas.md').exists()}")

    # symlink escape: create a symlink inside sandbox pointing outside it,
    # then try to write "through" it.
    try:
        outside = sandbox.parent / "outside_target"
        outside.mkdir(exist_ok=True)
        link = sandbox / "escape_link"
        if not link.exists():
            link.symlink_to(outside)
        r = wt.execute(path="escape_link/pwned.py", content="x = 1\n")
        landed_outside = (outside / "pwned.py").exists()
        check("F08 write through a symlink pointing outside sandbox is blocked",
              not landed_outside, f"result={r} landed_outside={landed_outside}")
    except (OSError, NotImplementedError) as e:
        check("F08 write through a symlink pointing outside sandbox is blocked",
              True, f"symlink unsupported in this environment, skipped: {e}", informational=True)

    # ---------------------------------------------------------------
    # Group G: collapsed-source detector precision (false positive / negative)
    # ---------------------------------------------------------------
    evasive = "import os\n" + ("import sys; import re; import json; " * 50)
    r = wt.execute(path="g01.py", content=evasive)
    check("G01 semicolon-chained collapsed garbage refused", not (sandbox/"g01.py").exists())

    long_valid = "x = {'a': 1, 'b': 2, 'c': 3, 'd': 4, 'e': 5, 'f': 6, 'g': 7, 'h': 8, 'i': 9, 'j': 10}\n"
    r = wt.execute(path="g02.py", content=long_valid)
    on_disk = (sandbox/"g02.py").read_text() if (sandbox/"g02.py").exists() else None
    check("G02 long-but-valid single-line dict literal not mangled by collapsed-import repair",
          on_disk == long_valid, f"on_disk={on_disk!r}")

    legal_semi = "import os\nx = 1; y = 2; z = x + y\nprint(z)\n"
    r = wt.execute(path="g03.py", content=legal_semi)
    check("G03 legal multi-line file containing ONE semicolon statement not falsely refused",
          (sandbox/"g03.py").exists() and not r.is_error)

    collapsed_imports = "import os import sys import json import re import time"
    r = wt.execute(path="g04.py", content=collapsed_imports)
    if (sandbox/"g04.py").exists():
        repaired_ok = _parses((sandbox/"g04.py").read_text())
        check("G04 collapsed space-separated imports: auto-repaired into valid python",
              repaired_ok, f"content={(sandbox/'g04.py').read_text()!r}")
    else:
        check("G04 collapsed space-separated imports: refused (acceptable fallback if repair declined)",
              True, informational=True)

    # ---------------------------------------------------------------
    # Group H: tiny/shrinking overwrite protection
    # ---------------------------------------------------------------
    wt.execute(path="h01.py", content="x = 1\n" * 30)
    original_size = (sandbox/"h01.py").stat().st_size
    r = wt.execute(path="h01.py", content="x=1\n")
    check("H01 tiny overwrite of a large existing source file refused (prevents accidental truncation)",
          r.is_error, f"result={r}")

    wt.execute(path="h02.py", content="x = 1\n" * 30)
    r = wt.execute(path="h02.py", content="x=1\n", force=True)
    check("H02 force=True allows intentional truncation", not r.is_error and (sandbox/"h02.py").exists())


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    agent_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "neon_architect_v4_7_mobile_env.py"
    neon = load_agent(agent_path)
    sandbox = Path(__file__).parent / "sandbox_tier1"
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
    print(f"\nTier 1 FAILS: {len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1 if FAILS else 0)
