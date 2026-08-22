#!/usr/bin/env python3
"""
Tier 3 — SearchTool, GlobTool, LsTool, RunTool.

Scope: ReDoS resistance in SearchTool's user-controlled regex, path
traversal on read-only tools (lower stakes than write, but still an
info-disclosure surface), and RunTool's structural (argv-only, no shell)
design plus its destructive-git-operation allowlist.
"""
import sys, time
from pathlib import Path


def run(neon, sandbox: Path, check, agent_path_for_subprocess=None):
    WriteTool = neon.WriteTool
    SearchTool = neon.SearchTool
    GlobTool = neon.GlobTool
    LsTool = neon.LsTool
    RunTool = neon.RunTool

    wt = WriteTool(sandbox)
    st = SearchTool(sandbox)
    gt = GlobTool(sandbox)
    lt = LsTool(sandbox)
    rt = RunTool(sandbox)

    # seed some files to search/list
    wt.execute(path="src/app.py", content="import os\nSECRET = 'not really'\n")
    wt.execute(path="src/util.py", content="def helper():\n    pass\n")
    wt.execute(path="README.md", content="# hi\n")

    # ---------------------------------------------------------------
    # Group A: SearchTool — traversal and ReDoS
    # ---------------------------------------------------------------
    r = st.execute(pattern="SECRET", path="../../etc")
    check("A01 search path traversal outside sandbox blocked", r.is_error, f"result={r}")

    r = st.execute(pattern="", path=".")
    check("A02 empty pattern refused with a clear error", r.is_error)

    # classic catastrophic-backtracking pattern: (a+)+$ against a
    # non-matching string with many 'a's. If SearchTool compiles this
    # directly against user-controlled file content, this can hang the
    # WHOLE PROCESS (confirmed: Python's own re engine hangs on this
    # independent of SearchTool's code). Run it OUT OF PROCESS with a
    # hard timeout so a genuine hang here doesn't take the rest of this
    # test suite down with it.
    evil_pattern = r"(a+)+$"
    wt.execute(path="src/redos_target.py", content="a" * 35 + "b\n")
    import subprocess as _sp
    probe = (
        "import sys, importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('neon', {str(agent_path_for_subprocess)!r})\n"
        "neon = importlib.util.module_from_spec(spec)\n"
        "sys.modules['neon'] = neon\n"
        "spec.loader.exec_module(neon)\n"
        f"st = neon.SearchTool({str(sandbox)!r})\n"
        f"r = st.execute(pattern={evil_pattern!r}, path='src/redos_target.py')\n"
        "print('DONE', r.is_error)\n"
    )
    start = time.time()
    try:
        proc = _sp.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=8)
        elapsed = time.time() - start
        hung = False
    except _sp.TimeoutExpired:
        elapsed = time.time() - start
        hung = True
    # This is scored as a real FAIL if it hangs — a timeout here means the
    # tool itself has no protection against catastrophic backtracking,
    # which is the actual vulnerability. The subprocess wrapper only
    # exists so THIS TEST SUITE survives confirming that; it is not a
    # mitigation present in SearchTool itself.
    check("A03 SearchTool has NO timeout/complexity guard around user-controlled regex — "
          "(a+)+$ against 35 a's + non-matching suffix causes catastrophic backtracking "
          "with zero protection (confirmed: Python's own re engine hangs on this "
          "pattern independent of SearchTool's code; SearchTool adds no timeout)",
          not hung,
          f"elapsed={elapsed:.2f}s hung={hung}. "
          f"{'CONFIRMED VULNERABLE — a single crafted pattern argument hangs the tool indefinitely.' if hung else 'did not hang in this run'}")

    r = st.execute(pattern="[", path=".")
    check("A04 invalid regex syntax (unclosed bracket) refused cleanly, not a raw traceback",
          r.is_error, f"result={r}")

    r = st.execute(pattern="SECRET", path="src/app.py")
    check("A05 legitimate single-file search finds the match",
          not r.is_error and "SECRET" in r.output)

    # ---------------------------------------------------------------
    # Group B: GlobTool — traversal and pattern edge cases
    # ---------------------------------------------------------------
    r = gt.execute(pattern="*.py", path="../../")
    check("B01 glob path traversal outside sandbox blocked", r.is_error, f"result={r}")

    r = gt.execute(pattern="**/*.py", path=".")
    check("B02 recursive glob finds nested python files",
          not r.is_error and "app.py" in r.output and "util.py" in r.output)

    r = gt.execute(pattern="", path=".")
    check("B03 empty glob pattern handled without crash", True, f"result={r}")

    # ---------------------------------------------------------------
    # Group C: LsTool — traversal
    # ---------------------------------------------------------------
    r = lt.execute(path="../../")
    check("C01 ls path traversal outside sandbox blocked", r.is_error, f"result={r}")

    r = lt.execute(path=".")
    check("C02 legitimate ls of project root works", not r.is_error and "src" in r.output)

    r = lt.execute(path="does/not/exist")
    check("C03 ls of nonexistent path refused cleanly, not a raw traceback", r.is_error)

    # ---------------------------------------------------------------
    # Group D: RunTool — destructive git guard
    # ---------------------------------------------------------------
    r = rt.execute(program="git", args=["push", "--force"])
    check("D01 git push --force refused outright", r.is_error, f"result={r}")

    r = rt.execute(program="git", args=["push", "-f"])
    check("D02 git push -f (short flag) also refused", r.is_error, f"result={r}")

    r = rt.execute(program="git", args=["push", "--force-with-lease"])
    check("D03 git push --force-with-lease also refused (still rewrites remote history)",
          r.is_error, f"result={r}")

    r = rt.execute(program="git", args=["reset", "--hard"])
    check("D04 git reset --hard refused", r.is_error, f"result={r}")

    r = rt.execute(program="git", args=["clean", "-fd"])
    check("D05 git clean -fd refused", r.is_error, f"result={r}")

    r = rt.execute(program="git", args=["branch", "-D", "main"])
    check("D06 git branch -D refused", r.is_error, f"result={r}")

    r = rt.execute(program="git", args=["status"])
    check("D07 non-destructive git command (status) allowed through the guard",
          not r.is_error or "REFUSED" not in (r.error or "") and "destructive" not in (r.error or "").lower(),
          f"result={r}")

    # Case-sensitivity bypass attempt on the destructive-flag check
    r = rt.execute(program="git", args=["push", "--FORCE"])
    check("D08 [CASING BYPASS PROBE] git push --FORCE (uppercase flag) — "
          "confirms whether the destructive-flag matcher is case-insensitive",
          r.is_error, f"result={r}")

    # Combined-short-flag bypass attempt: git clean -xfd bundles -f into a
    # combined short-option string rather than a standalone '-f' token.
    r = rt.execute(program="git", args=["clean", "-xfd"])
    check("D09 [COMBINED-FLAG BYPASS PROBE] git clean -xfd (force bundled into "
          "a combined short flag, not standalone '-f'/'-fd'/'-fdx'/'--force')",
          r.is_error, f"result={r}")

    # ---------------------------------------------------------------
    # Group E: RunTool — non-git programs still constrained to the enum
    # ---------------------------------------------------------------
    r = rt.execute(program="bash", args=["-c", "echo pwned"])
    check("E01 arbitrary program outside the enum (bash) rejected by schema/dispatch",
          r.is_error, f"result={r}")

    r = rt.execute(program="python", args=["-c", "import os; os.system('echo shell-escape')"])
    check("E02 python -c with an os.system call inside it: "
          "tool itself doesn't add a shell layer (the python process choosing to "
          "shell out is a property of the executed code, not RunTool — informational)",
          True, f"result={r}", informational=True)


if __name__ == "__main__":
    import importlib.util
    agent_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "neon_architect_v4_7_mobile_env.py"
    spec = importlib.util.spec_from_file_location("neon", str(agent_path))
    neon = importlib.util.module_from_spec(spec)
    sys.modules["neon"] = neon
    spec.loader.exec_module(neon)

    sandbox = Path(__file__).parent / "sandbox_tier3"
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

    run(neon, sandbox, check, agent_path_for_subprocess=agent_path)
    print(f"\nTier 3 FAILS: {len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1 if FAILS else 0)
