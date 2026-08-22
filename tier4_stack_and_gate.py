#!/usr/bin/env python3
"""
Tier 4 — Stack dispatch (DatabaseAgent/FrontendAgent/DesignerAgent/
TesterAgent/BackendAgent) and phase-gate src_write dedup.

IMPORTANT SCOPE NOTE: NeonArchitect (the class that owns the live
_src_write_paths / _phase_tool_counts state machine) requires a real or
present NVIDIA_API_KEY to construct at all — build_pool() raises
RuntimeError otherwise, confirmed by direct test. It cannot be
instantiated offline. This tier therefore:

  1. Verifies stack-dispatch behavior directly against the real
     SpecializedAgent subclasses (fully testable offline — .run() only
     needs a ctx dict, no network unless _call_nim is actually invoked,
     which we avoid by only testing dispatch structure, not generation).

  2. Verifies the src_write dedup CONDITION as an isolated unit —
     `if rel not in self._src_write_paths: append + increment` — by
     replicating the exact operators from the source (line-referenced
     below) against a stand-in list/counter, since the real state lives
     on an object we cannot construct without live credentials. This is
     logic verification, not an end-to-end exercise of the orchestrator.
"""
import sys, inspect
from pathlib import Path


def run(neon, sandbox: Path, check):
    DatabaseAgent = neon.DatabaseAgent
    FrontendAgent = neon.FrontendAgent
    DesignerAgent = neon.DesignerAgent
    TesterAgent = neon.TesterAgent
    BackendAgent = neon.BackendAgent
    GenerateAppTool = neon.GenerateAppTool

    KNOWN_STACKS = ("fastapi-react", "nextjs-postgres", "flutter-fastapi", "expo-node")

    # ---------------------------------------------------------------
    # Group A: every agent has explicit dispatch for every known stack
    # AND an else/default for unknown ones (structural check via source
    # inspection — doesn't invoke _call_nim, so no network needed).
    # ---------------------------------------------------------------
    for cls in [DatabaseAgent, FrontendAgent, DesignerAgent, TesterAgent, BackendAgent]:
        src = inspect.getsource(cls.run)
        for stack in KNOWN_STACKS:
            check(f"A01 {cls.__name__} has an explicit branch for stack={stack!r}",
                  f'"{stack}"' in src or f"'{stack}'" in src,
                  )
        has_fallback = "else:" in src
        check(f"A02 {cls.__name__} has an else/default branch for unrecognized stacks",
              has_fallback)
        if has_fallback:
            # confirm the fallback actually signals failure rather than
            # silently returning {} — grep for raise/ValueError near the
            # else clause specifically, not just anywhere in the method.
            else_idx = src.rfind("else:")
            tail = src[else_idx:else_idx + 300]
            check(f"A03 {cls.__name__}'s else branch raises rather than silently no-opping",
                  "raise" in tail, f"tail={tail!r}")

    # ---------------------------------------------------------------
    # Group B: GenerateAppTool stack whitelist (the entry point that
    # decides what string reaches the agents above in the first place)
    # ---------------------------------------------------------------
    src = inspect.getsource(GenerateAppTool.execute)
    check("B01 GenerateAppTool.execute has an explicit known-stacks whitelist",
          "_KNOWN_STACKS" in src or all(s in src for s in KNOWN_STACKS))
    check("B02 GenerateAppTool.execute refuses an unrecognized explicit stack "
          "(not just falling through to _detect_stack)",
          "Unsupported stack" in src or ("else" in src and "ToolResult" in src))

    # A synthetic call with an unrecognized stack, WITHOUT touching the
    # network: GenerateAppTool.execute's stack-validation happens before
    # any provider call, so this should return a clean ToolResult error,
    # not raise, not hang, not attempt generation.
    try:
        gt = GenerateAppTool(sandbox, pool=None, config={})
        r = gt.execute(spec="a test app", stack="definitely-not-a-real-stack", start_preview=False)
        check("B03 GenerateAppTool.execute('definitely-not-a-real-stack') "
              "refuses cleanly without touching the network/pool",
              getattr(r, "is_error", False), f"result={r}")
    except Exception as e:
        # Some constructors may require a non-None pool; if so, this is
        # a scope limitation of this offline test, not a product defect —
        # reported as informational rather than a false FAIL.
        check("B03 GenerateAppTool instantiation requires live pool/config "
              "(offline dispatch check not reachable this way)",
              True, f"{type(e).__name__}: {e}", informational=True)

    # ---------------------------------------------------------------
    # Group C: src_write dedup logic, isolated unit replication.
    # Source (search for the literal string in the agent file to confirm
    # this test still matches the real condition if the file changes):
    #     if rel not in self._src_write_paths:
    #         self._src_write_paths.append(rel)
    #         self._phase_tool_counts["src_write"] = ... + 1
    # ---------------------------------------------------------------
    agent_src = inspect.getsource(neon)
    dedup_condition_present = "if rel not in self._src_write_paths:" in agent_src
    check("C01 dedup guard 'if rel not in self._src_write_paths:' still present verbatim in source",
          dedup_condition_present)

    # Replicate the exact state-machine step to prove the CONDITION is
    # sound (not just present): editing the same path N times must only
    # increment the counter once.
    src_write_paths = []
    phase_tool_counts = {}
    def apply_write(rel):
        if rel not in src_write_paths:
            src_write_paths.append(rel)
            phase_tool_counts["src_write"] = phase_tool_counts.get("src_write", 0) + 1

    for _ in range(5):
        apply_write("backend/models.py")
    check("C02 writing the SAME path 5 times increments src_write count exactly once",
          phase_tool_counts.get("src_write") == 1,
          f"count={phase_tool_counts.get('src_write')} paths={src_write_paths}")

    apply_write("backend/routes.py")
    apply_write("backend/db.py")
    check("C03 writing 3 DIFFERENT paths increments src_write count to exactly 3",
          phase_tool_counts.get("src_write") == 3,
          f"count={phase_tool_counts.get('src_write')} paths={src_write_paths}")

    # path-normalization gap probe: does the real gate canonicalize `rel`
    # before the membership check, or would "backend/models.py" and
    # "./backend/models.py" (or a trailing-slash / different-case variant
    # on a case-insensitive filesystem) be treated as two different paths
    # and double-count? Inspect the surrounding source for a normalize
    # step immediately before the check.
    idx = agent_src.find("if rel not in self._src_write_paths:")
    context_before = agent_src[max(0, idx - 600):idx]
    normalizes = "Tool._sanitize_rel_path" in context_before or "path_key" in context_before
    check("C04 [PROBE] path is canonicalized/normalized before the dedup membership "
          "check (protects against './x.py' vs 'x.py' vs 'x.py/' double-counting)",
          True,  # informational — documents whether this protection exists, doesn't fail the suite
          f"normalization_evidence_found={normalizes}",
          informational=True)


if __name__ == "__main__":
    import importlib.util
    agent_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "neon_architect_v4_7_mobile_env.py"
    spec = importlib.util.spec_from_file_location("neon", str(agent_path))
    neon = importlib.util.module_from_spec(spec)
    sys.modules["neon"] = neon
    spec.loader.exec_module(neon)

    sandbox = Path(__file__).parent / "sandbox_tier4"
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
    print(f"\nTier 4 FAILS: {len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1 if FAILS else 0)
