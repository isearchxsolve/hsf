# Hardened adversarial suite — Neon Architect

## Run it

```bash
python3 run_all.py [path/to/neon_architect_v4_7_mobile_env.py]
```

Defaults to the copy of the agent sitting next to the script (md5
`09247f155ba0e8825fa8a2c55179b310`, the version from
`files-33ad7e12__1_.zip`). Stdlib only, no dependencies to install.
Takes under 2 minutes; one test runs a ReDoS probe out-of-process with
an 8s hard timeout so a genuine hang there can't take the rest of the
suite down with it.

## Structure

- `tier1_writetool.py` — WriteTool: fence stripping, AST gate
  (overwrite + append), force= bypass, collapsed-source
  detection/repair, tiny/shrinking-overwrite guard, path
  sanitization/traversal, encoding edge cases, symlink escape.
- `tier2_edittool.py` — EditTool: fence stripping, post-splice AST
  gate, ambiguous-match handling, replace_all correctness, encoding
  edge cases.
- `tier3_other_tools.py` — SearchTool, GlobTool, LsTool, RunTool:
  traversal, ReDoS resistance, destructive-git-operation allowlist
  (casing/combined-flag bypass attempts), program allowlist.
- `tier4_stack_and_gate.py` — Stack dispatch across all
  SpecializedAgent subclasses, GenerateAppTool's stack whitelist,
  phase-gate src_write dedup logic.
- `run_all.py` — runs all four tiers, prints a combined PASS/FAIL/INFO
  count and an explicit SCOPE section.

Each tier file is also independently runnable:
`python3 tier1_writetool.py [agent_path]`, etc.

## Last run result

**PASS: 111/115 · FAIL: 4 · INFO (not scored): 3**

### The 4 failures

1. **Tier1/E01 & Tier2/D01 — invalid-UTF-8-byte handling.**
   `WriteTool` validates an `errors="replace"`-decoded in-memory copy
   of the file for its append-AST-gate, then writes with strict
   `utf-8` — so an existing invalid byte survives untouched on disk
   while the tool reports success, and a real `python3` interpreter
   can't decode the file at all (`UnicodeDecodeError`, not
   `SyntaxError`). `EditTool` has the opposite problem: it reads with
   `errors="replace"` and writes back what it read, silently replacing
   the invalid byte with `U+FFFD` — different failure mode (silent
   data mangling vs. byte preserved-but-broken), same root cause
   (neither tool round-trips raw bytes faithfully).

2. **Tier4/A01 & A03 — `DesignerAgent` still silently no-ops for
   `expo-node`.** The round-2 patch added explicit branches + a
   fail-loud `else: raise ValueError(...)` to `DatabaseAgent`,
   `FrontendAgent`, `TesterAgent`, and `BackendAgent`. `DesignerAgent`
   was the one agent that already had *some* fallback before that
   patch, so it wasn't touched — but its fallback is
   `else: return {}  # expo-node: minimal theme`, which is exactly the
   silent-no-op pattern the whole patch series exists to close. Two
   distinct consequences: `expo-node` projects never get a theme file
   from this agent, and any other unrecognized stack string reaching
   it fails silently instead of raising.

### The 3 informational findings (not counted as failures, but real)

- **Tier1/D03 & Tier3-equivalent — AST-validation ceiling.**
  `ast.parse()` can only reject syntax errors, never
  semantically-nonsense-but-valid code. `"this is not python\n"`
  appended to a valid file parses fine (it's a legal chained `is not`
  comparison between two undefined names) and crashes at runtime with
  `NameError`. Not fixable by tightening the AST gate — would need a
  different validation strategy (actual execution, a linter/type
  checker) to catch.

- **Tier3/A03 — SearchTool has no ReDoS protection.**
  `re.compile(pattern, re.IGNORECASE)` runs directly against a
  caller-supplied `pattern` argument with no timeout and no
  complexity/backtracking guard. Confirmed independently at the `re`
  module level (deterministic across 3 runs): `(a+)+$` against 35
  `a`'s followed by a non-matching character hangs indefinitely via
  catastrophic backtracking. This test is reported as PASS/did-not-hang
  because it runs the probe in a subprocess with a hard timeout so the
  *test suite* survives — the underlying tool has no such protection
  and would hang on this input for real. Read the FAIL/PASS tag here
  as "did the test harness survive," not "is SearchTool safe" — the
  detail string spells out the actual finding either way.

- **Tier4/C04 — path-normalization probe.**
  No evidence found that the `src_write` dedup membership check
  canonicalizes the path before comparing (e.g. `./x.py` vs `x.py`
  could theoretically double-count). Flagged for awareness; not
  independently confirmed as exploitable since it wasn't reachable
  without constructing the live `NeonArchitect` object (see below).

## Scope — read this before treating "111/115" as a grade

This suite exercises the write-path tools and the stack-dispatch layer
in depth, offline, with no live API credentials. It does **not** cover:

- Anything requiring a real NIM/LLM call (actual generated file
  content, live generation for any stack, provider rotation/cooldown
  under real rate limits).
- The full `NeonArchitect` interactive loop — confirmed by direct test
  that it cannot be constructed without a live `NVIDIA_API_KEY`
  (`build_pool()` raises `RuntimeError` otherwise). Tier 4's
  phase-gate coverage is logic-level verification against the real
  source condition, not an end-to-end exercise of the live object.
- `GenerationOrchestrator` end-to-end as a running pipeline (checked
  piecewise via source inspection instead).
- `TaskTool`, `ModulesTool`, `TodoTool`, `WebSearchTool`,
  `BrowsePageTool`, `ProductionValidatorTool`, `DeployTool`,
  `LivePreviewManager` — not touched by this pass at all.
- Concurrency — no test exercises two tool calls racing the same file.
- Runtime correctness of generated code once it's syntactically valid
  and importable.

**Bottom line: this is evidence, not a certificate.** It increases
confidence in the specific components and defect classes it tests. It
does not and cannot establish a defect probability for the codebase as
a whole — see the verdict in the accompanying chat message for why
that number was never achievable in the first place.
