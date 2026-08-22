#!/usr/bin/env python3
"""
run_all.py — master runner for the hardened adversarial suite.

Usage:
    python3 run_all.py [path/to/neon_architect_v4_7_mobile_env.py]

Runs Tier 1-4 against the given agent file (or the copy sitting next to
this script by default) and prints a combined summary plus an explicit
SCOPE section — what this suite does and does NOT establish, so the
final number isn't mistaken for a guarantee it can't back up.
"""
import sys, importlib.util
from pathlib import Path

import tier1_writetool
import tier2_edittool
import tier3_other_tools
import tier4_stack_and_gate


def load_agent(agent_path: Path):
    spec = importlib.util.spec_from_file_location("neon", str(agent_path))
    neon = importlib.util.module_from_spec(spec)
    sys.modules["neon"] = neon  # required: dataclass() introspects sys.modules[cls.__module__]
    spec.loader.exec_module(neon)
    return neon


def main():
    agent_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "neon_architect_v4_7_mobile_env.py"
    if not agent_path.exists():
        print(f"ERROR: agent file not found at {agent_path}")
        print("Usage: python3 run_all.py [path/to/neon_architect_v4_7_mobile_env.py]")
        sys.exit(1)

    print(f"Testing: {agent_path.resolve()}")
    import hashlib
    print(f"md5: {hashlib.md5(agent_path.read_bytes()).hexdigest()}\n")

    neon = load_agent(agent_path)

    all_fails = []
    all_pass = 0
    all_info = 0

    def make_check(bucket):
        def check(name, cond, detail="", informational=False):
            nonlocal all_pass, all_info
            tag = "INFO" if informational else ("PASS" if cond else "FAIL")
            print(f"[{tag}] {name}" + (f"  -- {detail}" if detail else ""))
            if informational:
                all_info += 1
            elif cond:
                all_pass += 1
            else:
                all_fails.append(f"{bucket}: {name}")
        return check

    print("=" * 70)
    print("TIER 1 — WriteTool")
    print("=" * 70)
    sandbox1 = Path(__file__).parent / "sandbox_tier1"
    if sandbox1.exists():
        import shutil; shutil.rmtree(sandbox1)
    sandbox1.mkdir()
    tier1_writetool.run(neon, sandbox1, make_check("Tier1/WriteTool"))

    print("\n" + "=" * 70)
    print("TIER 2 — EditTool")
    print("=" * 70)
    sandbox2 = Path(__file__).parent / "sandbox_tier2"
    if sandbox2.exists():
        import shutil; shutil.rmtree(sandbox2)
    sandbox2.mkdir()
    tier2_edittool.run(neon, sandbox2, make_check("Tier2/EditTool"))

    print("\n" + "=" * 70)
    print("TIER 3 — SearchTool / GlobTool / LsTool / RunTool")
    print("=" * 70)
    sandbox3 = Path(__file__).parent / "sandbox_tier3"
    if sandbox3.exists():
        import shutil; shutil.rmtree(sandbox3)
    sandbox3.mkdir()
    tier3_other_tools.run(neon, sandbox3, make_check("Tier3/OtherTools"), agent_path_for_subprocess=agent_path)

    print("\n" + "=" * 70)
    print("TIER 4 — Stack dispatch & phase-gate dedup")
    print("=" * 70)
    sandbox4 = Path(__file__).parent / "sandbox_tier4"
    if sandbox4.exists():
        import shutil; shutil.rmtree(sandbox4)
    sandbox4.mkdir()
    tier4_stack_and_gate.run(neon, sandbox4, make_check("Tier4/StackAndGate"))

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total = all_pass + len(all_fails)
    print(f"PASS: {all_pass}/{total}   FAIL: {len(all_fails)}   INFO (not scored): {all_info}")
    if all_fails:
        print("\nFailures:")
        for f in all_fails:
            print(" -", f)

    print("\n" + "=" * 70)
    print("SCOPE — what this suite covers and does NOT cover")
    print("=" * 70)
    print("""
COVERED (exercised directly, offline, no live credentials needed):
  - WriteTool: fence stripping, AST gate (overwrite + append), force=
    bypass, collapsed-source detection/repair, tiny/shrinking-overwrite
    guard, path sanitization/traversal, encoding edge cases, symlink
    escape.
  - EditTool: fence stripping, post-splice AST gate, ambiguous-match
    handling, replace_all correctness, encoding edge cases.
  - SearchTool / GlobTool / LsTool: path traversal, malformed input,
    ReDoS resistance.
  - RunTool: destructive git-operation allowlist (including casing and
    combined-short-flag bypass attempts), program allowlist enforcement.
  - Stack dispatch: every SpecializedAgent subclass checked for explicit
    branches on all 4 known stacks + a fail-loud fallback for unknown
    ones. GenerateAppTool's stack whitelist.
  - Phase-gate src_write dedup: logic verified as an isolated unit
    (see Tier 4 docstring for why — NeonArchitect can't be constructed
    without a live API key, confirmed by direct test).

NOT COVERED (out of reach for this suite, in this environment):
  - Anything requiring an actual NIM/LLM call: the real content
    _call_nim() would return for any agent prompt, live generation for
    any of the 4 stacks, ProviderPool rotation/cooldown behavior under
    real rate limits.
  - The full NeonArchitect interactive loop (autopilot, phase
    transitions, provider fallback, malformed-tool-call streak
    detection) — requires live credentials to construct at all.
  - GenerationOrchestrator end-to-end (scaffold -> agents -> repair ->
    devops -> test) — exercised piecewise via source inspection in
    Tier 4, not run as a live pipeline.
  - TaskTool, ModulesTool, TodoTool, WebSearchTool, BrowsePageTool,
    ProductionValidatorTool, DeployTool, LivePreviewManager — not
    covered by this pass at all; scope was prioritized toward the
    write-path and dispatch-path components with a proven history of
    defects. A future pass should extend here.
  - Concurrency: nothing in this suite exercises two tool calls racing
    against the same file simultaneously.
  - Anything below the Python/AST layer once a file is on disk — e.g.
    whether generated code that imports cleanly and parses correctly
    also behaves correctly at runtime for its actual business logic.

This suite increases confidence in the specific components and defect
classes it exercises. It does not, and cannot, establish a defect
probability for the codebase as a whole.
""")

    sys.exit(1 if all_fails else 0)


if __name__ == "__main__":
    main()
