#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  NEON ARCHITECT v4.6  —  Monolithic Coding Agent for NVIDIA NIM              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Backend:   NVIDIA NIM  →  GLM-5.2 (primary) | MiniMax M3 / Laguna XS (fallback)║
║  Design:    Warm terminal UI · Zero Bottlenecks · Self-Healing Streams       ║
║  Features:  Auto-Nudge · XML Interceptor · Smart Compaction · Token Bucket   ║
╚══════════════════════════════════════════════════════════════════════════════╝

Usage:
    python neon_architect.py                    # interactive mode
    python neon_architect.py --project ./myapp  # set project dir
    python neon_architect.py --model glm-5.2    # pick model

Commands (inside agent):
    /exit          quit
    /clear         wipe conversation history
    /compact       force context compaction
    /files         show project file tree
    /cost          show token usage stats
    /model <name>  switch model
    /todo          show task tracker
    <Enter>        continue / proceed with last task
"""

from __future__ import annotations

import argparse
import ast
import copy
import difflib
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import concurrent.futures
import queue
import platform
from collections import deque
import random
import atexit

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    import openai
    OPENAI_ERRORS = (
        openai.APITimeoutError,
        openai.APIConnectionError,
        openai.InternalServerError,
        openai.RateLimitError,
        openai.APIStatusError,
    )
except ImportError:
    OPENAI_ERRORS = (Exception,)
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from enum import Enum
from collections import defaultdict

# Generation layer sentinel — set to True at end of file once Section 10 is parsed
_GENERATION_LAYER_READY: bool = False

def verify_nim(base_url: str, api_key: str) -> bool:
    try:
        r = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


# ── OpenAI client ─────────────────────────────────────────────────────────────
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    OpenAI = None  # type: ignore

# ── Rich TUI ──────────────────────────────────────────────────────────────────
try:
    from rich.console import Console, Group, RenderableType
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.rule import Rule
    from rich.syntax import Syntax
    from rich.markdown import Markdown
    from rich.tree import Tree
    from rich.align import Align
    from rich.padding import Padding
    from rich.theme import Theme
    from rich.box import ROUNDED, MINIMAL, SIMPLE_HEAD, DOUBLE_EDGE, HEAVY_EDGE, ASCII
    from rich.live import Live
    from rich.progress import (
        Progress, SpinnerColumn, TextColumn,
        BarColumn, TimeElapsedColumn
    )
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    Console = None  # type: ignore

# ── Windows console fix & legacy detection ──────────────────────────────────
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

LEGACY_WIN_CONSOLE = (
    sys.platform == "win32"
    and not os.environ.get("WT_SESSION")           # Not Windows Terminal
    and not os.environ.get("ANSICON")              # Not ANSICON
    and os.environ.get("ConEmuANSI") != "ON"         # Not ConEmu
    and not os.environ.get("TERM_PROGRAM")           # Not VS Code / Hyper / etc.
)
if LEGACY_WIN_CONSOLE:
    # Force plain ASCII on legacy cmd.exe to prevent □□□ garbage
    os.environ["PYTHONIOENCODING"] = "utf-8"

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 1: CONSTANTS & CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

APP_NAME = "NEON ARCHITECT"
APP_VERSION = "4.6.4"
APP_TAGLINE = "GLM-5.2 · NVIDIA NIM · Sleek · Zero Bottlenecks"

CONFIG_DIR = Path.home() / ".neon_architect"
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSIONS_DIR = CONFIG_DIR / "sessions"
HISTORY_FILE = CONFIG_DIR / "history.jsonl"
STATE_VERSION = 2  # bump when session schema changes


def _project_id(project_dir: Path) -> str:
    """Stable short id for a project directory."""
    import hashlib
    key = str(project_dir.resolve()).encode("utf-8", errors="replace")
    return hashlib.sha256(key).hexdigest()[:16]


def _project_session_path(project_dir: Path) -> Path:
    """Stable per-project session file (survives relaunch)."""
    return SESSIONS_DIR / f"project_{_project_id(project_dir)}.json"


def _project_key_path(project_dir: Path) -> Path:
    """Per-project API key file — avoids throttling one global NVIDIA account."""
    return SESSIONS_DIR / f"project_{_project_id(project_dir)}.key.json"


def load_project_api_key(project_dir: Path) -> str:
    data = _safe_json_load(_project_key_path(project_dir))
    if data and isinstance(data.get("api_key"), str):
        return data["api_key"].strip()
    return ""


def load_dead_models(project_dir: Path) -> List[str]:
    """Model ids that returned a permanent-miss status (404 Not Found or
    410 Gone — not enabled on this account's key, or retired/renamed on
    the provider's end) in a past session. Persisted so every fresh
    launch/pool-rebuild doesn't have to re-discover the same unavailable
    models via live errors before falling through to a working provider —
    this was previously pure wasted wall-clock + retry budget on every
    single turn/session.
    """
    data = _safe_json_load(_project_key_path(project_dir))
    if data and isinstance(data.get("dead_models"), list):
        return [m for m in data["dead_models"] if isinstance(m, str)]
    return []


def mark_model_dead(project_dir: Path, model_id: str) -> None:
    """Record a permanently-missing (404/410) model id so future sessions skip it at pool-build time."""
    try:
        path = _project_key_path(project_dir)
        data = _safe_json_load(path) or {}
        dead = set(data.get("dead_models") or [])
        if model_id in dead:
            return
        dead.add(model_id)
        data["dead_models"] = sorted(dead)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)
    except Exception:
        pass  # best-effort; never let bookkeeping break the retry loop


def save_project_api_key(project_dir: Path, api_key: str) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(SESSIONS_DIR, 0o700)
    except Exception:
        pass  # best-effort; e.g. unsupported on some filesystems
    path = _project_key_path(project_dir)
    cleaned = api_key.strip().strip('"').strip("'").replace("\n", "").replace("\r", "")
    payload = {
        "project": str(Path(project_dir).resolve()),
        "api_key": cleaned,
        "updated": datetime.now().isoformat(),
    }
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass  # best-effort; e.g. unsupported on some filesystems
    tmp.replace(path)
    return path


def _safe_json_load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None

NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"

# ── Model Registry ────────────────────────────────────────────────────────────
MODELS: Dict[str, Dict[str, Any]] = {
    "glm-5.2": {
        "id": "z-ai/glm-5.2",
        "name": "GLM-5.2",
        "provider": "NVIDIA NIM",
        "rpm": 40,
        # Was 8192. GLM-5.2 defaults to Think-Max reasoning and can produce
        # long chains of thought before the tool-call/content tokens even
        # start; Together's own quickstart warns to "set max_tokens
        # generously" when thinking is on, or the reasoning can eat the
        # whole budget and truncate the actual tool call mid-JSON.
        "max_tokens": 32768,
        "ctx_window": 131072,
        "thinking": False,
        "extra_body": None,
        "temperature": None,
        "top_p": None,
    },
    "glm-5.2-fp8": {
        "id": "z-ai/glm-5.2-fp8",
        "name": "GLM-5.2-FP8",
        "provider": "NVIDIA NIM",
        "rpm": 40,
        "max_tokens": 32768,
        "ctx_window": 131072,
        "thinking": False,
        "extra_body": None,
        "temperature": None,
        "top_p": None,
    },
    "minimax-m3": {
        "id": "minimaxai/minimax-m3",
        "name": "MiniMax M3",
        "provider": "NVIDIA NIM",
        "rpm": 40,
        # MoE, 1M context, native tool-calling/agent support per NVIDIA's
        # own deployment docs — used here as a fast, large-context fallback
        # behind GLM-5.2/GLM-5.2-FP8.
        "max_tokens": 16384,
        "ctx_window": 1000000,
        "thinking": False,
        "extra_body": None,
        "temperature": 1.0,
        "top_p": 0.95,
    },
    "laguna-xs-2.1": {
        "id": "poolside/laguna-xs-2.1",
        "name": "Laguna XS 2.1",
        "provider": "NVIDIA NIM",
        "rpm": 40,
        # Poolside's 33B-total/3B-active MoE, purpose-built for agentic
        # coding/tool-use/long-horizon work, 262K context, native
        # interleaved reasoning + tool calls.
        "max_tokens": 16384,
        "ctx_window": 262144,
        "thinking": True,
        "extra_body": None,
        "temperature": 0.3,
        "top_p": 0.95,
    },
    "kimi-k2-thinking": {
        "id": "moonshotai/kimi-k2-thinking",
        "name": "Kimi K2 Thinking",
        "provider": "NVIDIA NIM",
        "rpm": 40,
        # Moonshot's 1T-total/32B-active MoE, confirmed on NVIDIA NIM's
        # catalog (LLM APIs section, docs.api.nvidia.com/nim/reference/
        # moonshotai-kimi-k2-thinking). Native INT4, 256K context, and
        # its own tool-call parsing pipeline explicitly documented to
        # stay coherent across 200-300 consecutive tool calls without
        # drift — the single most agentic-tool-use-focused model in this
        # fallback chain. NVIDIA's model card recommends temperature=1.0.
        # Always runs in thinking mode (no non-thinking variant).
        "max_tokens": 16384,
        "ctx_window": 262144,
        "thinking": True,
        "extra_body": None,
        "temperature": 1.0,
        "top_p": None,
    },
}

DEFAULT_CONFIG = {
    "version": APP_VERSION,
    "api_key": "",
    "default_model": "glm-5.2",
    "default_persona": "engineer",
    "base_url": NIM_BASE_URL,
    "project_dir": str(Path.cwd()),
    "temperature": 0.2,
    "max_rounds": 0,
    "compact_threshold": 5000,
    "auto_nudge": True,
    "max_nudges": 2,
    "stream_timeout": 420.0,
    "request_timeout": 420.0,
    "rpm_safety": 0.80,
    "show_thinking": True,
    "theme": "neon",
    "retry_max": 8,
    "retry_base_delay": 2.5,
    "enable_openrouter": True,
    "stream_content_timeout": 200.0,
    "first_token_timeout": 60.0,
    "http_read_timeout": 180.0,
    "http_connect_timeout": 15.0,
    "max_retries_per_provider": 3,
    "nim_shared_rpm": 38.0,
    "thinking_effort": "low",
    "allow_medium_high_thinking": False,
    "first_token_timeout_off": 60.0,
    "first_token_timeout_low": 150.0,
    "first_token_timeout_medium": 240.0,
    "first_token_timeout_high": 360.0,
    "stream_content_timeout_off": 200.0,
    "stream_content_timeout_low": 200.0,
    "stream_content_timeout_medium": 220.0,
    "stream_content_timeout_high": 300.0,
    "empty_pool_max_wait": 120.0,
    "inter_turn_delay": 0.35,
    "post_429_backoff": 25.0,
    "max_autopilot_rounds": 0,
    "max_phase_rounds": 150,
    "testing_max_phase_rounds": 150,
    "verification_max_phase_rounds": 150,
    "min_tests_passed": 1,
    "min_coverage_pct": 0,
    "max_same_tool_successes": 3,
    "startup_compact_tokens": 60000,
    "autopilot_thinking": False,
    "plan_artifact": "PLAN.md",
    "architecture_artifact": "ARCHITECTURE.md",
    "working_memory_file": ".neon_working_memory.md",
    "project_state_file": ".neon_project_state.json",
    "max_same_tool_failures": 3,
    # Per-phase model override. Autopilot runs a single model for the whole
    # SDLC by default (default_model); this lets specific phases opt into a
    # stronger/different model without paying for it everywhere. Empty/absent
    # phase -> falls back to default_model. Keys must be SDLC_PHASES entries,
    # values must be MODELS keys. testing/verification default to MiniMax M3
    # (previously the 120B Nemotron, now removed) since that's where GLM-5.2
    # was observed getting stuck producing collapsed-source writes it
    # couldn't self-correct.
    "phase_models": {
        "testing": "minimax-m3",
        "verification": "minimax-m3",
    },
    "require_research_for_external": True,
    "full_access": True,
    "allow_path_outside_project": False,
    "allow_local_network": True,
    "bash_confirm_destructive": False,
    "bash_timeout_max": 600,
    "implementation_path_prefixes": ["src/", "app/", "backend/", "frontend/", "lib/", "tests/"],
    "min_implementation_src_writes": 4,
    "session_restore_max_messages": 24,
    "session_restore_max_tokens": 80000,
    "tdd_enforce_red_green": True,
    "git_worktree_on_autopilot": True,
}

def normalize_thinking_policy(cfg: Dict[str, Any]) -> Dict[str, Any]:
    allow = bool(cfg.get("allow_medium_high_thinking", False))
    effort = str(cfg.get("thinking_effort") or "off").lower().strip()
    if effort in ("high", "medium") and not allow:
        effort = "off"
    if effort not in ("off", "low", "medium", "high"):
        effort = "off"
    cfg["thinking_effort"] = effort
    cfg["show_thinking"] = bool(cfg.get("show_thinking", False)) and effort != "off"
    cfg["autopilot_thinking"] = False

    ft_key = f"first_token_timeout_{effort}"
    ct_key = f"stream_content_timeout_{effort}"
    defaults = {
        "off": (60.0, 200.0),
        "low": (150.0, 200.0),
        "medium": (240.0, 220.0),
        "high": (360.0, 300.0),
    }
    ft_def, ct_def = defaults.get(effort, (60.0, 200.0))
    ft = float(cfg.get(ft_key) or cfg.get("first_token_timeout") or ft_def)
    ct = float(cfg.get(ct_key) or cfg.get("stream_content_timeout") or ct_def)
    floors = {"off": 60.0, "low": 150.0, "medium": 240.0, "high": 360.0}
    cfg["first_token_timeout"] = max(floors.get(effort, 60.0), ft)
    cfg["stream_content_timeout"] = max(ct_def, ct)
    cfg["stream_timeout"] = max(
        float(cfg.get("stream_timeout") or 420.0),
        cfg["first_token_timeout"] + 60.0,
    )
    cfg["request_timeout"] = max(
        float(cfg.get("request_timeout") or 420.0),
        cfg["stream_timeout"],
    )
    return cfg


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 1b: PERSONAS & SDLC DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════════

PERSONAS: Dict[str, Dict[str, Any]] = {
    "adaptive": {
        "name": "Adaptive",
        "description": "Automatically switches based on task keywords",
        "prompt": "You are an adaptive agent. Analyze the user request and adopt the most appropriate professional role to solve the task efficiently.",
        "auto_switch": True,
    },
    "architect": {
        "name": "Architect",
        "description": "System design, APIs, data models, structure",
        "prompt": "You are a senior software architect. Focus on system design, APIs, data models, and overall structure. Think about scalability, maintainability, and clean separation of concerns. Create diagrams and interfaces before implementation.",
        "auto_switch": False,
    },
    "designer": {
        "name": "Designer",
        "description": "UI/UX, components, styling, accessibility",
        "prompt": "You are a UI/UX designer and frontend engineer. Focus on user experience, component design, styling, accessibility, and responsive layouts. Think about color theory, typography, and interaction patterns.",
        "auto_switch": False,
    },
    "engineer": {
        "name": "Engineer",
        "description": "Implementation, algorithms, business logic",
        "prompt": "You are a senior software engineer. Focus on clean implementation, algorithms, business logic, and code quality. Write well-documented, tested code. Follow SOLID principles and best practices.",
        "auto_switch": False,
    },
    "tester": {
        "name": "Tester",
        "description": "TDD, test writing, validation, coverage",
        "prompt": "You are a QA engineer and test specialist. Focus on test-driven development, writing comprehensive tests, edge case coverage, and validation. Ensure all code paths are tested. Use mocking and fixtures appropriately.",
        "auto_switch": False,
    },
    "debugger": {
        "name": "Debugger",
        "description": "Bug diagnosis, root cause analysis, fixes",
        "prompt": "You are a debugging specialist. Focus on systematic bug diagnosis, root cause analysis, and precise fixes. Use logging, breakpoints, and tracing. Think about race conditions, memory leaks, and edge cases.",
        "auto_switch": False,
    },
    "reviewer": {
        "name": "Reviewer",
        "description": "Code quality, security, performance audit",
        "prompt": "You are a code reviewer and security auditor. Focus on code quality, security vulnerabilities, performance bottlenecks, and best practices. Check for OWASP issues, SQL injection, XSS, and inefficient algorithms.",
        "auto_switch": False,
    },
    "devops": {
        "name": "DevOps",
        "description": "Deployment, CI/CD, infrastructure, Docker",
        "prompt": "You are a DevOps engineer. Focus on deployment pipelines, CI/CD, Docker, Kubernetes, infrastructure as code, monitoring, and logging. Ensure systems are observable, scalable, and resilient.",
        "auto_switch": False,
    },
}

PERSONA_TRIGGERS: Dict[str, List[str]] = {
    "architect": ["design", "architecture", "system", "api", "structure", "model", "schema", "interface"],
    "designer": ["ui", "ux", "frontend", "css", "html", "component", "layout", "style", "responsive", "theme"],
    "engineer": ["implement", "code", "function", "algorithm", "logic", "backend", "feature", "build"],
    "tester": ["test", "testing", "tdd", "coverage", "pytest", "unit test", "integration test", "validation"],
    "debugger": ["bug", "fix", "debug", "error", "crash", "issue", "broken", "fail", "traceback"],
    "reviewer": ["review", "audit", "security", "quality", "refactor", "clean up", "optimize", "performance"],
    "devops": ["deploy", "docker", "ci/cd", "pipeline", "kubernetes", "k8s", "infrastructure", "server"],
}

SDLC_PHASES: List[str] = [
    "planning",
    "architecture",
    "design",
    "implementation",
    "testing",
    "review",
    "deployment",
    "verification",
]

SDLC_PHASE_PROMPTS: Dict[str, str] = {
    "planning": (
        "Research the goal (web_search + browse_page if external product). "
        "Write PLAN.md with exact headings Requirements, Risks, Acceptance Criteria. "
        "Add >=5 todos via todo tool with content field each time "
        '(example: action=add content="Draft API surface for NIM chat completions"). '
        "Do NOT implement app code yet."
    ),
    "architecture": (
        "Follow PLAN.md. Write ARCHITECTURE.md (components, interfaces, data flow, tech stack). "
        "Define production behavior and failure handling, not interface stubs. Do not mark this phase complete "
        "until the implementation plan names concrete modules, persistence boundaries, and executable checks."
    ),
    "design": "Follow PLAN.md and ARCHITECTURE.md. Design UI/UX, components, styles, layouts.",
    "implementation": (
        "Follow PLAN.md and ARCHITECTURE.md. Implement REAL application code under "
        "src/, app/, backend/, frontend/, lib/, or tests/ (not just PLAN.md). "
        "TDD: write meaningful tests, run the project's real test/build/lint commands, and fix every failure. "
        "Do not create empty files, TODO-only functions, fake success responses, or demo-only placeholders. "
        "Track todos. Use validate_project before claiming implementation is complete."
    ),
    "testing": (
        "Test against PLAN.md acceptance criteria. Run validate_project and the project's real test/build commands. "
        "Fix failures, missing integrations, placeholder implementations, and startup errors. No new features."
    ),
    "review": "Review against PLAN.md and ARCHITECTURE.md for quality, security, consistency.",
    "deployment": "Create deployment artifacts aligned with ARCHITECTURE.md.",
    "verification": (
        "Verify every PLAN.md acceptance criterion with executable evidence. Run validate_project, confirm the "
        "application's build/start path works, and mark todos done only when criteria pass. Never certify a scaffold."
    ),
}

SDLC_PERSONA_MAP: Dict[str, str] = {
    "planning": "architect",
    "architecture": "architect",
    "design": "designer",
    "implementation": "engineer",
    "testing": "tester",
    "review": "reviewer",
    "deployment": "devops",
    "verification": "tester",
}

SDLC_PHASE_REQUIREMENTS: Dict[str, Dict[str, Any]] = {
    "planning": {
        "kind": "plan_complete",
        "artifact": "PLAN.md",
        "min_chars": 400,
        "min_todos": 5,
        "require_sections": ["Acceptance Criteria", "Requirements", "Risks"],
        "require_research": True,
    },
    "architecture": {
        "kind": "architecture_complete",
        "artifact": "ARCHITECTURE.md",
        "min_chars": 300,
        "any_tool": ["write", "edit"],
        "min_calls": 0,
        "require_sections": ["Components", "Data Flow"],
    },
    "design": {
        "kind": "tool_calls",
        "any_tool": ["write", "edit"],
        "min_calls": 3,
        "min_chars_written": 300,
    },
    "implementation": {
        "kind": "implementation_complete",
        "any_tool": ["write", "edit"],
        "min_calls": 8,
        "min_src_writes": 4,
        "require_validation": True,
    },
    "testing": {
        "kind": "bash_success",
        "require_test_cmd": True,
        "require_validation": True,
    },
    "review": {
        "kind": "tool_calls",
        "any_tool": ["read", "edit", "search"],
        "min_calls": 3,
    },
    "deployment": {
        "kind": "deployment_complete",
        "artifact": "docker-compose.yml",
        "min_chars": 500,
        "require_sections": ["frontend", "backend", "database", "services"],
    },
    "verification": {
        "kind": "product_validation",
        "require_test_cmd": True,
        "require_validation": True,
        "artifact": "VERIFICATION.md",
        "product_checks": [
            "docker-compose.yml",
            "Dockerfile",
            "frontend",
            "backend",
            "live_preview",
            "project_persistence",
        ],
    },
}


PLAN_REQUIRED_HEADERS = ("acceptance criteria", "requirements", "risks")
ARCH_REQUIRED_HEADERS = ("components", "data flow")

DESTRUCTIVE_BASH_RE = re.compile(
    r"(?:^|[\s;|&])(rm\s+-[a-zA-Z]*[fr]|rm\s+--recursive|del\s+/[sf]|format\s+|"
    r"git\s+push\s+.*--force|git\s+reset\s+--hard|dd\s+if=|mkfs\.|shutdown|reboot)\b",
    re.I,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 2: TERMINAL THEME & UI SYSTEM  —  Aurora Glass
#  A card-based, full-width, gutter-free presentation modeled after modern
#  agent CLIs (OpenCode / Crush / Claude Code): wide breathing room, real
#  panels instead of hand-drawn box characters, a persistent status rail,
#  and zero raw stdout writes — every pixel goes through Rich so wrapping,
#  color, and padding are always in sync with the live terminal width.
# ═══════════════════════════════════════════════════════════════════════════════

if HAS_RICH:
    THEME = Theme({
        # ── Brand ramp (violet → indigo → cyan) ────────────────────────────
        "brand":            "bold #a78bfa",
        "brand_dim":        "#7c6fce",
        "brand_soft":       "#c4b5fd",
        "cc_primary":       "#38bdf8",
        "cc_primary_bold":  "bold #38bdf8",
        "cc_shimmer":       "#7dd3fc",
        "cc_accent":        "bold #f472b6",
        "cc_accent_soft":   "#f9a8d4",

        # ── Roles ────────────────────────────────────────────────────────
        "cc_tool":          "bold #fb923c",
        "cc_tool_dim":      "#c2740f",
        "cc_permission":    "bold #c4b5fd",
        "cc_success":       "bold #4ade80",
        "cc_success_dim":   "#22c55e",
        "cc_warn":          "bold #facc15",
        "cc_warn_dim":      "#ca8a04",
        "cc_error":         "bold #fb7185",
        "cc_error_dim":     "#e11d48",
        "cc_muted":         "#94a3b8",
        "cc_subtle":        "#64748b",
        "cc_faint":         "#475569",
        "cc_fg":            "bold #f1f5f9",
        "cc_body":          "#e2e8f0",
        "cc_user":          "bold #f8fafc",
        "cc_surface":       "#1e293b",
        "cc_thinking":      "italic #7dd3fc",
        "cc_thinking_dim":  "italic #64748b",
        "cc_prompt":        "bold #38bdf8",
        "cc_status":        "#94a3b8",
        "cc_card_border":   "#334155",
        "cc_card_border_hi":"#475569",

        # ── Compatibility aliases (older call-sites keep working) ─────────
        "neon_cyan":        "bold #38bdf8",
        "neon_magenta":     "bold #f472b6",
        "neon_green":       "bold #4ade80",
        "neon_yellow":      "bold #facc15",
        "neon_red":         "bold #fb7185",
        "neon_blue":        "bold #818cf8",
        "neon_white":       "bold #f8fafc",
        "neon_dim":         "#94a3b8",
        "neon_prompt":      "bold #38bdf8",
        "neon_accent":      "bold #f472b6",
        "neon_success":     "bold #4ade80",
        "neon_warning":     "bold #facc15",
        "neon_error":       "bold #fb7185",
        "neon_info":        "bold #818cf8",
        "user":             "bold #f8fafc",
        "assistant":        "#e2e8f0",
        "thinking":         "italic #7dd3fc",
        "tool_name":        "bold #fb923c",
        "tool_args":        "#c2740f",
        "tool_result":      "#94a3b8",
        "dim_cyan":         "#38bdf8",
        "dim_white":        "#94a3b8",
        "status_ok":        "bold #4ade80",
        "status_warn":      "bold #facc15",
        "status_err":       "bold #fb7185",
    })

    def _detect_width() -> int:
        """Full terminal width, never artificially narrowed. Floors at 100
        so the card layout never collapses into a single cramped column,
        but otherwise always tracks the live terminal."""
        try:
            import shutil as _shutil_ui
            cols = int(_shutil_ui.get_terminal_size(fallback=(112, 32)).columns)
        except Exception:
            cols = 112
        return max(100, cols)

    console = Console(
        theme=THEME,
        highlight=False,
        soft_wrap=False,          # Rich owns wrapping — no raw stdout writes
        color_system="truecolor",
        force_terminal=True,
        legacy_windows=False,
        width=_detect_width(),
    )

    def _resync_width() -> None:
        """Call before any card render so a mid-session terminal resize is
        always reflected — panels never render stale-narrow."""
        try:
            console.width = _detect_width()
        except Exception:
            pass
else:
    console = None

    def _resync_width() -> None:
        pass

_CC_VERBS = [
    "Thinking", "Cooking", "Percolating", "Ruminating", "Cogitating",
    "Ideating", "Kneading", "Sautéing", "Pondering", "Brewing",
    "Noodling", "Whirring", "Mulling", "Spinning", "Crunching",
    "Synthesizing", "Harmonizing", "Channeling", "Orchestrating",
    "Conjuring", "Distilling", "Weaving", "Sculpting", "Composing",
]
_CC_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_CC_THINK_IDX = 0


class UI:
    """Aurora Glass UI — wide cards, real panels, zero raw stdout.

    Design rules that fix the "cramped terminal" complaint:
      • Every panel is built at the LIVE terminal width (_resync_width()
        is called first), never a fixed narrow column.
      • Long-running "thinking" / "streaming" status lives on ONE
        self-overwriting line via Rich's own control codes — never a
        loose sys.stdout.write escape sequence that leaves stray
        fragments behind.
      • Tool calls/results render as bordered cards with a title row,
        not `╭│╰` hand-drawn once per line.
    """

    # Small left gutter — a card border supplies the rest of the framing.
    _PAD = "  "

    @staticmethod
    def esc(text: str) -> str:
        return text.replace("[", "\\[").replace("]", "\\]") if HAS_RICH else text

    @staticmethod
    def _box():
        return ASCII if LEGACY_WIN_CONSOLE else ROUNDED

    @staticmethod
    def _card(body: "RenderableType", *, border: str = "cc_card_border",
               title: str = "", title_style: str = "cc_fg",
               subtitle: str = "", pad=(0, 1)) -> "Panel":
        """The one shared building block behind every boxed element —
        guarantees identical corner radii, padding, and full-width
        stretch everywhere in the app."""
        _resync_width()
        kwargs = dict(
            border_style=border,
            box=UI._box(),
            padding=pad,
            expand=True,
        )
        if title:
            kwargs["title"] = Text(title, style=title_style)
            kwargs["title_align"] = "left"
        if subtitle:
            kwargs["subtitle"] = Text(subtitle, style="cc_subtle")
            kwargs["subtitle_align"] = "right"
        return Panel(body, **kwargs)

    # ── App header / banner ─────────────────────────────────────────────
    @staticmethod
    def header(model_name: str, project: str) -> "RenderableType":
        if not HAS_RICH:
            return f"{APP_NAME}  v{APP_VERSION}  ·  {model_name}  ·  {project}"
        row = Table.grid(expand=True, padding=(0, 1))
        row.add_column(ratio=1)
        row.add_column(justify="right")
        left = Text()
        left.append("◆ ", style="brand")
        left.append(APP_NAME, style="brand")
        left.append(f"  v{APP_VERSION}", style="cc_subtle")
        right = Text()
        right.append(str(model_name), style="cc_fg")
        right.append("  ", style="cc_subtle")
        right.append(str(project), style="cc_muted")
        row.add_row(left, right)
        return UI._card(row, border="brand_dim")

    @staticmethod
    def neon_banner(model_name: str = "NVIDIA NIM"):
        if not HAS_RICH:
            print(f"{APP_NAME}  v{APP_VERSION}")
            print(f"{model_name}  ·  NVIDIA NIM")
            return
        _resync_width()
        console.print()
        title = Text(justify="center")
        title.append("◆  ", style="brand")
        title.append(APP_NAME, style="brand")
        title.append(f"  v{APP_VERSION}", style="cc_subtle")
        sub = Text(justify="center")
        sub.append(str(model_name), style="cc_fg")
        sub.append("   ·   ", style="cc_subtle")
        sub.append("NVIDIA NIM", style="cc_primary")
        sub.append("   ·   ", style="cc_subtle")
        sub.append("Zero Bottlenecks", style="cc_accent_soft")
        body = Group(Align.center(title), Text(""), Align.center(sub))
        console.print(UI._card(body, border="brand_dim", pad=(1, 2)))
        console.print()

    # ── Turn / status rail ───────────────────────────────────────────────
    @staticmethod
    def turn_indicator(round_num: int, tokens: int, status: str = "") -> "RenderableType":
        if not HAS_RICH:
            return f"{UI._PAD}●  TURN {round_num:03d}  ·  {tokens:,} tokens  {status}"
        row = Table.grid(expand=True, padding=(0, 1))
        row.add_column(ratio=1)
        row.add_column(justify="right")
        left = Text()
        left.append("● ", style="brand")
        left.append(f"TURN {round_num:03d}", style="brand")
        right = Text()
        right.append(f"{tokens:,} tokens", style="cc_status")
        if status:
            right.append("   ", style="cc_faint")
            right.append(str(status).upper(), style="cc_tool")
        row.add_row(left, right)
        return row

    @staticmethod
    def status_bar(model: str, tokens: int, phase: str = "") -> "RenderableType":
        if not HAS_RICH:
            return f"{model}  ·  {tokens:,} tokens  ·  {phase}"
        t = Text()
        t.append(" ", style="cc_status")
        t.append(str(model), style="cc_primary_bold")
        t.append("  ·  ", style="cc_faint")
        t.append(f"{tokens:,} tokens", style="cc_status")
        if phase:
            t.append("  ·  ", style="cc_faint")
            t.append(str(phase).upper(), style="cc_tool")
        return t

    @staticmethod
    def separator(title: str = ""):
        _resync_width()
        if HAS_RICH:
            console.print()
            if title:
                console.print(Rule(title=Text(f" {title.upper()} ", style="brand"),
                                    style="cc_faint", align="left"))
            else:
                console.print(Rule(style="cc_faint"))
        else:
            print(f"── {title} ──" if title else "─" * 40)

    # ── Conversation turns ───────────────────────────────────────────────
    @staticmethod
    def user_message(text: str) -> "RenderableType":
        text = text or ""
        if not HAS_RICH:
            out = [f"{UI._PAD}you ›"]
            for ln in text.splitlines() or [""]:
                out.append(f"{UI._PAD}  {ln}")
            return "\n".join(out)
        body = Text(text, style="cc_user")
        card = UI._card(body, border="cc_primary", title="you", title_style="cc_primary_bold")
        return card

    @staticmethod
    def assistant_stream() -> "Text":
        return Text("", style="cc_body") if HAS_RICH else Text("")  # type: ignore

    # ── Thinking / streaming heartbeat (single self-clearing line) ──────
    @staticmethod
    def thinking_block(text: str = "") -> "RenderableType":
        global _CC_THINK_IDX
        verb = _CC_VERBS[_CC_THINK_IDX % len(_CC_VERBS)]
        glyph = _CC_SPINNER[_CC_THINK_IDX % len(_CC_SPINNER)]
        _CC_THINK_IDX += 1
        if not HAS_RICH:
            return f"{UI._PAD}{glyph} {verb}…"
        t = Text()
        t.append(f"{glyph} ", style="cc_primary_bold")
        t.append(f"{verb}…", style="cc_thinking")
        return t

    @staticmethod
    def thinking_panel(text: str) -> "RenderableType":
        """Render a *finished* reasoning segment as real markdown inside
        a card, matching tool_call/tool_result/user_message styling.
        Markdown needs the whole segment (headers/lists/fences don't
        parse incrementally), so this is called once a reasoning block
        ends — the live token-by-token view stays on stream_write for
        responsiveness, and this replaces it with a properly formatted
        panel right after."""
        text = (text or "").strip()
        if not text:
            return Text("")
        if not HAS_RICH:
            return text
        body = Markdown(text, code_theme="monokai")
        return UI._card(body, border="cc_thinking", title="thinking",
                         title_style="cc_thinking")

    @staticmethod
    def thinking_live(elapsed: float = 0.0) -> None:
        """Overwrites a single status line in place — no leftover
        fragments, no raw ANSI, always clipped to the live width."""
        global _CC_THINK_IDX
        verb = _CC_VERBS[_CC_THINK_IDX % len(_CC_VERBS)]
        glyph = _CC_SPINNER[_CC_THINK_IDX % len(_CC_SPINNER)]
        _CC_THINK_IDX += 1
        suffix = f"  ({int(elapsed)}s)" if elapsed >= 8 else ""
        if HAS_RICH:
            _resync_width()
            line = Text()
            line.append(f" {glyph} ", style="cc_primary_bold")
            line.append(f"{verb}…", style="cc_thinking")
            if suffix:
                line.append(suffix, style="cc_thinking_dim")
            pad = max(0, console.width - line.cell_len - 1)
            console.print(line, " " * pad, end="\r", sep="")
        else:
            print(f"{UI._PAD}{glyph} {verb}…{suffix}", end="\r", flush=True)

    @staticmethod
    def streaming_pulse(elapsed: float) -> None:
        """Lightweight heartbeat once content has started flowing, so a
        long generation doesn't look frozen — one overwritten line."""
        if HAS_RICH:
            _resync_width()
            line = Text()
            line.append(" ⟲ ", style="cc_subtle")
            line.append(f"streaming · {elapsed:.0f}s", style="cc_thinking_dim")
            pad = max(0, console.width - line.cell_len - 1)
            console.print(line, " " * pad, end="\r", sep="")
        else:
            print(f"  streaming... ({elapsed:.0f}s)", end="\r", flush=True)

    @staticmethod
    def clear_live_line() -> None:
        if HAS_RICH:
            _resync_width()
            console.print(" " * max(1, console.width - 1), end="\r")
        else:
            print(" " * 100, end="\r", flush=True)

    # ── Word-wrapped token streaming (replaces raw sys.stdout.write) ────
    # Streaming the model's output char-by-char through Rich's markup
    # parser would be far too slow, and writing raw ANSI bypassed all
    # wrapping/padding — which is exactly what produced the one-word-
    # per-line, unstyled dump seen in narrow terminals. Instead we keep
    # a tiny local word buffer, wrap complete words ourselves against
    # the live terminal width, and emit each finished line through
    # Rich once, so color + width stay authoritative without per-token
    # markup-parsing overhead.
    _stream_col = 0
    _stream_buf = ""
    _stream_gutter = "  "

    @staticmethod
    def _stream_width() -> int:
        _resync_width()
        try:
            return max(40, console.width - len(UI._stream_gutter) - 1)
        except Exception:
            return 96

    @staticmethod
    def stream_write(chunk: str, style: str = "cc_body") -> None:
        """Append streamed text, wrapping at word boundaries against the
        live terminal width and writing only *complete* lines through
        Rich. Never prints a partial word via end="" — on a legacy
        Windows console (plain cmd.exe, no WT_SESSION/ANSICON/ConEmu)
        Rich's legacy renderer emits a newline on every print() call
        regardless of the `end` kwarg, so any per-token print collapses
        into one fragment per line. Buffering until a full wrapped line
        is ready fixes that on every console type."""
        if not chunk:
            return
        if not HAS_RICH:
            print(chunk, end="", flush=True)
            return
        limit = UI._stream_width()
        buf = UI._stream_buf + chunk
        out_lines = []
        while True:
            nl = buf.find("\n")
            window = buf if nl == -1 else buf[:nl]
            if len(window) <= limit and nl == -1:
                break
            if nl != -1 and nl <= limit:
                out_lines.append(buf[:nl])
                buf = buf[nl + 1:]
                continue
            break_at = window.rfind(" ", 0, limit)
            if break_at <= 0:
                break_at = limit
            out_lines.append(buf[:break_at])
            buf = buf[break_at:].lstrip(" ")
        UI._stream_buf = buf
        if out_lines:
            for ln in out_lines:
                console.print(Text(UI._stream_gutter + ln, style=style))
            UI._stream_col = 1
        # No partial-line branch: any leftover partial word/line stays
        # in UI._stream_buf until the next chunk completes it, or until
        # stream_flush_line() forces it out at a segment boundary.

    @staticmethod
    def stream_flush_line() -> None:
        """Flush any partial word buffer and drop to a fresh line —
        call between reasoning/content/tool-call segments."""
        if not HAS_RICH:
            print()
            return
        if UI._stream_buf:
            prefix = UI._stream_gutter if UI._stream_col == 0 else ""
            console.print(Text(prefix + UI._stream_buf))
            UI._stream_buf = ""
        console.print()
        UI._stream_col = 0

    # ── Tool calls / results as bordered cards ───────────────────────────
    @staticmethod
    def tool_call(name: str, args_preview: str) -> "RenderableType":
        label = {
            "read": "Read", "write": "Write", "edit": "Edit", "bash": "Bash", "run": "Run",
            "search": "Search", "glob": "Glob", "ls": "List", "todo": "Todo",
            "web_search": "WebSearch", "browse_page": "Browse", "task": "Task",
            "modules": "Modules",
        }.get(name, str(name).replace("_", " ").title().replace(" ", ""))
        if not HAS_RICH:
            return f"{UI._PAD}▸ {label}" + (f"  {args_preview}" if args_preview else "")
        t = Text()
        t.append("▸ ", style="cc_tool")
        t.append(label, style="cc_tool")
        if args_preview:
            t.append("  ", style="cc_faint")
            ap = args_preview if len(args_preview) < 120 else args_preview[:117] + "…"
            t.append(ap, style="cc_muted")
        return t

    @staticmethod
    def tool_result(name: str, output: str, is_error: bool = False) -> "RenderableType":
        lines = (output or "").splitlines() or [""]
        max_show = 18
        if len(lines) > max_show:
            shown = lines[:max_show]
            shown.append(f"… {len(lines) - max_show} more lines")
        else:
            shown = lines
        if not HAS_RICH:
            prefix = f"{UI._PAD}  " if not is_error else f"{UI._PAD}✗ "
            return "\n".join(prefix + ln for ln in shown)
        style = "cc_error" if is_error else "cc_body"
        border = "cc_error_dim" if is_error else "cc_card_border"
        body = Text("\n".join(ln[:220] for ln in shown), style=style)
        card = UI._card(
            body, border=border,
            title=("✗ error" if is_error else ""),
            title_style="cc_error",
            pad=(0, 2),
        )
        return card

    @staticmethod
    def permission_prompt(action: str, detail: str = "") -> "RenderableType":
        if not HAS_RICH:
            return f"\n{UI._PAD}[Permission] {action}\n{UI._PAD}{detail}\n{UI._PAD}Allow? (y/n)\n"
        rows = [Text(action, style="cc_fg")]
        if detail:
            rows.append(Text(detail[:220], style="cc_muted"))
        ask = Text()
        ask.append("Allow this action?  ", style="cc_permission")
        ask.append("[y/n]", style="bold cc_fg")
        rows.append(Text(""))
        rows.append(ask)
        card = UI._card(Group(*rows), border="cc_permission",
                         title="permission", title_style="cc_permission")
        return card

    # ── Files / diffs ─────────────────────────────────────────────────────
    @staticmethod
    def file_tree(root: Path, cwd: Path) -> "RenderableType":
        if not HAS_RICH:
            lines = []
            for p in sorted(root.rglob("*")):
                if any(part.startswith(".") for part in p.relative_to(root).parts):
                    continue
                depth = len(p.relative_to(root).parts) - 1
                icon = "📁" if p.is_dir() else "·"
                lines.append("  " * depth + f"{icon} {p.name}")
            return "\n".join(lines[:60])
        tree = Tree(Text(str(root), style="brand"))
        count = 0
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if count > 60:
                tree.add(Text("…", style="cc_muted"))
                break
            style = "cc_primary" if p.is_dir() else "cc_body"
            icon = "📁 " if p.is_dir() else "  "
            tree.add(Text(f"{icon}{rel}", style=style))
            count += 1
        return tree

    @staticmethod
    def file_diff(path: str, old: str, new: str) -> "RenderableType":
        if not HAS_RICH:
            return f"--- {path}\n+++ {path}\n"
        rows = []
        n = 0
        for line in difflib.unified_diff(
            (old or "").splitlines(), (new or "").splitlines(), lineterm="",
            n=3,
        ):
            if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
                rows.append(Text(line, style="cc_subtle"))
            elif line.startswith("+"):
                rows.append(Text(line, style="cc_success"))
                n += 1
            elif line.startswith("-"):
                rows.append(Text(line, style="cc_error"))
                n += 1
            else:
                rows.append(Text(line, style="cc_muted"))
            if n > 40:
                rows.append(Text("…", style="cc_muted"))
                break
        card = UI._card(Group(*rows) if rows else Text("(no changes)", style="cc_muted"),
                         border="cc_tool_dim", title="✎ edit", title_style="cc_tool",
                         subtitle=path, pad=(0, 2))
        return card

    # ── One-line status messages ─────────────────────────────────────────
    @staticmethod
    def ok(msg: str):
        if HAS_RICH:
            console.print(Text.assemble((f"{UI._PAD}✓ ", "cc_success"), (msg, "cc_body")))
        else:
            print(f"{UI._PAD}✓ {msg}")

    @staticmethod
    def warn(msg: str):
        if HAS_RICH:
            console.print(Text.assemble((f"{UI._PAD}⚠ ", "cc_warn"), (msg, "cc_body")))
        else:
            print(f"{UI._PAD}⚠ {msg}")

    @staticmethod
    def err(msg: str):
        if HAS_RICH:
            console.print(Text.assemble((f"{UI._PAD}✗ ", "cc_error"), (msg, "cc_fg")))
        else:
            print(f"{UI._PAD}✗ {msg}")

    @staticmethod
    def info(msg: str):
        if HAS_RICH:
            console.print(Text.assemble((f"{UI._PAD}· ", "cc_primary"), (msg, "cc_body")))
        else:
            print(f"{UI._PAD}· {msg}")


class TokenBucket:
    """Thread-safe token bucket for RPM enforcement with jitter."""

    def __init__(self, rpm: float, burst: int = 2, min_gap: float = 0.5, safety: float = 0.80):
        self.rpm = max(1.0, rpm) * safety
        self.burst = burst
        self.min_gap = min_gap
        self.tokens = float(burst)
        self.last_update = time.monotonic()
        self.last_request = 0.0
        self.lock = threading.RLock()
        self.history: deque[float] = deque()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.burst, self.tokens + elapsed * (self.rpm / 60.0))
        self.last_update = now

    def _prune(self):
        cutoff = time.monotonic() - 60.0
        while self.history and self.history[0] < cutoff:
            self.history.popleft()

    def wait_time(self) -> float:
        with self.lock:
            self._refill()
            self._prune()
            waits = []
            if self.tokens < 1.0:
                waits.append((1.0 - self.tokens) / (self.rpm / 60.0))
            if self.history and len(self.history) >= int(self.rpm):
                waits.append(60.0 - (time.monotonic() - self.history[0]) + 0.5)
            since_last = time.monotonic() - self.last_request
            if since_last < self.min_gap:
                waits.append(self.min_gap - since_last)
            return max(waits) if waits else 0.0

    def try_acquire(self) -> bool:
        with self.lock:
            self._refill()
            self._prune()
            self._refill()
            self._prune()
            waits = []
            if self.tokens < 1.0:
                waits.append((1.0 - self.tokens) / (self.rpm / 60.0))
            if self.history and len(self.history) >= int(self.rpm):
                waits.append(60.0 - (time.monotonic() - self.history[0]) + 0.5)
            since_last = time.monotonic() - self.last_request
            if since_last < self.min_gap:
                waits.append(self.min_gap - since_last)
            wait = max(waits) if waits else 0.0
            if wait <= 0.01:
                self.tokens -= 1.0
                self.last_request = time.monotonic()
                return True
            return False

    def acquire(self, timeout: float = 5.0) -> bool:
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            with self.lock:
                self._refill()
                self._prune()
                waits = []
                if self.tokens < 1.0:
                    waits.append((1.0 - self.tokens) / (self.rpm / 60.0))
                if self.history and len(self.history) >= int(self.rpm):
                    waits.append(60.0 - (time.monotonic() - self.history[0]) + 0.5)
                since_last = time.monotonic() - self.last_request
                if since_last < self.min_gap:
                    waits.append(self.min_gap - since_last)
                wait = max(waits) if waits else 0.0
                if wait <= 0.01:
                    self.tokens -= 1.0
                    self.last_request = time.monotonic()
                    return True
            time.sleep(min(max(wait, 0.05), 0.25))
        return False

    def commit(self):
        with self.lock:
            self.history.append(time.monotonic())

    def penalize(self, seconds: float):
        with self.lock:
            self.tokens = 0.0
            self.last_update = time.monotonic()
            self.last_request = time.monotonic() + max(0.0, seconds) - self.min_gap


@dataclass
class TokenMeter:
    prompt: int = 0
    completion: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, prompt: int, completion: int):
        with self.lock:
            self.prompt += max(0, prompt)
            self.completion += max(0, completion)

    def report(self) -> str:
        total = self.prompt + self.completion
        return f"{total:,} tok total ({self.prompt:,} prompt + {self.completion:,} completion)"


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 4: PROVIDER POOL (Self-Healing)
# ═══════════════════════════════════════════════════════════════════════════════

class Provider:
    def __init__(self, name: str, base_url: str, api_key: str, model_cfg: Dict[str, Any],
                 connect_timeout: float = 10.0, read_timeout: float = 20.0, bucket: Optional[TokenBucket] = None):
        self.name = name
        self.model_cfg = model_cfg
        self.model_id = model_cfg["id"]
        self.bucket = bucket if bucket is not None else TokenBucket(model_cfg.get("rpm", 20), safety=0.80)
        # Identity of the account/endpoint this provider talks to. Used
        # solely to find "sibling" providers that share the same real
        # quota (same base_url + api_key) when one of them gets a LIVE
        # 429 from the server — see ProviderPool.propagate_shared_cooldown.
        # Providers already share a TokenBucket instance when they share
        # this key (see build_pool's get_or_create_bucket), which correctly
        # prevents new *local* over-request; this additional identity is
        # for the separate case of a 429 that has already happened on the
        # wire, where every sibling provider should skip straight to its
        # own cooldown instead of each one having to independently
        # round-trip to the server and get its own 429 first.
        self._account_key = (base_url, api_key)

        if HAS_OPENAI and HAS_HTTPX:
            # Relaxed pool limits to prevent connection drops on heavy retry/sprawl
            http_timeout = httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=30.0,
                pool=30.0
            )
            self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=http_timeout, max_retries=0)
        elif HAS_OPENAI:
            self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=read_timeout, max_retries=0)
        else:
            self.client = None
        self.cooldown_until = 0.0
        self.failures = 0
        self.consecutive_failures = 0
        self.permanently_disabled = False

    def available(self) -> bool:
        if getattr(self, "permanently_disabled", False):
            return False
        return time.monotonic() >= self.cooldown_until

    def record_success(self):
        self.bucket.commit()
        self.failures = max(0, self.failures - 1)
        self.consecutive_failures = 0

    def record_failure(self, cooldown: float = 15.0, *, permanent: bool = False):
        self.failures += 1
        self.consecutive_failures += 1
        if permanent:
            self.permanently_disabled = True
            self.cooldown_until = time.monotonic() + 86400.0  # effectively gone
            return
        self.cooldown_until = time.monotonic() + cooldown * min(self.consecutive_failures, 3)


class ProviderPool:
    def __init__(self, providers: List[Provider]):
        self.providers = providers
        self.index = 0
        self.attempts_on_current = 0

    def current(self) -> Provider:
        return self.providers[self.index]

    def rotate(self):
        self.index = (self.index + 1) % len(self.providers)
        self.attempts_on_current = 0

    def next_available(self) -> Optional[Provider]:
        for _ in range(len(self.providers)):
            p = self.current()
            if p.available() and p.bucket.try_acquire():
                # Advance the index now, on hand-out, not only when a
                # provider is found unavailable. Previously self.index
                # only ever moved inside the `else: self.rotate()`
                # branch below — meaning that with every provider
                # healthy, next_available() always returned
                # self.providers[self.index] unchanged, since nothing
                # ever advanced it on a success path. Confirmed via
                # simulation: 3 healthy providers, 6 consecutive calls,
                # same provider returned every time. That's not a
                # resilience bug (failure cooldown/permanent-disable
                # still route around a broken provider correctly via
                # this same loop) — it's a load-spreading bug: capacity
                # on 2 of 3 configured providers went completely unused
                # under normal healthy operation, concentrating rate
                # limits and cost on a single account instead of
                # spreading across the pool as configured.
                self.rotate()
                return p
            self.rotate()
        return None

    def should_rotate(self, max_per_provider: int = 3) -> bool:
        self.attempts_on_current += 1
        return self.attempts_on_current >= max_per_provider

    def propagate_shared_cooldown(self, source: "Provider", cooldown: float) -> List[str]:
        """After `source` gets a LIVE 429 from the server, apply the same
        cooldown to every other provider sharing its (base_url, api_key)
        — i.e. the same real account quota. The shared TokenBucket already
        stops new *local* over-request between siblings, but a 429 that
        already happened on the wire means the account is confirmed over
        quota right now; without this, next_available() will still hand
        out sibling providers (they pass their own, still-fresh
        cooldown_until and their own try_acquire on the shared bucket
        might still succeed depending on timing) and each one burns a
        real network round-trip just to get its own 429 and its own
        cooldown, which is exactly the back-to-back
        NIM-Primary / NIM-minimax-m3 / NIM-laguna-xs-2.1 429 sequence
        this exists to shorten. Returns the names of siblings that were
        cooled down, for logging.
        """
        cooled: List[str] = []
        for p in self.providers:
            if p is source or getattr(p, "permanently_disabled", False):
                continue
            if getattr(p, "_account_key", None) == getattr(source, "_account_key", None):
                p.cooldown_until = max(p.cooldown_until, time.monotonic() + cooldown)
                cooled.append(p.name)
        return cooled

    def shortest_wait(self) -> float:
        active = [p for p in self.providers if not getattr(p, "permanently_disabled", False)]
        if not active:
            return 9999.0
        waits = [max(0.0, p.cooldown_until - time.monotonic()) for p in active]
        bucket_waits = [p.bucket.wait_time() for p in active]
        return min(waits + bucket_waits) if (waits or bucket_waits) else 2.0


def build_pool(config: Dict[str, Any], model_key_override: Optional[str] = None) -> ProviderPool:
    providers: List[Provider] = []
    api_key = config.get("api_key", "")
    base_url = config.get("base_url", NIM_BASE_URL)
    model_key = model_key_override if model_key_override in MODELS else config.get("default_model", "glm-5.2")
    read_timeout = float(config.get("http_read_timeout") or 180.0)
    connect_timeout = float(config.get("http_connect_timeout") or 15.0)
    _pd_raw = config.get("project_dir")
    dead_model_ids = set(load_dead_models(Path(_pd_raw).resolve())) if _pd_raw else set()
    
    nim_shared_rpm = float(config.get("nim_shared_rpm", 38.0))
    shared_buckets: Dict[Tuple[str, str], TokenBucket] = {}

    def get_or_create_bucket(_model_key: str) -> TokenBucket:
        key = (base_url, api_key)
        if key not in shared_buckets:
            shared_buckets[key] = TokenBucket(nim_shared_rpm, safety=0.80)
        return shared_buckets[key]

    if api_key and model_key in MODELS and MODELS[model_key]["id"] not in dead_model_ids:
        providers.append(Provider("NIM-Primary", base_url, api_key, MODELS[model_key],
                                  connect_timeout, read_timeout, get_or_create_bucket(model_key)))

    # Base fallback order, used when no phase override is active: ordered by
    # expected tool-call reliability, not raw size. glm-5.2-fp8 shares
    # GLM-5.2's post-training/tool-calling recipe, so it drifts least from
    # primary. minimax-m3 is MoE with native tool-calling and a 1M context
    # window — strong long-context fallback. kimi-k2-thinking (Moonshot) is
    # placed ahead of laguna-xs-2.1: its NVIDIA model card explicitly
    # documents stable tool-use across 200-300 consecutive calls and ships
    # its own tool-call parsing pipeline (see the malformed-tool-call
    # streak detector above, which now rotates off a stuck provider after
    # 3 consecutive malformed turns — kimi-k2-thinking is a better landing
    # spot than laguna-xs-2.1 when that fires). Still NOT promoted above
    # glm-5.2-fp8/minimax-m3 because it always runs in forced thinking
    # mode, the same category of model that produced the original stall.
    # laguna-xs-2.1 (Poolside) stays last: newest/least battle-tested
    # entry here, and the one whose output triggered the malformed
    # tool-call loop this fallback chain was patched to survive.
    # Nemotron and Llama 3.1 405B removed from the catalog entirely.
    # qwen3-coder removed: NVIDIA pulled it from the NIM catalog.
    base_fallbacks = [
        "glm-5.2-fp8",
        "minimax-m3",
        "kimi-k2-thinking",
        "laguna-xs-2.1",
    ]
    if model_key_override is not None and model_key_override != config.get("default_model", "glm-5.2"):
        # A phase override (e.g. testing/verification -> a stronger model)
        # exists specifically because glm-5.2/glm-5.2-fp8 got stuck in this
        # phase before. If the override model itself rate-limits or errors,
        # ProviderPool.rotate() walks this same list — so leaving
        # glm-5.2-fp8 first would silently hand the phase right back to the
        # model it was overridden to avoid. Push the OTHER strong models
        # first instead, and demote glm-5.2/glm-5.2-fp8 to last-resort: still
        # available so a fully exhausted pool doesn't hard-fail, but only
        # reached after every stronger option is gone.
        strong_first = [m for m in base_fallbacks if m not in ("glm-5.2-fp8",)]
        demoted = [m for m in base_fallbacks if m in ("glm-5.2-fp8",)]
        if config.get("default_model", "glm-5.2") == "glm-5.2":
            demoted = ["glm-5.2"] + demoted if "glm-5.2" not in demoted else demoted
        fallbacks = strong_first + demoted
    else:
        fallbacks = base_fallbacks
    for fb in fallbacks:
        if fb != model_key and fb in MODELS and api_key and MODELS[fb]["id"] not in dead_model_ids:
            providers.append(Provider(f"NIM-{fb}", base_url, api_key, MODELS[fb],
                                      connect_timeout, read_timeout, get_or_create_bucket(fb)))

    if config.get("enable_openrouter", True):
        or_key = os.environ.get("OPENROUTER_API_KEY")
        if or_key:
            or_base = "https://openrouter.ai/api/v1"
            or_models = [
                ("OR-Nemotron", "nvidia/nemotron-3-ultra-550b-a55b:free"),
                ("OR-Qwen3", "qwen/qwen3-coder:free"),
            ]
            or_bucket = TokenBucket(15, safety=0.80)
            for name, mid in or_models:
                fake_cfg = {
                    "id": mid,
                    "name": name,
                    "rpm": 15,
                    "max_tokens": 8192,
                    "ctx_window": 131072,
                    "thinking": False,
                    "extra_body": None,
                }
                providers.append(Provider(
                    name, or_base, or_key, fake_cfg,
                    connect_timeout, read_timeout, or_bucket,
                ))

    if not providers:
        raise RuntimeError(
            "No providers configured for this project. "
            "Set a NVIDIA NIM key:  (1) env NVIDIA_API_KEY=nvapi-...  "
            "(2) python neon_architect.py and /apikey nvapi-...  "
            "(3) or OPENROUTER_API_KEY for OpenRouter fallback. "
            f"project={config.get('project_dir')}"
        )

    return ProviderPool(providers)


@dataclass
class ToolResult:
    output: str
    error: str = ""
    is_error: bool = False
    # Per-call test-run digest, set only by RunTool when the call was a
    # test invocation. This used to travel via a RunTool.last_test_digest
    # CLASS attribute (mutated in execute(), read back by the caller
    # immediately after) — harmless while tool calls run strictly
    # sequentially, but a real race condition waiting to happen if the
    # execution loop is ever parallelized (concurrent.futures is already
    # imported and MAX_PARALLEL_TOOLS already exists as a batch-size
    # concept). Attaching it to the ToolResult instance instead removes
    # the shared mutable state altogether — each call's digest travels
    # with that call's own result object, so there's nothing to race on
    # regardless of how the loop is executed in the future.
    test_digest: str = ""

    def text(self) -> str:
        # is_error covers two different situations that must not be
        # collapsed into the same "[ERROR] " rendering:
        #   (a) an exception prevented the tool from producing output at
        #       all — self.error is populated, self.output is empty.
        #   (b) the underlying program ran successfully but exited
        #       non-zero (e.g. a failing pytest run, a failed lint) —
        #       self.output holds the actually useful output (the failure
        #       trace) and self.error is empty. Discarding that output
        #       here would leave the model staring at a bare "[ERROR] "
        #       with no idea what actually failed.
        if self.is_error and self.error:
            return f"[ERROR] {self.error}"
        if self.is_error and self.output:
            return self.output
        if self.is_error:
            return "[ERROR] (no output captured)"
        return self.output


class Tool:
    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)

    def __init__(self, cwd: Path):
        self.cwd = cwd.resolve()

    allow_outside_project: bool = False

    # Chars that only ever show up in a path when the model has started
    # dumping raw tool-call fragments (braces, quotes, commas from JSON;
    # '#' from markdown headers) into the path string instead of retrying
    # cleanly. None of these are valid in a real project-relative path.
    _JUNK_CHARS = set("{}\"'`#*?<>|")
    _MAX_PATH_LEN = 200

    @staticmethod
    def _sanitize_rel_path(path: str) -> str:
        """Aggressive sanitization for hallucinated paths (PLAN.md, content:...)"""
        raw = (path or "").strip().strip('"').strip("'")
        if not raw:
            return ""
        if raw.startswith("\\?\\"):
            raw = raw[4:]
        raw = raw.split("#", 1)[0]
        # A '?query=string' suffix is never part of a real project-relative
        # path — it's a tool-call/URL fragment leaking in, same failure mode
        # as the '#' case above. Strip it the same way instead of letting it
        # reach _degenerate_path_reason, where it hard-rejects on every retry
        # because '?' is a junk char but nothing upstream ever removes it.
        raw = raw.split("?", 1)[0]
        
        low = raw.lower()
        if ", content" in low or ", requirements" in low or ", risks" in low:
            raw = raw.split(",", 1)[0]
        if ":" in raw and not (len(raw) > 1 and raw[1] == ":" and sys.platform == "win32"):
            # strip hallucinated keys like 'path: PLAN.md'
            raw = raw.split(":", 1)[0] if len(raw.split(":", 1)[0]) > 2 else raw
        if "," in raw:
            left = raw.split(",", 1)[0].strip()
            if "." in Path(left).name or "/" in left:
                raw = left
                
        raw = raw.strip().rstrip(",").replace("\\", "/")
        bad = {"write", "edit", "read", "path", "content", "todo", "bash", "none", "null"}
        if raw.lower() in bad:
            return ""
        return raw

    @classmethod
    def _degenerate_path_reason(cls, raw: str) -> str:
        """Detect self-inflicted degenerate paths: a model dodging a write/edit
        refusal by mutating the filename (accreting '.txt.txt.txt...', or
        stuffing JSON/markdown fragments into the path) instead of retrying
        with edit/append/force. Returns a non-empty reason string if the path
        should be rejected outright, else ''.
        """
        if len(raw) > cls._MAX_PATH_LEN:
            return (
                f"path is {len(raw)} chars, over the {cls._MAX_PATH_LEN}-char limit. "
                "This usually means a filename got mutated across repeated failed "
                "calls instead of being retried cleanly. Use the ORIGINAL intended "
                "path, and use edit/append/force=true to fix content, not a new filename."
            )
        low = raw.lower()
        if low.count(".mdx.dump") >= 1 or low.count(".dump.") >= 2:
            return (
                "path looks like a hallucinated backup/dump accretion "
                "(e.g. PLAN.md.backup.mdx.dump...). Use the ORIGINAL filename only "
                "(PLAN.md / ARCHITECTURE.md / etc.), never invent dump suffixes."
            )
        if any(ch in cls._JUNK_CHARS for ch in raw):
            bad = sorted({ch for ch in raw if ch in cls._JUNK_CHARS})
            return (
                f"path contains invalid character(s) {bad}. "
                "This looks like a raw tool-call fragment leaked into the path "
                "rather than a real filename. Use a plain project-relative path."
            )
        name = Path(raw).name
        # Collapse repeated identical dot-segments, e.g. "foo.txt.txt.txt" ->
        # segments ['foo','txt','txt','txt']. 3+ identical trailing segments
        # is never a real extension, only accretion from repeated retries.
        segs = name.split(".")
        if len(segs) >= 4:
            tail = segs[-1]
            repeat = 0
            for s in reversed(segs[1:]):
                if s == tail:
                    repeat += 1
                else:
                    break
            if repeat >= 3:
                return (
                    f"filename '{name}' has the same extension ('.{tail}') repeated "
                    f"{repeat}x in a row. This is accretion from retrying with a "
                    "slightly different filename each time rather than fixing the "
                    "actual call. Use the original filename (e.g. strip back to "
                    f"'{segs[0]}.{tail}') and use edit/append/force=true instead of "
                    "renaming."
                )
        return ""

    def _safe_path(self, path: str) -> Path:
        raw = self._sanitize_rel_path(path)
        if not raw:
            raise ValueError(
                "path is required (use project-relative paths, e.g. src/app.py or PLAN.md). "
                f"Got: {path!r}"
            )
        degenerate = self._degenerate_path_reason(raw)
        if degenerate:
            raise ValueError(f"Rejected path '{path}': {degenerate}")
        candidate = Path(raw)
        target = candidate.resolve() if candidate.is_absolute() else (self.cwd / raw).resolve()
        try:
            target.relative_to(self.cwd.resolve())
        except ValueError:
            raise PermissionError(
                f"Path '{path}' is outside the project folder ({self.cwd}). "
                f"All file access is limited to the project directory."
            )
        return target

    def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }


class ReadTool(Tool):
    name = "read"
    description = "Read the contents of a file. Whole file is returned unless it exceeds the limit."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to file"},
            "offset": {"type": "integer", "description": "Line number to start from (1-based)", "default": 1},
            "limit": {"type": "integer", "description": "Max lines to read", "default": 2000},
        },
        "required": ["path"],
    }

    _cache: Dict[str, Tuple[float, int, str, List[str]]] = {}
    _cache_lock = threading.Lock()

    def execute(self, path: str, offset: int = 1, limit: int = 2000) -> ToolResult:
        try:
            fp = self._safe_path(path)
            if not fp.exists():
                return ToolResult("", error=f"File not found: {path}", is_error=True)

            key = str(fp)
            stat = fp.stat()
            was_cached = False

            with self._cache_lock:
                cached = self._cache.get(key)
                if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                    lines = cached[3]
                    was_cached = True
                else:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    lines = text.splitlines(keepends=True)
                    self._cache[key] = (stat.st_mtime, stat.st_size, text, lines)

            start = max(0, offset - 1)
            end = start + limit
            chunk = "".join(lines[start:end])
            remaining = len(lines) - end

            prefix = ""
            if was_cached and offset == 1:
                prefix = f"[already read this session, unchanged — {len(lines)} lines total]\n"

            if remaining > 0:
                chunk += (
                    f"\n\n... [{remaining} more lines — increase 'limit' or set "
                    f"offset={end + 1} to continue, file has {len(lines)} lines total] ..."
                )
            return ToolResult(prefix + chunk)
        except Exception as e:
            return ToolResult("", error=str(e), is_error=True)


class WriteTool(Tool):
    name = "write"
    description = (
        "Create or overwrite a file (or append). path MUST be project-relative "
        "(e.g. src/app.py). Prefer coherent chunks (<4000 chars). "
        "CRITICAL: content MUST include REAL newlines between statements/lines — "
        "never collapse a whole Python/JS/TS file onto one line (SyntaxError). "
        "NEVER overwrite an existing file with a tiny fragment — that destroys it. "
        "To change part of a file use edit. To continue a long write use append=true. "
        "Creates parent dirs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project-relative path"},
            "content": {
                "type": "string",
                "description": (
                    "Full file text with real newline characters between lines. "
                    "Do NOT join statements with spaces only."
                ),
            },
            "append": {"type": "boolean", "default": False, "description": "Append instead of overwrite"},
            "force": {
                "type": "boolean",
                "default": False,
                "description": "Set true to intentionally truncate/shrink an existing file with a small payload.",
            },
        },
        "required": ["path", "content"],
    }
    WARN_CHARS = 12000
    # Content that looks like it's meant to be *added* to a doc rather than
    # replace it (markdown headers, bullet lists, etc). Used to auto-append
    # instead of hard-failing on the tiny/shrinking-overwrite guard below.
    _ADDITION_MARKERS = ("#", "-", "*", "1.", "```")
    # A marker with (almost) nothing after it — e.g. a bare "```python" or
    # a lone "#" — is a malformed/truncated payload, not a real section
    # being added. Require a minimum body length beyond the marker itself
    # before the auto-append heuristic below is allowed to fire.
    _MIN_ADDITION_BODY_CHARS = 20
    # Plan/spec artifacts where a tiny "section-looking" scrap must NOT be
    # silently auto-appended — these are hand-curated, order-sensitive docs
    # where a stray fragment landing in the wrong place is worse than a
    # loud refusal. Real edits go through edit(), or write(..., force=true)
    # for an intentional full overwrite.
    _NO_AUTO_APPEND = {"plan.md", "architecture.md", "verification.md", "design.md"}
    # Multi-statement source code with almost no newlines — the classic
    # "model collapsed an entire file onto one line" failure. This must
    # NOT fire on a single legitimate statement that happens to contain
    # two keywords from the list (e.g. "from __future__ import
    # annotations" contains both "from" and "import" but is one atomic
    # statement). So instead of matching any two keywords anywhere in the
    # string, require a statement BOUNDARY between them: a colon opening
    # a block body, a semicolon, or a second statement-starting keyword
    # appearing after the first statement has already ended (heuristically:
    # preceded by whitespace and not immediately following "from"/"import"
    # dotted-path tokens).
    _COLLAPSED_SRC_RE = re.compile(
        r"\b(def|class|import|from|function|const|let|var|export|package|fn|pub)\b"
        r"[^\n]{0,200}?"
        r"[:;]\s*"
        r"[^\n]{4,}?"
        r"\b(def|class|import|from|function|const|let|var|export|package|fn|pub|return|assert)\b",
        re.S,
    )
    # A single "from X import Y[, Z...]" / "import X[, Y...]" line is
    # normally one legitimate statement, no matter how long — EXCEPT when
    # the imported names repeat many times, which is the actual signature
    # RC-2 identified: a model-corrupted write like "from datetime import
    # datetime, timezone, ..." repeated 80x still matches this regex's
    # grammar (comma-separated names, arbitrarily long), so it was exiting
    # _looks_collapsed_source as "exempt" before length/newline checks
    # ever ran. No real import statement names the same symbol dozens of
    # times — that repetition is diagnostic of corrupted output, not a
    # long-but-legitimate import. See _has_repeated_import_names below,
    # checked alongside this regex rather than folded into it, since a
    # regex can't count distinct-name repetition on its own.
    _SINGLE_IMPORT_LINE_RE = re.compile(
        r"^\s*(from\s+[\w.]+\s+import\s+[\w., *()\n]+|import\s+[\w., ]+)\s*$"
    )

    @staticmethod
    def _has_repeated_import_names(stripped_import_line: str) -> bool:
        """True if the same imported name appears many times in a single
        import statement — the concrete pattern behind RC-2's garbage
        write (`from datetime import datetime, timezone, ...` repeated
        80x). A real import statement lists each name once; >=4 repeats
        of any single name is well past anything a person or a correct
        model output would ever write, and comfortably below what a
        genuinely large-but-legitimate import list would produce for any
        one name.
        """
        # Strip a leading "from X import " prefix if present; either way
        # what's left is the comma-separated name list to inspect.
        m = re.match(r"^\s*from\s+[\w.]+\s+import\s+(.*)$", stripped_import_line, re.S)
        names_part = m.group(1) if m else re.sub(r"^\s*import\s+", "", stripped_import_line)
        names = [n.strip().split(" as ")[0].strip() for n in names_part.split(",")]
        names = [n for n in names if n and n != "*"]
        if not names:
            return False
        from collections import Counter
        counts = Counter(names)
        return counts.most_common(1)[0][1] >= 4

    # Markdown collapse detector: real Markdown headings each start their
    # own line. Three or more '#'-heading markers appearing mid-string
    # (i.e. not just at position 0) with almost no real newlines means the
    # document's structure has been flattened onto one line — every
    # section/heading/list-item boundary downstream parsers rely on
    # (extract_acceptance_criteria, plan_missing_sections, etc.) is gone,
    # even though the text itself looks superficially complete.
    _COLLAPSED_MD_HEADING_RE = re.compile(r"(?:^|\s)#{1,6}\s+\S")

    @staticmethod
    @staticmethod
    def _strip_balanced_code_fence(data: str, path: str) -> str:
        """Strip a matched ```lang ... ``` wrapper the model sometimes wraps
        its ENTIRE file output in, despite tool instructions saying not to.

        This is the gap RC-1 identified correctly: the collapsed-source
        guard below looks for "almost no real newlines" — a well-formed
        fenced block with normal internal formatting has plenty of
        newlines, so it sails past that check untouched and gets written
        to disk as literally invalid syntax (the fence markers themselves
        aren't valid Python/JS/etc). The existing _ADDITION_MARKERS /
        odd-fence-count logic only catches an UNBALANCED fence (a bare
        opening marker with no close) — it warns, it doesn't strip a
        cleanly matched pair.

        Deliberately narrow, same safety posture as
        _try_repair_collapsed_imports: only fires when the fence is
        unambiguous — first non-empty line is exactly ```<optional lang
        tag>, last non-empty line is exactly ```, and nothing meaningful
        sits outside those markers. Anything less clear-cut (fence
        appearing mid-file, multiple fenced blocks, prose before/after) is
        left alone and falls through to the existing refuse/warn paths
        rather than risk silently mangling real content — e.g. a file that
        legitimately needs to contain the string "```" in a docstring or
        markdown-generation code.
        """
        if not data:
            return data
        stripped = data.strip()
        lines = stripped.splitlines()
        if len(lines) < 3:
            return data
        first, last = lines[0].rstrip(), lines[-1].strip()
        if not first.startswith("```") or last != "```":
            return data
        # First line must be JUST the fence marker plus an optional bare
        # language tag (letters/digits/+/- only) — not "```python code
        # here" with real content glued onto the same line, which would
        # mean stripping the marker loses that content.
        lang_tag = first[3:].strip()
        if lang_tag and not re.match(r"^[A-Za-z0-9+_-]+$", lang_tag):
            return data
        body = "\n".join(lines[1:-1])
        # A second, unmatched ``` anywhere inside means this isn't a
        # single clean fence (could be a second fenced block, or fences
        # discussed as literal text) — bail rather than guess.
        if "```" in body:
            return data
        if not body.strip():
            return data
        return body + "\n"

    @staticmethod
    def _normalize_content(data: str) -> str:
        """When a payload looks collapsed (almost no real newlines, but
        contains literal \\n / \\t escape sequences), expand those escapes
        into real newlines/tabs before we even run the collapsed-source
        check. This recovers the common case where the model meant to
        send real lines but the string got escaped somewhere upstream."""
        if not data:
            return data
        if data.count("\n") < 2 and ("\\n" in data or "\\t" in data):
            data = (
                data.replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace("\\r", "\n")
            )
        return data

    @classmethod
    def _try_repair_collapsed_imports(cls, data: str, path: str) -> Optional[str]:
        """Narrow, provably-safe auto-repair for ONE specific collapsed-source
        pattern: a run of import statements concatenated with spaces instead
        of newlines (e.g. "import pytest from backend.auth_mock import
        signup, login" — exactly the shape that triggered repeated refuse/
        retry loops in practice, since a model that drops newlines tends to
        do it on its import block specifically).

        Deliberately does NOT attempt to repair collapsed def/class bodies,
        control flow, or anything with executable statement logic — the
        model's intended statement boundaries there are genuinely ambiguous
        from a single-line string (e.g. is "assert" starting a new statement
        or is it inside an expression?), and guessing wrong would silently
        write broken-but-plausible-looking code. Only python files are
        attempted since the statement grammar this matches against is
        python-specific.

        Returns repaired text (guaranteed to (a) reconstruct the original
        content exactly when whitespace is collapsed to single spaces, and
        (b) successfully ast.parse) or None if it can't confidently repair,
        in which case the caller falls through to the normal refusal.
        """
        ext = Path(path or "").suffix.lower()
        if ext not in (".py", ".pyi"):
            return None
        stripped = data.strip()
        # Bail immediately if anything beyond imports is present — this is
        # the safety boundary. Only pure import-statement text is eligible.
        if re.search(
            r"\b(def|class|return|assert|if|elif|else|for|while|try|except|"
            r"finally|with|raise|lambda|yield|async|await)\b",
            stripped,
        ):
            return None
        stmt_re = re.compile(
            r"from\s+[\w.]+\s+import\s+[\w, ()*]+?(?=\s+(?:from\s|import\s)|$)"
            r"|import\s+[\w.]+(?:\s*,\s*[\w.]+)*(?=\s+(?:from\s|import\s)|$)"
        )
        matches = list(stmt_re.finditer(stripped))
        stmts = [m.group(0).strip() for m in matches]
        if len(stmts) < 2:
            return None  # nothing to repair, or not actually collapsed imports
        # Every matched statement, rejoined with single spaces, must
        # reconstruct the original exactly. If anything in the middle
        # wasn't captured by the pattern (stray token, malformed syntax),
        # this fails and we refuse to guess.
        if " ".join(stmts) != stripped:
            return None
        candidate = "\n".join(stmts)
        try:
            ast.parse(candidate)
        except SyntaxError:
            return None
        return candidate

    @classmethod
    def _looks_collapsed_source(cls, data: str, path: str) -> bool:
        """Detect multi-statement source code, OR multi-section Markdown,
        crammed onto (almost) one line — this is what turns into
        SyntaxErrors (for code) or silently-unparseable documents (for
        Markdown, where downstream section/heading extraction depends on
        real line breaks) further down the pipeline. Config/data files
        other than the two categories below are exempt, since single-line
        content is often legitimate there (e.g. minified JSON).
        A lone import/from-import statement is exempt outright — that's
        one real statement, not collapsed code, no matter its length.
        Very short payloads are also exempt: there isn't room for two
        real statements to be jammed together under ~40 chars, so this
        is far more likely a genuinely tiny file (a one-line stub,
        __init__.py re-export, etc.) than truncated multi-statement code."""
        if data.count("\n") >= 3:
            return False
        stripped = data.strip()
        if cls._SINGLE_IMPORT_LINE_RE.match(stripped):
            if not cls._has_repeated_import_names(stripped):
                return False  # a real, non-repeating import line — exempt
            # Matches import-line grammar but repeats a name >=4 times —
            # this IS the collapsed-garbage case (RC-2), report it
            # directly rather than falling through to the generic
            # multi-statement/double-space checks below, which are
            # looking for a different shape of corruption (two distinct
            # statements glued together) and don't reliably catch a
            # single repeated statement like this one.
            return True
        if len(stripped) < 40:
            return False
        ext = Path(path or "").suffix.lower()
        if ext in {".md", ".markdown"}:
            # A short single-line note ("# TODO") is fine; a long document
            # with several heading markers flattened onto one line is not
            # — it means every section boundary a later parser relies on
            # has been lost. Require several headings AND real length so
            # we don't flag e.g. a single long unwrapped paragraph.
            if len(stripped) > 300 and len(cls._COLLAPSED_MD_HEADING_RE.findall(data)) >= 3:
                return True
            return False
        if ext not in {
            ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            ".go", ".rs", ".java", ".kt", ".cs", ".rb", ".php", ".swift",
        }:
            return False
        if cls._COLLAPSED_SRC_RE.search(data):
            return True
        if len(data) > 120 and data.count("\n") == 0 and data.count("  ") >= 2:
            return True
        return False

    def execute(self, path: str = "", content: str = None, append: bool = False, force: bool = False) -> ToolResult:
        try:
            if not path or not str(path).strip():
                return ToolResult(
                    "",
                    error="path is required. Example: path='src/core/agents/pm.py'",
                    is_error=True,
                )
            if content is None:
                return ToolResult("", error="content is required", is_error=True)
            fp = self._safe_path(path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            data = content if isinstance(content, str) else str(content)
            data = self._normalize_content(data)
            fence_stripped = self._strip_balanced_code_fence(data, path)
            if fence_stripped != data:
                data = fence_stripped
            # Try the narrow, provably-safe import-block repair FIRST and
            # unconditionally (not gated behind _looks_collapsed_source):
            # a pure run of collapsed import statements with single spaces
            # (no colons/semicolons, no double-spaces) never trips that
            # guard's pattern in the first place, so gating the repair
            # behind it would make the repair dead code for its own most
            # common case. _try_repair_collapsed_imports only ever returns
            # non-None when it can prove the repaired text (a) preserves
            # every character of the original statement content and
            # (b) successfully ast.parse()s, so applying it here can only
            # help and never silently corrupts anything.
            if not force:
                repaired = self._try_repair_collapsed_imports(data, path)
                if repaired is not None and repaired != data:
                    UI.info(
                        f"Auto-repaired collapsed import statements in {path} "
                        f"(inserted newlines, verified with ast.parse)."
                    )
                    data = repaired
            if self._looks_collapsed_source(data, path) and not force:
                is_md = Path(path or "").suffix.lower() in {".md", ".markdown"}
                if is_md:
                    detail = (
                        "looks like a multi-section Markdown document with several headings "
                        "flattened onto one line. Re-send content with REAL newline characters "
                        "between headings, list items, and paragraphs — otherwise downstream "
                        "section parsers (acceptance-criteria extraction, section checks, etc.) "
                        "will silently see it as empty or malformed. "
                        "If you keep producing this same flattened text, STOP resending the "
                        "full document at once — instead write() just the first section (e.g. "
                        "'# Title\\n\\n## Requirements\\n...' with real \\n between lines) and "
                        "then write(..., append=true) each remaining section one at a time; "
                        "smaller chunks are far less likely to collapse than one giant string."
                    )
                else:
                    detail = (
                        "looks like multi-statement code. Re-send content with REAL newline "
                        "characters between imports/defs/statements. If this keeps recurring, "
                        "write the file in smaller chunks via write(...) then "
                        "write(..., append=true) instead of one large payload."
                    )
                return ToolResult(
                    "",
                    error=(
                        f"REFUSED collapsed write to {path}: content has almost no "
                        f"newlines but {detail} ({len(data)} chars, "
                        f"{data.count(chr(10))} newlines). "
                        f"(force=true bypasses this guard only if you truly mean one line.)"
                    ),
                    is_error=True,
                )
            auto_appended = False
            if not append and not force and fp.is_file():
                try:
                    existing_len = fp.stat().st_size
                except OSError:
                    existing_len = 0
                target_ext = fp.suffix.lower()
                is_source_file = target_ext in {
                    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
                    ".go", ".rs", ".java", ".kt", ".cs", ".rb", ".php", ".swift",
                }
                stripped_for_marker_check = data.strip()
                looks_like_addition = (
                    not is_source_file
                    and stripped_for_marker_check.startswith(self._ADDITION_MARKERS)
                    and len(stripped_for_marker_check) >= self._MIN_ADDITION_BODY_CHARS
                )
                is_tiny_overwrite = existing_len >= 80 and len(data) < 40
                is_shrinking_overwrite = existing_len >= 200 and len(data) < max(40, existing_len // 5)
                no_auto = fp.name.lower() in self._NO_AUTO_APPEND or is_source_file
                def _no_auto_explanation() -> str:
                    # Never suggest force=true here. A model that's already
                    # thrashing (e.g. resending a stale header-only payload,
                    # or a stray fenced-code scrap, across several turns)
                    # will take force=true at face value and genuinely
                    # truncate/corrupt the file if offered it as an escape
                    # hatch — edit/append is always the correct fix here,
                    # so don't hand it a worse one. Wording differs by WHY
                    # auto-append is disabled, since these are genuinely
                    # different situations for the model to reason about.
                    if is_source_file:
                        return (
                            " This is a source-code file — tiny/partial payloads (including "
                            "a bare code-fence marker like ```python with no body) are never "
                            "auto-appended here, since that silently produces invalid syntax. "
                            "If you meant to add code, use edit (old_text/new_text) or "
                            "write(..., append=true) with the REAL content, not just a fence. "
                            "If you didn't mean to touch this file at all, drop this call "
                            "and act on the real target instead."
                        )
                    return (
                        " This file is a plan/spec artifact — tiny scraps are never "
                        "auto-appended here. This content also does not belong in "
                        "IMPLEMENTATION/TESTING/REVIEW phases; if you didn't mean to "
                        "touch this file, drop this call entirely and act on the real "
                        "target instead (e.g. run the test command, or edit source code)."
                    )

                if is_tiny_overwrite or is_shrinking_overwrite:
                    if looks_like_addition and not no_auto:
                        # Small, section-shaped content against a large existing
                        # file is almost always an attempt to ADD a section, not
                        # replace the file. Auto-append instead of forcing the
                        # model into an endless failed-retry loop it rarely
                        # recovers from cleanly. Use force=true to really truncate.
                        append = True
                        auto_appended = True
                    elif is_tiny_overwrite:
                        if no_auto:
                            extra = _no_auto_explanation()
                            force_hint = ""
                        else:
                            extra = ""
                            force_hint = " or write(..., force=true) if you really mean to truncate the file."
                        return ToolResult(
                            "",
                            error=(
                                f"Refused tiny overwrite of {path} "
                                f"(existing ~{existing_len} bytes, new payload {len(data)} chars). "
                                f"Use edit (old_text/new_text), write(..., append=true) to extend,"
                                f"{force_hint}"
                                f"{extra}"
                            ),
                            is_error=True,
                        )
                    else:
                        if no_auto:
                            extra = _no_auto_explanation()
                            force_hint = ""
                        else:
                            extra = ""
                            force_hint = " or write(..., force=true) if you really mean to truncate the file."
                        return ToolResult(
                            "",
                            error=(
                                f"Refused shrinking overwrite of {path} "
                                f"(existing ~{existing_len} bytes → {len(data)} chars). "
                                f"Pass the complete new file content, use edit/append,"
                                f"{force_hint}"
                                f"{extra}"
                            ),
                            is_error=True,
                        )
            # Pre-write AST gate for Python sources. Applies to BOTH overwrite
            # and append unless force=true. Append previously skipped the gate
            # so a model could stream pure garbage via append=true and leave
            # invalid Python on disk with zero refusal (stress T4). The cost
            # is that mid-stream partial chunks must use force=true; complete
            # functions/sections appended to a valid file still parse and pass.
            if not force and Path(path or "").suffix.lower() in {".py", ".pyi"}:
                if append and fp.is_file() and fp.stat().st_size > 0:
                    try:
                        existing = fp.read_text(encoding="utf-8")  # strict — refuse dirty files
                    except UnicodeDecodeError as e:
                        return ToolResult(
                            "",
                            error=(
                                f"REFUSED append to {path}: existing file is not valid UTF-8 "
                                f"({e}). Fix encoding first (or rewrite with force=true)."
                            ),
                            is_error=True,
                        )
                    if not data.startswith("\n"):
                        candidate = existing + "\n" + data
                    else:
                        candidate = existing + data
                else:
                    candidate = data
                try:
                    ast.parse(candidate)
                except SyntaxError as e:
                    return ToolResult(
                        "",
                        error=(
                            f"REFUSED write to {path}: resulting content is not valid Python "
                            f"({e.msg} at line {e.lineno}). Re-send a complete, syntactically "
                            f"valid source payload. For a deliberate partial/incomplete chunk, "
                            f"pass force=true (the only AST bypass). Do NOT use append to smuggle "
                            f"invalid fragments — append is validated against the full file."
                        ),
                        is_error=True,
                    )
            mode = "a" if append else "w"
            if append and mode == "a" and fp.is_file() and fp.stat().st_size > 0 and not data.startswith("\n"):
                data = "\n" + data
            with open(fp, mode, encoding="utf-8", newline="\n") as f:
                f.write(data)
            with ReadTool._cache_lock:
                ReadTool._cache.pop(str(fp), None)
            size = fp.stat().st_size if fp.is_file() else 0
            notes = []
            if auto_appended:
                notes.append(
                    "NOTE: content looked like a section addition against a large existing file, "
                    "so it was auto-appended instead of failing. Use force=true next time if you "
                    "genuinely intend to truncate the file."
                )
            if len(data) >= self.WARN_CHARS:
                notes.append(
                    f"WARN: payload {len(data)} chars is large — prefer smaller chunks + append=true "
                    "to avoid model/stream truncation."
                )
            stripped = data.rstrip()
            if stripped.count("```") % 2 == 1:
                notes.append("WARN: odd number of ``` fences — content may be truncated; append the rest.")
            if any(stripped.endswith(x) for x in ("{", "(", "[", ",")) or stripped.endswith("className="):
                notes.append("WARN: content ends mid-syntax — likely truncated; continue with append=true.")
            msg = f"{'Appended' if append else 'Written'} {len(data)} chars ({size} bytes on disk) to {path}"
            if notes:
                msg += "\n" + "\n".join(notes)
            return ToolResult(msg)
        except Exception as e:
            return ToolResult("", error=str(e), is_error=True)


class EditTool(Tool):
    name = "edit"
    description = "Replace old_text with new_text in a file. Use replace_all to replace all occurrences."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["path", "old_text", "new_text"],
    }

    @staticmethod
    def _diagnose_no_match(old_text: str, original: str) -> str:
        if not old_text:
            return " (old_text is empty)"
        if "\r\n" in old_text:
            return " (old_text contains \\r\\n but the file's line endings are normalized to \\n — strip \\r)"
            
        import difflib
        lines = original.splitlines(keepends=True)
        old_lines = old_text.splitlines(keepends=True)
        
        if not old_lines or len(lines) == 0:
            return " (file or old_text is practically empty)"
            
        best_ratio = 0.0
        best_match = ""
        window_size = len(old_lines)
        
        # Slide a window of the same line-length as old_text across the file
        for i in range(max(1, len(lines) - window_size + 1)):
            window = "".join(lines[i:i+window_size])
            ratio = difflib.SequenceMatcher(None, old_text, window).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = window
                
        if best_ratio > 0.6:
            return (
                f"\n\nold_text was NOT FOUND exactly. However, I found a very similar block in the file (similarity {best_ratio:.0%}). "
                f"You probably missed some leading spaces, made a typo, or have a quote difference.\n\n"
                f"--- HERE IS THE EXACT TEXT FROM THE FILE ---\n"
                f"{best_match}\n"
                f"--------------------------------------------\n"
                f"Copy the text above EXACTLY as your old_text in your next try."
            )
            
        return (
            " (no match at all, and no similar blocks found — old_text likely doesn't exist in this "
            "file; re-view the file to confirm the target text is actually there)"
        )

    def execute(self, path: str, old_text: str, new_text: str, replace_all: bool = False) -> ToolResult:
        try:
            fp = self._safe_path(path)
            if not fp.exists():
                return ToolResult("", error=f"File not found: {path}", is_error=True)
            try:
                with open(fp, "r", encoding="utf-8") as f:  # strict — no silent U+FFFD mangling
                    original = f.read()
            except UnicodeDecodeError as e:
                return ToolResult(
                    "",
                    error=(
                        f"REFUSED edit of {path}: file is not valid UTF-8 ({e}). "
                        f"Fix encoding before editing."
                    ),
                    is_error=True,
                )
            if old_text not in original:
                hint = self._diagnose_no_match(old_text, original)
                return ToolResult("", error=f"old_text not found in {path}{hint}", is_error=True)
            total_occurrences = original.count(old_text)
            ambiguous_warning = ""
            # Always strip a balanced ```lang ... ``` wrapper from new_text.
            # Previously this only ran on the replace_all=True path, so a
            # single-occurrence edit could inject fenced content (RC-1 gap)
            # and leave invalid syntax on disk. Same narrow helper WriteTool
            # uses — only fires on an unambiguous whole-payload fence pair.
            new_text = WriteTool._strip_balanced_code_fence(new_text, path)
            if replace_all:
                count = total_occurrences
                new_content = original.replace(old_text, new_text)
            else:
                new_content = original.replace(old_text, new_text, 1)
                count = 1
                if total_occurrences > 1:
                    ambiguous_warning = (
                        f"\n\n⚠ WARNING: old_text matched {total_occurrences} times in {path}, "
                        f"but only the FIRST occurrence was replaced (replace_all was not set). "
                        f"If you intended to edit a different occurrence, include more surrounding "
                        f"context in old_text to make it unique, or pass replace_all=true if you "
                        f"meant to replace all of them."
                    )
            # Post-splice AST gate for Python: fence-stripping new_text alone is
            # not enough — splicing a locally-valid fragment can still break the
            # file (bare `def broken(:`, dedent attacks, etc.). Refuse before
            # write so EditTool matches WriteTool's "no invalid .py on disk" rule.
            if Path(path or "").suffix.lower() in {".py", ".pyi"}:
                try:
                    ast.parse(new_content)
                except SyntaxError as e:
                    return ToolResult(
                        "",
                        error=(
                            f"REFUSED edit of {path}: result would not be valid Python "
                            f"({e.msg} at line {e.lineno}). Adjust old_text/new_text so the "
                            f"full file remains syntactically valid."
                        ),
                        is_error=True,
                    )
            with open(fp, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_content)
            with ReadTool._cache_lock:
                ReadTool._cache.pop(str(fp), None)
            diff_lines = list(difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(),
                fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
            ))
            diff_txt = "\n".join(diff_lines[:80])
            if len(diff_lines) > 80:
                diff_txt += f"\n… [{len(diff_lines)-80} more diff lines]"
            msg = (
                f"Replaced {count} occurrence(s) in {path} ({len(new_content)} chars now)\n"
                f"--- diff ---\n{diff_txt}"
                f"{ambiguous_warning}"
            )
            return ToolResult(msg)
        except Exception as e:
            return ToolResult("", error=str(e), is_error=True)


class SearchTool(Tool):
    name = "search"
    description = "Search file contents with a regex pattern."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern"},
            "path": {"type": "string", "default": ".", "description": "Base directory"},
        },
        "required": ["pattern"],
    }

    SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", "dist", "build"}
    SKIP_EXTS = {".pyc", ".exe", ".dll", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".tar", ".gz", ".mp3", ".mp4", ".woff", ".ttf"}

    def execute(self, pattern: str, path: str = ".") -> ToolResult:
        try:
            if not pattern or not str(pattern).strip():
                return ToolResult(
                    "",
                    error=(
                        "pattern is empty. This usually means the model's tool call "
                        "was malformed (a field got dropped or merged into another "
                        "argument) rather than an intentional search. Re-issue the "
                        "call with a real regex pattern."
                    ),
                    is_error=True,
                )
            base = self._safe_path(path)
            # ReDoS guard: nested quantifiers on the same group (e.g. (a+)+$)
            # can hang Python's `re` engine indefinitely on crafted input.
            # Reject high-risk patterns up front; also cap pattern length.
            pat = str(pattern)
            if len(pat) > 500:
                return ToolResult(
                    "",
                    error="pattern too long (max 500 chars) — refuse possible ReDoS / abuse.",
                    is_error=True,
                )
            if re.search(r"\([^)]*[+*][^)]*\)[+*]", pat) or re.search(r"\(\w\+\)\+", pat):
                return ToolResult(
                    "",
                    error=(
                        "pattern looks like nested quantifiers (ReDoS risk). "
                        "Simplify the regex (avoid (a+)+ / (a*)* forms)."
                    ),
                    is_error=True,
                )
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                return ToolResult("", error=f"invalid regex: {e}", is_error=True)
            matches: List[str] = []
            if base.is_file():
                try:
                    with open(base, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                rel = base.relative_to(self.cwd)
                                matches.append(f"{rel}:{i}:{line.rstrip()}")
                                if len(matches) >= 100:
                                    break
                except Exception as e:
                    return ToolResult("", error=f"could not read file '{path}': {e}", is_error=True)
                if not matches:
                    return ToolResult(f"No matches for pattern: {pattern} (searched single file: {path})")
                return ToolResult("\n".join(matches))
            if not base.is_dir():
                return ToolResult("", error=f"path '{path}' does not exist", is_error=True)
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS and not d.startswith(".")]
                for fn in files:
                    if any(fn.endswith(ext) for ext in self.SKIP_EXTS):
                        continue
                    fp = Path(root) / fn
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as f:
                            for i, line in enumerate(f, 1):
                                if rx.search(line):
                                    rel = fp.relative_to(self.cwd)
                                    matches.append(f"{rel}:{i}:{line.rstrip()}")
                                    if len(matches) >= 100:
                                        break
                    except Exception:
                        pass
                    if len(matches) >= 100:
                        break
                if len(matches) >= 100:
                    break
            if not matches:
                return ToolResult(f"No matches for pattern: {pattern}")
            return ToolResult("\n".join(matches))
        except Exception as e:
            return ToolResult("", error=str(e), is_error=True)


class GlobTool(Tool):
    name = "glob"
    description = "Find files matching a glob pattern (e.g., '**/*.py')."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
        },
        "required": ["pattern"],
    }

    def execute(self, pattern: str, path: str = ".") -> ToolResult:
        try:
            if not pattern or not str(pattern).strip():
                return ToolResult(
                    "",
                    error=(
                        "pattern is empty. This usually means the model's tool call "
                        "was malformed (a field got dropped or merged into another "
                        "argument) rather than an intentional glob. Re-issue the "
                        "call with a real glob pattern, e.g. '**/*.py'."
                    ),
                    is_error=True,
                )
            base = self._safe_path(path)
            if base.is_file():
                if fnmatch.fnmatch(base.name, pattern):
                    return ToolResult(str(base.relative_to(self.cwd)))
                return ToolResult(
                    f"'{path}' is a file, not a directory, and its name does not match "
                    f"pattern '{pattern}'. Glob matches filenames within a directory tree; "
                    f"pass the containing directory instead if you meant to search within it."
                )
            if not base.is_dir():
                return ToolResult("", error=f"path '{path}' does not exist", is_error=True)
            _GLOB_EXCLUDE_PARTS = {".neon_worktrees", "__pycache__", ".git", "node_modules"}
            matches = [
                m for m in base.rglob(pattern)
                if not any(part in _GLOB_EXCLUDE_PARTS for part in m.parts)
            ]
            if not matches:
                return ToolResult("No files found")
            rels = sorted(str(m.relative_to(self.cwd)) for m in matches)
            return ToolResult("\n".join(rels))
        except Exception as e:
            return ToolResult("", error=str(e), is_error=True)


class LsTool(Tool):
    name = "ls"
    description = "List files in a directory."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
        },
        "required": [],
    }

    _cache: Dict[str, str] = {}
    _cache_lock = threading.Lock()

    def execute(self, path: str = ".") -> ToolResult:
        try:
            base = self._safe_path(path)
            if not base.exists():
                return ToolResult("", error=f"Directory not found: {path}", is_error=True)
            items = []
            for p in sorted(base.iterdir()):
                icon = "📁" if p.is_dir() else "📄"
                items.append(f"{icon} {p.name}")
            listing = "\n".join(items)

            key = str(base)
            with self._cache_lock:
                was_same = self._cache.get(key) == listing
                self._cache[key] = listing

            if was_same:
                listing = (
                    f"[unchanged since your last listing of {path} this session]\n"
                    + listing
                )
            return ToolResult(listing)
        except Exception as e:
            return ToolResult("", error=str(e), is_error=True)


class RunTool(Tool):
    """Structural replacement for the old free-text BashTool.

    ROOT CAUSE this addresses: LLMs default to Unix shell syntax regardless
    of system-prompt instructions, because "developer shell" in their
    training data is overwhelmingly Unix-flavored. A free-text `command`
    field gives the model an open channel to express that bias; any fix
    that inspects/blocks specific strings after the fact (a blocklist of
    "ls", "cat", "head", ...) is symptomatic — it can only catch patterns
    already enumerated, and the space of Unix-shaped strings a model might
    emit is effectively open-ended.

    This tool removes the channel instead of policing it: the schema has
    a `program` enum and a plain `args` array — there is no field in which
    pipes, &&, redirection, or `cd` could even be expressed. Execution uses
    subprocess with shell=False against a resolved real executable path, so
    even if a stray shell-metacharacter-looking token ends up inside an
    `args` string, it is passed as an inert literal argv element to the
    target program's own argument parser — never interpreted as shell
    syntax, because no shell is ever invoked.

    File inspection (ls/cat/head/grep equivalents) is intentionally NOT
    reimplemented here — that's already covered by ReadTool/LsTool/
    SearchTool/GlobTool. This tool's job is narrowed to running known dev
    programs (test runners, package managers, interpreters, VCS).
    """

    name = "run"
    description = (
        "Run a known development program (test runner, package manager, "
        "interpreter, or VCS) with explicit arguments. This is NOT a shell: "
        "there is no pipe, redirect, `&&`, `cd`, or Unix/Windows shell "
        "syntax of any kind — only a program name and an argv-style list "
        "of arguments. Example: program='pytest', args=['-v', '-k', 'foo']. "
        "For file inspection (listing/reading/searching files) use the "
        "read/ls/search/glob tools instead — this tool does not do that. "
        "Destructive git operations (push --force, reset --hard, clean -f, "
        "branch -D, checkout -f) are refused outright, with no confirmation "
        "path — do not retry them with different flags."
    )
    parameters = {
        "type": "object",
        "properties": {
            "program": {
                "type": "string",
                "enum": [
                    "python", "pytest", "pip", "npm", "pnpm", "npx", "node", "git",
                    "cargo", "go", "make", "ruff", "black", "flake8", "mypy",
                    "yarn", "bun",
                    "flutter", "dart",
                    "gradle", "gradlew",
                    "xcodebuild", "pod", "xcrun",
                    "eas",
                ],
                "description": (
                    "Which program to run. No shell — pick exactly one program. "
                    "Note: 'make' executes recipe commands from the project's "
                    "Makefile, which can run arbitrary shell-equivalent logic "
                    "defined in that file — treat it with the same caution as "
                    "running untrusted project code, not as a fixed linter. "
                    "Mobile/native programs (flutter, dart, gradle, gradlew, "
                    "xcodebuild, pod, xcrun, eas) are only usable if the "
                    "underlying SDK is actually installed on THIS machine — "
                    "check the environment capability report shown at session "
                    "start before assuming any of these will resolve. "
                    "xcodebuild/pod/xcrun only exist on macOS, full stop, "
                    "regardless of what is installed."
                ),
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Arguments as a plain list, one item per argv element "
                    "(NOT a single joined string). Example: "
                    '["-m", "pytest", "tests/", "-v"]'
                ),
                "default": [],
            },
            "cwd_subpath": {
                "type": "string",
                "description": (
                    "Optional subdirectory under the project root to run in "
                    "(relative path, e.g. 'backend'). Omit to run at the "
                    "project root. Never an absolute path."
                ),
            },
            "timeout": {"type": "integer", "default": 120},
        },
        "required": ["program", "args"],
    }

    MAX_OUTPUT = 80000

    _REGISTRY: Dict[str, List[str]] = {
        "python": [sys.executable, "python", "python3"],
        "pytest": ["pytest", "py.test"],
        "pip": [sys.executable, "pip", "pip3"],
        "npm": ["npm"],
        "pnpm": ["pnpm"],
        "npx": ["npx"],
        "node": ["node"],
        "git": ["git"],
        "cargo": ["cargo"],
        "go": ["go"],
        "make": ["make"],
        "ruff": ["ruff"],
        "black": ["black"],
        "flake8": ["flake8"],
        "mypy": ["mypy"],
        # NOTE: yarn/bun were already selectable by _manifest_commands()
        # (as the inferred package manager, based on yarn.lock/bun.lockb
        # presence) before this registry entry existed — meaning that
        # branch would return a manager name RunTool could not resolve
        # at all. Adding them here fixes that pre-existing gap, not just
        # adding new capability.
        "yarn": ["yarn"],
        "bun": ["bun"],
        # Mobile/native toolchain. Presence in this registry means "the
        # agent is ALLOWED to try resolving this program" — it does NOT
        # mean the underlying SDK is installed. _resolve_executable still
        # does a real shutil.which()-based lookup and returns a clear
        # "not found" error if the binary genuinely isn't present, same
        # as any other program. See EnvironmentCapabilities for the
        # up-front detection report surfaced to the model at session
        # start, so it doesn't have to discover missing SDKs by trial
        # and error on every single call.
        "flutter": ["flutter"],
        "dart": ["dart"],
        "gradle": ["gradle"],
        # gradlew is NOT a PATH binary — it's a project-local wrapper
        # script (./gradlew or gradlew.bat) committed into Android/Gradle
        # projects. It gets special-cased in _resolve_executable rather
        # than looked up here; this entry exists only so the registry
        # membership check (`program not in self._REGISTRY`) in execute()
        # doesn't reject it before reaching that special case.
        "gradlew": [],
        "xcodebuild": ["xcodebuild"],
        "pod": ["pod"],
        "xcrun": ["xcrun"],
        "eas": ["eas"],
    }

    @staticmethod
    def _windows_shim_variants(path_or_name: str) -> List[str]:
        # On Windows, npm/npx/etc. are typically installed as .cmd/.bat
        # shims rather than bare .exe files. shutil.which() normally
        # resolves these fine because it consults PATHEXT, but PATHEXT is
        # user/environment-configurable — if it's been stripped or
        # customized, shutil.which(name) can return None even though
        # e.g. npm.cmd genuinely exists on PATH. This generates the
        # explicit variants to retry against, both at resolution time
        # (_resolve_executable) and at execution time (the
        # FileNotFoundError fallback in execute(), for the rarer case
        # where resolution succeeds but launching what it found does
        # not — e.g. a stale PATH entry pointing at a removed binary).
        #
        # VERIFIED on real Windows (10.0.19045, Python 3.11.9) via
        # test_windows_shim.py: confirmed _resolve_executable finds
        # npm.CMD/npx.CMD/git.EXE/etc. correctly, INCLUDING under a
        # deliberately stripped PATHEXT (the actual failure mode this
        # fallback targets), and that subprocess.run(shell=False)
        # launches the resolved .CMD files directly with no cmd /c
        # wrapping needed. All RunTool.execute() round-trip calls
        # (git, python, npm, node) passed. See test_windows_shim.py
        # in the repo for the diagnostic script if this needs
        # re-verification after a future change.
        if sys.platform != "win32" or Path(path_or_name).suffix:
            return [path_or_name]
        return [path_or_name + ext for ext in (".cmd", ".exe", ".bat", "")]

    @staticmethod
    def _venv_bin_dir(project_root: Path) -> Optional[Path]:
        # Look for a conventional in-project virtualenv (.venv or venv,
        # the two overwhelmingly common names) at the project root only —
        # not walking up parent directories, since that could pick up an
        # unrelated venv outside the project the agent is scoped to.
        bin_name = "Scripts" if sys.platform == "win32" else "bin"
        for venv_name in (".venv", "venv"):
            candidate = project_root / venv_name / bin_name
            if candidate.is_dir():
                return candidate
        return None

    def _resolve_executable(self, program: str, project_root: Optional[Path] = None,
                             run_cwd: Optional[Path] = None) -> Optional[str]:
        # gradlew is a project-committed wrapper script (./gradlew on
        # Unix/macOS, gradlew.bat on Windows), not something on PATH —
        # it has to be found by walking the filesystem, not shutil.which.
        # Look first relative to wherever this specific run actually
        # executes (run_cwd — e.g. an `android/` subdir for a React
        # Native project, where gradlew conventionally lives), then fall
        # back to the project root, since some Flutter/native-Android
        # layouts keep it at the top level instead.
        if program == "gradlew":
            wrapper_name = "gradlew.bat" if sys.platform == "win32" else "gradlew"
            search_roots = [d for d in (run_cwd, project_root) if d is not None]
            for root in search_roots:
                candidate = root / wrapper_name
                if candidate.is_file():
                    return str(candidate)
            return None

        # Prefer an in-project virtualenv for python/pip specifically, if
        # one exists at the project root. Previously this always resolved
        # python/pip to sys.executable (the interpreter running the agent
        # itself) with zero awareness of a project-local venv — so if the
        # model expected `pip install X` to land in a venv it (or the
        # user) had already set up, it silently installed into the
        # agent's own environment instead. Other programs (git, npm,
        # cargo, etc.) are unaffected — venvs are a Python-specific
        # concept.
        if program in ("python", "pip") and project_root is not None:
            venv_bin = self._venv_bin_dir(project_root)
            if venv_bin is not None:
                candidates = (
                    ["python.exe", "python3.exe"] if sys.platform == "win32" else ["python", "python3"]
                ) if program == "python" else (
                    ["pip.exe", "pip3.exe"] if sys.platform == "win32" else ["pip", "pip3"]
                )
                for name in candidates:
                    candidate = venv_bin / name
                    if candidate.exists():
                        return str(candidate)

        for name in self._REGISTRY.get(program, []):
            if name == sys.executable and Path(name).exists():
                return name
            found = shutil.which(name)
            if found:
                return found
            if sys.platform == "win32":
                # Bare-name lookup failed; retry with explicit shim
                # extensions in case PATHEXT doesn't cover it (see
                # _windows_shim_variants docstring above).
                for variant in self._windows_shim_variants(name):
                    if variant == name:
                        continue
                    found = shutil.which(variant)
                    if found:
                        return found
        return None

    def _safe_cwd(self, cwd_subpath: Optional[str]) -> Path:
        if not cwd_subpath or not str(cwd_subpath).strip():
            return self.cwd
        raw = str(cwd_subpath).strip().replace("\\", "/")
        candidate = Path(raw)
        target = candidate.resolve() if candidate.is_absolute() else (self.cwd / raw).resolve()
        try:
            target.relative_to(self.cwd.resolve())
        except ValueError:
            raise PermissionError(
                f"cwd_subpath '{cwd_subpath}' resolves outside the project folder "
                f"({self.cwd}). Only relative in-project subdirectories are allowed."
            )
        if not target.exists():
            raise FileNotFoundError(f"cwd_subpath '{cwd_subpath}' does not exist under the project root.")
        return target

    # Destructive git operations expressed as argv flags. The old free-text
    # BashTool had a regex gate over the whole shell string for these
    # (see the r"git\s+push\s+.*--force|git\s+reset\s+--hard|..." pattern
    # near the top of the file); that gate was never reimplemented against
    # RunTool's structured args array when the shell string was removed,
    # which left a real gap — a model can currently ask for
    # program="git", args=["push","--force"] and it runs with zero
    # confirmation. This restores an equivalent check against argv
    # elements instead of a command string.
    _GIT_DESTRUCTIVE_SUBCOMMANDS = {"push", "reset", "clean", "branch", "checkout", "rebase"}

    @staticmethod
    def _git_destructive_reason(run_args: List[str]) -> Optional[str]:
        low = [str(a).lower() for a in (run_args or [])]
        if not low:
            return None
        subcmd = low[0]
        if subcmd not in RunTool._GIT_DESTRUCTIVE_SUBCOMMANDS:
            return None
        rest = set(low[1:])
        if subcmd == "push" and ({"--force", "-f", "--force-with-lease"} & rest):
            return "git push --force (or -f/--force-with-lease) can overwrite remote history."
        if subcmd == "reset" and "--hard" in rest:
            return "git reset --hard discards uncommitted local changes irreversibly."
        if subcmd == "clean":
            # Exact forms plus any bundled short-flag token that includes
            # force (-f), e.g. -xfd / -dfx / -fxd — exact-string sets miss
            # alternate orderings of the same letters.
            if ({"-f", "-fd", "-fdx", "--force"} & rest):
                return "git clean -f deletes untracked files irreversibly."
            for tok in rest:
                if tok.startswith("--"):
                    continue
                if tok.startswith("-") and "f" in tok[1:] and tok[1:].isalpha():
                    return "git clean -f deletes untracked files irreversibly."
        if subcmd == "branch" and ({"-d", "-D", "--delete"} & rest):
            return "git branch -D force-deletes a branch, discarding unmerged commits."
        if subcmd == "checkout" and "-f" in rest:
            return "git checkout -f discards uncommitted local changes."
        if subcmd == "rebase" and "--force-rebase" in rest:
            return "git rebase --force-rebase discards the current rebase state."
        return None

    @staticmethod
    def _is_test_invocation(program: str, run_args: List[str]) -> bool:
        program = (program or "").lower()
        low_args = [str(a).lower() for a in (run_args or [])]
        if program == "pytest":
            return True
        if program == "python" and any("pytest" in a or "unittest" in a for a in low_args):
            return True
        if program == "npm" and "test" in low_args:
            return True
        if program == "npx" and any(a in ("jest",) for a in low_args):
            return True
        if program in ("cargo", "go") and "test" in low_args:
            return True
        return False

    @staticmethod
    def _missing_test_path_targets(program: str, run_args: List[str], cwd: Path) -> List[str]:
        """For a pytest-style invocation, return any positional path-like
        arguments that don't exist relative to cwd. Used to short-circuit
        with a clear, specific cause (missing file, likely from an earlier
        failed write) instead of letting the run fail generically on a
        target that was never created — which otherwise looks identical
        to a real test failure and burns the same-arguments retry budget
        without ever telling the model what to actually fix.

        Deliberately conservative: only flags things that look like a
        real file/dir path argument (contains '/' or ends in a known test
        file suffix, or is a bare existing-looking relative segment) and
        skips option flags and their values (anything starting with '-',
        and pytest's own value-taking options like -k/-m/--maxfail so we
        never misread an expression like '-k test_foo' as a path).
        """
        program = (program or "").lower()
        if program not in ("pytest",) and not (
            program == "python" and any("pytest" in str(a).lower() for a in run_args)
        ):
            return []
        skip_next = False
        # pytest options that consume the following token as a value, not
        # a path — never treat these values as path targets.
        value_flags = {"-k", "-m", "--maxfail", "-n", "--timeout", "--cov",
                        "--junitxml", "--html", "-p"}
        # Flags whose PATH VALUE is embedded via '=' rather than passed as
        # a separate argv element. These were previously falling through
        # the blanket `if a.startswith("-"): continue` below and never
        # getting existence-checked at all — a truncated or misspelled
        # --ignore=backend/tests/test_nim_endp (missing the real suffix)
        # would silently match nothing, so pytest still collected the
        # file the model believed it had excluded, producing a failure
        # that looks like a real test failure but is actually a
        # never-validated exclusion path. Checking these here surfaces
        # that cause immediately instead of burning a retry cycle on a
        # misleading generic failure.
        embedded_path_flags = ("--ignore=", "--ignore-glob=", "--rootdir=", "--confcutdir=")
        missing: List[str] = []
        for a in run_args:
            a = str(a)
            if skip_next:
                skip_next = False
                continue
            if a in value_flags:
                skip_next = True
                continue
            embedded = next((pfx for pfx in embedded_path_flags if a.startswith(pfx)), None)
            if embedded:
                path_part = a[len(embedded):]
                if not path_part:
                    continue
                candidate = (cwd / path_part) if not Path(path_part).is_absolute() else Path(path_part)
                if not candidate.exists():
                    missing.append(a)
                continue
            if a.startswith("-"):
                continue
            # A pytest node id can carry ::TestClass::test_name — only
            # check the file-path portion before the first '::'.
            path_part = a.split("::", 1)[0]
            if not path_part:
                continue
            candidate = (cwd / path_part) if not Path(path_part).is_absolute() else Path(path_part)
            looks_path_like = ("/" in path_part or "\\" in path_part
                                or path_part.endswith(".py") or path_part in (".", "tests"))
            if looks_path_like and not candidate.exists():
                missing.append(path_part)
        return missing

    # ── Optional Docker isolation ────────────────────────────────────────
    # Everything above this point already closes the injection/traversal
    # surface (no shell, argv-only, program allowlist, cwd containment).
    # What it does NOT do is give the executed program its own filesystem/
    # network/process boundary — a legitimate `pytest` run (or a test file
    # the generation agents wrote) still executes with the real OS user's
    # full privileges: real network access, ability to touch anything that
    # user can touch outside project_dir via absolute paths embedded in
    # code, resource exhaustion, etc. This wraps execution in `docker run`
    # when available and opted into; when it isn't, behavior is unchanged
    # from before — this is additive, not a replacement for the argv-only
    # design above.
    _SANDBOX_IMAGE = os.environ.get("NEON_SANDBOX_IMAGE", "neon-sandbox:latest")

    @classmethod
    def _docker_available(cls) -> bool:
        if os.environ.get("NEON_SANDBOX_DOCKER", "").strip().lower() not in ("1", "true", "yes"):
            return False
        return shutil.which("docker") is not None

    @classmethod
    def _wrap_argv_for_docker(cls, argv: List[str], cwd: Path, project_root: Path) -> Optional[List[str]]:
        """Wrap a resolved argv in `docker run` against the project root,
        network-disabled, filesystem-scoped to the project via a bind
        mount, non-root, with hard resource limits. Returns None if the
        program isn't one we know how to translate inside the container
        (host-path-resolved binaries like a venv interpreter or a
        gradlew wrapper don't make sense to run inside an image that
        wasn't built with that exact venv/SDK) — callers fall back to
        direct execution in that case.
        """
        if not argv:
            return None
        host_bin = Path(argv[0])
        # Only programs resolvable by bare name inside the container image
        # (not a host-specific absolute path like a project .venv's
        # python, or a project-local gradlew script) get containerized.
        # Those two cases still run directly — they depend on host-only
        # state a generic image wouldn't have anyway.
        if host_bin.is_absolute():
            return None
        try:
            rel_cwd = cwd.resolve().relative_to(project_root.resolve())
        except ValueError:
            return None
        container_cwd = "/work" if str(rel_cwd) == "." else f"/work/{rel_cwd.as_posix()}"
        return [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", "1g",
            "--cpus", "2",
            "--pids-limit", "256",
            "--user", "1000:1000",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "-v", f"{project_root.resolve()}:/work",
            "-w", container_cwd,
            cls._SANDBOX_IMAGE,
            *argv,
        ]

    def _run_argv(self, argv: List[str], cwd: Path, timeout: int) -> "subprocess.CompletedProcess":
        """Single execution path used by both the primary run and the
        Windows-shim fallback below — tries Docker first when opted in
        and applicable, falls back to direct subprocess.run otherwise.
        """
        if self._docker_available():
            wrapped = self._wrap_argv_for_docker(argv, cwd, self.cwd)
            if wrapped is not None:
                try:
                    return subprocess.run(
                        wrapped, shell=False, cwd=str(self.cwd),
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=timeout,
                    )
                except FileNotFoundError:
                    # docker itself vanished mid-session or image is
                    # missing — fall through to direct execution rather
                    # than hard-failing the whole tool call.
                    pass
        return subprocess.run(
            argv, shell=False, cwd=str(cwd),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )

    def execute(self, program: str = "", args: Optional[List[str]] = None,
                cwd_subpath: Optional[str] = None, timeout: int = 120,
                _confirmed_destructive: bool = False) -> ToolResult:
        try:
            program = (program or "").strip().lower()
            if program not in self._REGISTRY:
                return ToolResult(
                    "",
                    error=(
                        f"Unknown program '{program}'. Allowed: "
                        f"{', '.join(sorted(self._REGISTRY.keys()))}. There is no "
                        "general shell here — pick one of these, or use the "
                        "read/ls/search/glob tools for file inspection."
                    ),
                    is_error=True,
                )
            if args is None:
                args = []
            if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
                return ToolResult(
                    "",
                    error=(
                        "args must be a JSON array of strings, one per argv element — "
                        'not a single command string. Example: ["-m", "pytest", "-v"]'
                    ),
                    is_error=True,
                )
            args = [str(a) for a in args]

            if program == "git" and not _confirmed_destructive:
                reason = self._git_destructive_reason(args)
                if reason:
                    return ToolResult(
                        "",
                        error=(
                            f"Blocked: {reason} This tool has no confirmation "
                            "step, so potentially destructive git operations are "
                            "refused outright rather than run unconfirmed. If this "
                            "is genuinely intended, ask the user to run it manually."
                        ),
                        is_error=True,
                    )

            try:
                cwd = self._safe_cwd(cwd_subpath)
            except (PermissionError, FileNotFoundError) as e:
                return ToolResult("", error=str(e), is_error=True)

            if self._is_test_invocation(program, args):
                missing = self._missing_test_path_targets(program, args, cwd)
                if missing:
                    embedded_prefixes = ("--ignore=", "--ignore-glob=", "--rootdir=", "--confcutdir=")
                    flag_misses = [m for m in missing if m.startswith(embedded_prefixes)]
                    target_misses = [m for m in missing if m not in flag_misses]
                    parts = []
                    if target_misses:
                        parts.append(
                            f"Target path(s) do not exist, so no tests can run: "
                            f"{', '.join(target_misses)}. This usually means an earlier "
                            f"write to that path failed or was never sent — check "
                            f"for a prior write/edit error on this exact path before "
                            f"retrying the same test command again."
                        )
                    if flag_misses:
                        parts.append(
                            f"The following flag(s) point at a path that does not exist, "
                            f"so they will silently exclude NOTHING (pytest treats a "
                            f"non-matching --ignore path as a no-op, not an error) — the "
                            f"test(s) you meant to exclude will still be collected and can "
                            f"fail: {', '.join(flag_misses)}. Re-view the actual file/dir "
                            f"name (it is very likely truncated or misspelled) and re-send "
                            f"the full, exact path."
                        )
                    return ToolResult("", error=" ".join(parts), is_error=True)

            resolved = self._resolve_executable(program, project_root=self.cwd, run_cwd=cwd)
            if not resolved:
                if program == "gradlew":
                    return ToolResult(
                        "",
                        error=(
                            f"No gradlew wrapper script found in {cwd} or {self.cwd}. "
                            "gradlew is a project-committed file (./gradlew or "
                            "gradlew.bat), not something installed globally — if this "
                            "project doesn't have one yet, use program='gradle' "
                            "instead (requires Gradle + a JDK installed on this "
                            "machine), or generate the wrapper first."
                        ),
                        is_error=True,
                    )
                return ToolResult(
                    "",
                    error=(
                        f"Could not find an executable for '{program}' on PATH "
                        f"(host OS: {platform.system()}). It may not be installed. "
                        f"For mobile/native toolchain programs, this is expected "
                        f"unless the corresponding SDK is present on this exact "
                        f"machine — see the environment capability report."
                    ),
                    is_error=True,
                )
            if program == "gradlew" and sys.platform != "win32" and not os.access(resolved, os.X_OK):
                return ToolResult(
                    "",
                    error=(
                        f"Found {resolved} but it is not marked executable. Run "
                        f"program='git', args=['update-index', '--chmod=+x', "
                        f"'gradlew'] or have the user run `chmod +x gradlew` — this "
                        f"tool will not silently change file permissions."
                    ),
                    is_error=True,
                )

            timeout = max(5, min(int(timeout or 120), 600))
            argv = [resolved, *args]

            try:
                result = self._run_argv(argv, cwd, timeout)
            except FileNotFoundError:
                result = None
                last_err = None
                for candidate in self._windows_shim_variants(resolved):
                    try:
                        result = self._run_argv([candidate, *args], cwd, timeout)
                        break
                    except FileNotFoundError as e:
                        last_err = e
                if result is None:
                    return ToolResult(
                        "",
                        error=(
                            f"Found '{program}' on PATH as {resolved!r} but could not "
                            f"execute it. Underlying error: {last_err}"
                        ),
                        is_error=True,
                    )

            output = result.stdout or ""
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if len(output) > self.MAX_OUTPUT:
                half = self.MAX_OUTPUT // 2
                output = output[:half] + f"\n\n… [truncated {len(output)} chars] …\n\n" + output[-half:]
            output += f"\n[exit code: {result.returncode}]"

            digest = ""
            if self._is_test_invocation(program, args):
                all_lines = [ln for ln in output.splitlines() if ln.strip()]
                digest_lines = all_lines[-40:]
                # Coverage summary lines (pytest-cov's "TOTAL ... N%", go's
                # "coverage: N% of statements", etc.) are what the gate now
                # needs to check real coverage, but they can print earlier
                # than the last 40 lines when a failing run's traceback
                # tail is long — the last-40 window exists to bound prompt
                # size, not to be a correctness-relevant cutoff. Explicitly
                # keep any coverage-looking line even if it fell outside
                # that window, rather than widening the whole digest (which
                # would bloat every test-run prompt just to cover this rare
                # case).
                cov_pattern = re.compile(
                    r"(^TOTAL\s+(?:\S+\s+)*?\d+(?:\.\d+)?%\s*$"
                    r"|coverage:\s*\d+(?:\.\d+)?%\s+of\s+statements"
                    r"|All files\s*\|\s*\d+(?:\.\d+)?"
                    r"|\d+(?:\.\d+)?%\s+coverage)",
                )
                if not any(cov_pattern.search(ln) for ln in digest_lines):
                    for ln in reversed(all_lines[:-40]):
                        if cov_pattern.search(ln):
                            digest_lines = [ln] + digest_lines
                            break
                digest = (
                    f"program: {program} {' '.join(args)}\nexit: {result.returncode}\n"
                    + "\n".join(digest_lines)
                )
            failed = result.returncode != 0
            err_summary = ""
            if failed:
                # Give the same failure-tail visibility to .error that the
                # model already sees in .output, so the [BLOCKED REPEAT]/
                # [BLOCKED SPAM] guidance messages (which quote .error, not
                # .output) don't show an empty string for a real failure.
                tail_lines = [ln for ln in output.splitlines() if ln.strip()]
                err_summary = "\n".join(tail_lines[-15:])
                # A collection/import error (ModuleNotFoundError, ImportError,
                # "ERROR collecting") looks, in the tail-line summary, exactly
                # like a normal assertion failure — same non-zero exit code,
                # same generic "call one tool to fix it" downstream framing.
                # But the fix is completely different: an assertion failure
                # means the code under test is wrong; a collection error
                # means required package files (e.g. backend/__init__.py)
                # don't exist yet, so pytest never even reached the test
                # body. Observed failure mode this addresses: on a fresh
                # project, a test file importing from a package that hasn't
                # been scaffolded yet fails with ModuleNotFoundError, and
                # without this flag that reads identically to "my
                # implementation has a bug" — the model has no signal to
                # go create __init__.py instead of re-examining logic that
                # was never actually exercised.
                if self._is_test_invocation(program, args):
                    collection_err_pattern = re.compile(
                        r"ModuleNotFoundError|ImportError|ERROR collecting|"
                        r"error(?:s)? during collection", re.IGNORECASE
                    )
                    if collection_err_pattern.search(output):
                        err_summary = (
                            "[COLLECTION/IMPORT ERROR — not an assertion failure] "
                            "pytest could not even import the module(s) under test, "
                            "so no test logic ran at all. This almost always means "
                            "a required package file is missing (e.g. an __init__.py "
                            "in a package directory, or the module file itself was "
                            "never written/renamed). Do NOT treat this as 'the "
                            "implementation is wrong' — check with the read/ls tool "
                            "which package files actually exist on disk, create "
                            "whichever ones are missing, then re-run.\n" + err_summary
                        )
            return ToolResult(output, error=err_summary, is_error=failed, test_digest=digest)
        except subprocess.TimeoutExpired:
            return ToolResult("", error=f"'{program}' timed out after {timeout}s", is_error=True)
        except Exception as e:
            return ToolResult("", error=f"{program} failed: {e}", is_error=True)


class ProductionValidatorTool(Tool):
    """Inspect and execute a generated project instead of trusting its shape.

    This is deliberately opinionated: a project is not production-ready merely
    because it has directories, Docker files, or an interface document. The
    validator looks for real source and test files, unresolved placeholder
    implementations, and the commands declared by the project's own manifest.
    Commands are routed through RunTool, so validation never becomes an
    arbitrary shell escape hatch.
    """

    name = "validate_project"
    description = (
        "Validate the project as working software. Detect empty/stub/placeholder "
        "implementations, discover the project's real test/build commands from "
        "its manifests, run safe checks, and return concrete failures to fix. "
        "Use this before claiming implementation, testing, or verification is complete."
    )
    parameters = {
        "type": "object",
        "properties": {
            "run_checks": {
                "type": "boolean",
                "default": True,
                "description": "Run discovered test/build/compile checks, not just static inspection.",
            },
            "timeout": {
                "type": "integer",
                "default": 180,
                "description": "Maximum seconds per discovered check.",
            },
        },
        "required": [],
    }

    _EXCLUDED_DIRS = {
        ".git", ".neon_worktrees", ".venv", "venv", "node_modules",
        "__pycache__", ".pytest_cache", "dist", "build", "coverage",
        ".next", ".nuxt", "target", "vendor",
    }
    _SOURCE_SUFFIXES = {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".cs",
        ".cpp", ".cc", ".c", ".h", ".hpp",
    }
    _TEST_NAME_RE = re.compile(
        r"(^test_.*|.*_test|.*\.test|.*\.spec|tests?|specs?)",
        re.IGNORECASE,
    )
    _PLACEHOLDER_RE = re.compile(
        r"(?:TODO\s*:\s*(?:implement|complete|finish|write)|"
        r"FIXME\s*:\s*(?:implement|complete|finish)|"
        r"not\s+implemented|coming\s+soon|lorem\s+ipsum|"
        r"replace\s+this\s+(?:with|by)|your[_ -]?api[_ -]?key|"
        r"example\s+only|demo\s+only|placeholder)",
        re.IGNORECASE,
    )
    _STUB_RE = re.compile(
        r"(?:raise\s+NotImplementedError|throw\s+new\s+Error\(['\"]"
        r"(?:not implemented|todo))",
        re.IGNORECASE | re.MULTILINE,
    )
    # Trivial-assertion detector: a test file can dodge the placeholder/stub
    # regexes entirely by asserting something that can never fail
    # (assert True, assert 1, self.assertTrue(True), expect(true).toBe(true)).
    # These pass pytest/jest with exit code 0 and look like "real tests" to
    # every other check in this tool, which is exactly how a scaffold
    # masquerades as a tested, verified feature.
    _TRIVIAL_ASSERT_RE = re.compile(
        r"(?:assert\s+(?:True|1)\s*(?:[,)\n]|$)|"
        r"assertTrue\s*\(\s*True\s*[,)]|"
        r"assertEqual\s*\(\s*True\s*,\s*True\s*[,)]|"
        r"expect\s*\(\s*true\s*\)\s*\.\s*to(?:Be|Equal)\s*\(\s*true\s*\)|"
        r"expect\s*\(\s*1\s*\)\s*\.\s*to(?:Be|Equal)\s*\(\s*1\s*\))",
        re.IGNORECASE | re.MULTILINE,
    )
    _TEST_FUNC_RE = re.compile(
        r"(?:^|\n)\s*(?:def\s+test_\w+\s*\(|it\s*\(\s*['\"]|test\s*\(\s*['\"])",
        re.IGNORECASE,
    )
    _ASSERT_CALL_RE = re.compile(
        r"\bassert\b|\bexpect\s*\(|\.should\.|\bassert_(?:equal|true|false|raises)\b",
        re.IGNORECASE,
    )
    # Shallow-function detector: a function/method whose entire body is a
    # single trivial statement (pass, ..., or a bare/literal return) with no
    # branching, loop, computation, or call to anything else. This is the
    # code-side twin of the trivial assertion above — it catches an
    # "implementation" that does nothing. Conservative on purpose (only
    # matches genuinely one-line bodies) so real tiny getters aren't
    # penalized individually; the ratio check below is what flags a file
    # that is *mostly* built from this pattern.
    _SHALLOW_BODY_RE = re.compile(
        r"(?:^|\n)([ \t]*)(?:def|function|const)\s+\w+\s*(?:\([^)]*\))?\s*"
        r"(?:->\s*\w+\s*)?[:{]?\s*\n"
        r"\1[ \t]+(?:pass|\.\.\.|return\s*(?:None|null|undefined|\{\s*\}|\[\s*\]|"
        r"true|false|True|False|0|1|['\"][^'\"]{0,40}['\"])?\s*;?\s*)\n",
        re.IGNORECASE,
    )
    _FUNC_DEF_RE = re.compile(r"(?:^|\n)\s*(?:def|function)\s+\w+\s*\(", re.IGNORECASE)

    # ── Security static checks ──────────────────────────────────────────
    # These are intentionally narrower-scope than the placeholder/stub
    # checks above: "is this insecure" has a much higher false-positive
    # cost than "is this a stub" (flagging a real, safe assignment as a
    # secret leak erodes trust in the gate fast), so each pattern below
    # is written to require a genuinely suspicious SHAPE, not just a
    # suspicious KEYWORD. A variable named `api_key` that reads from
    # os.environ is fine and must not match; `api_key = "sk-..."` must.

    # Hardcoded secret: an assignment (not a function call, not an env
    # lookup) to a name that looks credential-shaped, whose value is a
    # quoted literal of plausible secret length/shape. Excludes obvious
    # placeholder values (empty string, "changeme", "xxx", "your_key_here"
    # etc.) since those are already caught by _PLACEHOLDER_RE and flagging
    # them again here would just be noise, not a second real finding.
    _SECRET_VAR_NAME_RE = (
        r"(?:api[_-]?key|apikey|secret[_-]?key|secretkey|access[_-]?token|"
        r"auth[_-]?token|private[_-]?key|client[_-]?secret|"
        r"password|passwd|pwd|db[_-]?password|"
        r"encryption[_-]?key|signing[_-]?key|session[_-]?secret|"
        r"stripe[_-]?(?:secret|key)|aws[_-]?secret|"
        r"jwt[_-]?secret)"
    )
    _HARDCODED_SECRET_RE = re.compile(
        r"(?:^|\n)\s*(?:const|let|var|final|val)?\s*" + _SECRET_VAR_NAME_RE +
        r"\s*(?::\s*\w+\s*)?[:=]\s*['\"]([^'\"]{8,})['\"]",
        re.IGNORECASE,
    )
    _SECRET_PLACEHOLDER_VALUE_RE = re.compile(
        r"^(?:changeme|change_me|xxx+|your[_-]?(?:api[_-]?)?key(?:[_-]?here)?|"
        r"insert[_-]?key[_-]?here|todo|fixme|example|test|dummy|fake|null|none|"
        r"replace[_-]?me|<[^>]+>|\$\{[^}]+\}|%\w+%|\.\.\.+)$",
        re.IGNORECASE,
    )
    # Recognizable third-party secret formats — these are near-zero
    # false-positive because the prefix alone is a strong signal
    # (stripe/AWS/GitHub/Slack/private-key-block), independent of what
    # variable name (if any) holds them. Catches secrets pasted into
    # config dicts, JSON fixtures, or string concatenation that the
    # assignment-shaped regex above would miss.
    _KNOWN_SECRET_FORMAT_RE = re.compile(
        r"(?:sk_live_[0-9a-zA-Z]{16,}|sk_test_[0-9a-zA-Z]{16,}|"
        r"AKIA[0-9A-Z]{16}|"
        r"ghp_[0-9a-zA-Z]{36}|github_pat_[0-9a-zA-Z_]{22,}|"
        r"xox[baprs]-[0-9a-zA-Z-]{10,}|"
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----)"
    )

    # Insecure client-side storage: platform-native "plain, unencrypted,
    # app-sandbox-only" storage APIs (AsyncStorage on RN, localStorage on
    # web, NSUserDefaults/UserDefaults on iOS, SharedPreferences on
    # Android) used to persist something that reads as sensitive by name
    # (token/password/credential/ssn/location-history/child's-name-etc.
    # for this app's domain). These APIs are NOT encrypted at rest on the
    # device — this is a real, well-known mobile security finding, not a
    # style nitpick, and matters more than usual for a parental-control
    # app specifically since a compromised or shared device is exactly
    # the threat model where this bites.
    _INSECURE_STORAGE_CALL_RE = re.compile(
        r"(?:AsyncStorage|localStorage|sessionStorage)\s*\.\s*setItem\s*\(\s*"
        r"['\"]([^'\"]*)['\"]",
        re.IGNORECASE,
    )
    _INSECURE_STORAGE_KEY_SENSITIVE_RE = re.compile(
        r"(?:token|password|passwd|secret|credential|auth|session[_-]?id|"
        r"ssn|social[_-]?security|location|child|credit[_-]?card|cvv)",
        re.IGNORECASE,
    )
    # Native-side equivalents (Swift/Kotlin) — matched at the call-site
    # level since these don't take a JS-style string key argument the
    # same way; presence of the call at all next to write of a
    # credential-shaped variable name is the signal.
    _NATIVE_INSECURE_STORAGE_RE = re.compile(
        r"UserDefaults\.standard\.set\([^)]*\b(?:token|password|secret|"
        r"credential)\w*|"
        r"SharedPreferences\b[\s\S]{0,80}?\.putString\(\s*['\"](?:[^'\"]*"
        r"(?:token|password|secret|credential)[^'\"]*)['\"]",
        re.IGNORECASE,
    )

    # SQL built via string interpolation/concatenation instead of
    # parameterized queries — the single highest-signal, lowest-noise
    # "missing input validation" pattern available to a static regex
    # check (unlike general "is this input validated," which has no
    # reliable syntactic signature and would be mostly false positives
    # either direction). An f-string/template-literal/+-concatenation
    # feeding directly into what looks like a SQL statement is close to
    # always a real injection risk regardless of framework.
    _SQL_INJECTION_RE = re.compile(
        r"(?:f['\"](?:[^'\"]*)?\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^'\"]*\{|"
        r"['\"](?:[^'\"]*)?\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^'\"]*['\"]\s*\+\s*|"
        r"`(?:[^`]*)?\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^`]*\$\{)",
        re.IGNORECASE,
    )

    # XSS / unescaped-output: these are all explicit, syntactic opt-outs
    # of a framework's DEFAULT escaping — React/Vue/Angular/Jinja2/Django
    # all escape output by default, so reaching for one of these APIs is
    # the developer deliberately turning that off. That's what makes this
    # pattern reliable: it's not inferring "this data might be
    # unescaped," it's matching the literal syntax that disables
    # escaping, which is a much narrower and more certain claim.
    _XSS_UNESCAPED_OUTPUT_RE = re.compile(
        r"dangerouslySetInnerHTML\s*=\s*\{\{|"
        r"\.innerHTML\s*=(?!=)|"
        r"v-html\s*=|"
        r"\|\s*safe\b|"  # Jinja2/Django template filter
        r"mark_safe\s*\(|"
        r"document\.write\s*\(",
        re.IGNORECASE,
    )

    # eval/exec/new Function on data that isn't an obvious fixed literal —
    # i.e. the argument contains a variable/expression rather than being
    # a plain quoted string. Deliberately excludes eval("some literal")
    # (still bad practice, but not an injection vector) to keep this
    # scoped to the actually dangerous shape: dynamic code execution fed
    # by something other than a hardcoded string.
    _DYNAMIC_CODE_EXEC_RE = re.compile(
        r"\beval\s*\(\s*(?!['\"])[^)]|"
        r"\bexec\s*\(\s*(?!['\"])[^)]|"
        r"new\s+Function\s*\(\s*(?!['\"])[^)]",
    )

    # Route handler reads a request body/params object and passes it
    # onward (to a DB call, a response, or a downstream function) with no
    # schema/validation library visible anywhere in the same function
    # body. This is the least precise of the new checks — request bodies
    # get validated in a thousand different idiomatic shapes across
    # frameworks — so it stays a WARNING, requires a fairly specific
    # "raw body flows directly into something" shape, and explicitly
    # bails out if any common validator name appears anywhere in the
    # function, rather than trying to prove validation happened.
    _ROUTE_HANDLER_RE = re.compile(
        r"(?:app|router)\.(?:post|put|patch)\s*\(\s*['\"][^'\"]*['\"]\s*,"
        r"(?:[^{}]*=>\s*\{|[^{}]*function\s*\([^)]*\)\s*\{)",
        re.IGNORECASE,
    )
    _RAW_BODY_ACCESS_RE = re.compile(
        r"\breq(?:uest)?\.(?:body|params|query)\b",
    )
    _VALIDATION_LIBRARY_HINT_RE = re.compile(
        # Substring match, not \b-bounded — these hint-words routinely
        # appear INSIDE camelCase identifiers (UserSchema, validateBody,
        # sanitizeInput), not just as standalone tokens. An earlier
        # \b(?:...)\b version was tested against a realistic
        # zod-validated handler (UserSchema.parse(req.body)) and missed
        # it, because \b requires a non-word char on both sides and
        # there's none between "User" and "Schema" — a real false
        # negative that would have silently passed obviously-validated
        # code through the warning path anyway (harmless) but proves the
        # detector's own self-check was less reliable than intended.
        r"zod|joi\b|\byup\b|pydantic|marshmallow|class-validator|"
        r"express-validator|validate|schema|sanitize|escape",
        re.IGNORECASE,
    )

    def _security_findings(self, rel: str, text: str) -> Tuple[List[str], List[str]]:
        """Security-specific static checks, kept separate from the
        stub/placeholder scan above since they answer a different
        question ("is this insecure" vs "is this fake") and each needed
        its own false-positive tuning. Returns (blockers, warnings) for
        this one file.
        """
        blockers: List[str] = []
        warnings: List[str] = []

        for m in self._HARDCODED_SECRET_RE.finditer(text):
            value = m.group(1)
            if self._SECRET_PLACEHOLDER_VALUE_RE.match(value.strip()):
                continue  # placeholder value, not a real leaked secret
            if value.strip().startswith(("process.env", "os.environ", "System.getenv")):
                continue  # defensive: shouldn't match the outer regex anyway, but never flag env reads
            line_no = text[:m.start()].count("\n") + 1
            blockers.append(
                f"{rel}:{line_no}: hardcoded credential-shaped literal assigned directly in "
                f"source — move it to an environment variable or secrets manager, never commit "
                f"the real value"
            )

        for m in self._KNOWN_SECRET_FORMAT_RE.finditer(text):
            line_no = text[:m.start()].count("\n") + 1
            blockers.append(
                f"{rel}:{line_no}: value matches a known live secret format (Stripe/AWS/GitHub/"
                f"Slack key or PEM private key block) — treat as a real leaked credential, "
                f"rotate it, and remove from source"
            )

        for m in self._INSECURE_STORAGE_CALL_RE.finditer(text):
            key_name = m.group(1)
            if self._INSECURE_STORAGE_KEY_SENSITIVE_RE.search(key_name):
                line_no = text[:m.start()].count("\n") + 1
                warnings.append(
                    f"{rel}:{line_no}: storing '{key_name}' via AsyncStorage/localStorage — "
                    f"this is unencrypted, app-sandbox-only storage; use Keychain (iOS) / "
                    f"Keystore-backed EncryptedSharedPreferences (Android) / expo-secure-store "
                    f"for anything sensitive"
                )

        for m in self._NATIVE_INSECURE_STORAGE_RE.finditer(text):
            line_no = text[:m.start()].count("\n") + 1
            warnings.append(
                f"{rel}:{line_no}: credential-shaped value written to UserDefaults/"
                f"SharedPreferences — these are unencrypted; use Keychain Services (iOS) or "
                f"EncryptedSharedPreferences/Keystore (Android) instead"
            )

        for m in self._SQL_INJECTION_RE.finditer(text):
            line_no = text[:m.start()].count("\n") + 1
            blockers.append(
                f"{rel}:{line_no}: SQL statement built via string interpolation/concatenation — "
                f"use parameterized queries/prepared statements (?, %s, or the ORM's own "
                f"parameter binding) instead, this is an injection risk as written"
            )

        for m in self._XSS_UNESCAPED_OUTPUT_RE.finditer(text):
            line_no = text[:m.start()].count("\n") + 1
            blockers.append(
                f"{rel}:{line_no}: output rendered with escaping explicitly disabled "
                f"(dangerouslySetInnerHTML/innerHTML=/v-html/|safe/mark_safe/document.write) — "
                f"only use this on content you are certain is not user-controlled; otherwise "
                f"sanitize first (e.g. DOMPurify) or use the framework's default escaping"
            )

        for m in self._DYNAMIC_CODE_EXEC_RE.finditer(text):
            line_no = text[:m.start()].count("\n") + 1
            blockers.append(
                f"{rel}:{line_no}: eval/exec/new Function() called with a non-literal argument — "
                f"if any part of that value can be influenced by user input, this is arbitrary "
                f"code execution; replace with a specific parser/lookup instead of dynamic eval"
            )

        for finding in self._unvalidated_route_handlers(text):
            start, handler_snippet = finding
            line_no = text[:start].count("\n") + 1
            warnings.append(
                f"{rel}:{line_no}: route handler reads req.body/params/query and no "
                f"validation/schema library (zod/joi/yup/pydantic/marshmallow/"
                f"express-validator/etc.) appears in this handler — confirm the input is "
                f"actually validated before it reaches a DB call, file write, or response"
            )

        return blockers, warnings

    @staticmethod
    def _unvalidated_route_handlers(text: str) -> List[Tuple[int, str]]:
        """Find POST/PUT/PATCH route handlers that access req.body/params/
        query with no validation-library hint anywhere in the same
        handler body. Needs brace-matching (not a flat regex) to isolate
        "this one handler's body" from the rest of the file — a flat
        regex would either match across unrelated handlers (false
        negative: real validation two functions away hides a genuinely
        unvalidated one) or bail out on any validator anywhere in the
        file (false negative: one validated handler hides all the others
        in the same file that aren't).
        """
        findings: List[Tuple[int, str]] = []
        for m in ProductionValidatorTool._ROUTE_HANDLER_RE.finditer(text):
            brace_start = text.find("{", m.start())
            if brace_start == -1:
                continue
            depth = 1
            i = brace_start + 1
            while i < len(text) and depth > 0:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            body = text[brace_start:i]
            if not ProductionValidatorTool._RAW_BODY_ACCESS_RE.search(body):
                continue
            if ProductionValidatorTool._VALIDATION_LIBRARY_HINT_RE.search(body):
                continue
            findings.append((m.start(), body[:120]))
        return findings

    def _walk_files(self) -> Iterator[Path]:
        for root, dirs, files in os.walk(self.cwd):
            dirs[:] = [
                d for d in dirs
                if d not in self._EXCLUDED_DIRS and not d.startswith(".")
            ]
            for filename in files:
                path = Path(root) / filename
                if path.suffix.lower() in self._SOURCE_SUFFIXES:
                    yield path

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def _static_findings(self) -> Tuple[List[str], List[str], int, int, int]:
        blockers: List[str] = []
        warnings: List[str] = []
        source_count = 0
        test_count = 0
        product_count = 0
        # Project-wide tallies for the substance checks below. Individual
        # small/shallow files are only a warning (a tiny real getter is
        # fine) — but a project where MOST product code or MOST tests are
        # shallow/trivial is exactly the "scaffold that technically passes"
        # failure mode, so that gets promoted to a blocker.
        shallow_product_files = 0
        trivial_test_files = 0
        for path in self._walk_files():
            rel = str(path.relative_to(self.cwd)).replace("\\", "/")
            text = self._read_text(path)
            if not text.strip():
                blockers.append(f"{rel}: empty source file")
                continue
            source_count += 1
            name_low = rel.lower()
            is_test = bool(self._TEST_NAME_RE.search(path.stem) or "/test" in name_low)
            if is_test:
                test_count += 1
            else:
                product_count += 1
            # Keep test fixtures and intentionally tiny package markers from
            # being mistaken for product code, but never excuse an empty
            # application module.
            meaningful = [
                line for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith(("#", "//", "/*", "*"))
            ]
            if not is_test and len(text.strip()) < 80:
                warnings.append(f"{rel}: unusually small source file ({len(text.strip())} chars); review its behavior")
            if not is_test and self._PLACEHOLDER_RE.search(text):
                blockers.append(f"{rel}: unresolved placeholder language")
            if not is_test and self._STUB_RE.search(text):
                blockers.append(f"{rel}: stub-like return/exception detected")
            if not is_test and not meaningful:
                blockers.append(f"{rel}: comments-only source file")

            # Security checks run on ALL source files, test or product —
            # a hardcoded secret pasted into a test fixture is still a
            # real, committed credential; scoping this to product-only
            # (like the stub checks above) would create a blind spot.
            sec_blockers, sec_warnings = self._security_findings(rel, text)
            blockers.extend(sec_blockers)
            warnings.extend(sec_warnings)

            if is_test:
                # A test file that runs and exits 0 but never actually
                # asserts anything meaningful (assert True, expect(true)
                # .toBe(true), or a test function with no assert/expect at
                # all) is indistinguishable from a passing suite in every
                # OTHER check this tool runs. Catch it explicitly.
                test_funcs = len(self._TEST_FUNC_RE.findall(text))
                assert_calls = len(self._ASSERT_CALL_RE.findall(text))
                trivial_hits = len(self._TRIVIAL_ASSERT_RE.findall(text))
                if trivial_hits:
                    warnings.append(
                        f"{rel}: contains {trivial_hits} tautological assertion(s) "
                        f"(assert True / expect(true).toBe(true)) that can never fail"
                    )
                    trivial_test_files += 1
                elif test_funcs and assert_calls == 0:
                    warnings.append(
                        f"{rel}: {test_funcs} test function(s) with no assert/expect call — "
                        f"asserts nothing, so it can't actually fail"
                    )
                    trivial_test_files += 1
            elif not self._PLACEHOLDER_RE.search(text) and not self._STUB_RE.search(text):
                # Only run the shallow-body ratio check on files that
                # already cleared the placeholder/stub scan, to avoid
                # double-counting the same file under two different
                # blocker reasons.
                func_defs = len(self._FUNC_DEF_RE.findall(text))
                shallow_bodies = len(self._SHALLOW_BODY_RE.findall(text))
                if func_defs >= 2 and shallow_bodies / func_defs >= 0.6:
                    warnings.append(
                        f"{rel}: {shallow_bodies}/{func_defs} functions are single-line "
                        f"pass/return-literal stubs with no real logic; looks like a scaffold"
                    )
                    shallow_product_files += 1
        if product_count > 0 and shallow_product_files / product_count >= 0.5:
            blockers.append(
                f"{shallow_product_files}/{product_count} product source files are mostly "
                f"trivial stub functions (pass/return-literal bodies) — this reads as a "
                f"scaffold, not a working implementation. Replace stub bodies with real logic."
            )
        if test_count > 0 and trivial_test_files / test_count >= 0.5:
            blockers.append(
                f"{trivial_test_files}/{test_count} test files assert nothing meaningful "
                f"(tautological or missing assertions) — tests pass without proving any "
                f"behavior. Rewrite them to assert real outcomes."
            )
        return blockers, warnings, source_count, test_count, product_count

    def _manifest_commands(self) -> Tuple[List[Tuple[str, List[str], str]], List[str], List[str]]:
        commands: List[Tuple[str, List[str], str]] = []
        manifests: List[str] = []
        blockers: List[str] = []

        package = self.cwd / "package.json"
        if package.is_file():
            manifests.append("package.json")
            try:
                data = json.loads(self._read_text(package))
                scripts = data.get("scripts") or {}
                all_deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}

                # React Native / Expo projects declare 'react-native' or
                # 'expo' as a real dependency — that's a much more
                # reliable signal than file presence, since a plain web
                # project's package.json looks structurally identical
                # otherwise. This distinction matters because RN/Expo
                # projects do NOT conventionally ship a web-style "build"
                # script — treating its absence as a blocker (as the
                # original web-only logic did) would incorrectly flag
                # every healthy RN project as broken.
                is_react_native = "react-native" in all_deps
                is_expo = "expo" in all_deps
                is_mobile_js = is_react_native or is_expo

                manager = "pnpm" if (self.cwd / "pnpm-lock.yaml").is_file() else (
                    "yarn" if (self.cwd / "yarn.lock").is_file() else (
                        "bun" if (self.cwd / "bun.lockb").is_file() else "npm"
                    )
                )
                if not (self.cwd / "node_modules").exists():
                    blockers.append(
                        "node_modules is missing; install declared dependencies before validation"
                    )
                if "test" in scripts:
                    commands.append(("test", [manager, "test"], "javascript"))
                elif "test:ci" in scripts:
                    commands.append(("test", [manager, "run", "test:ci"], "javascript"))
                else:
                    blockers.append("package.json has no test script")

                if is_mobile_js:
                    manifests.append("react-native" if is_react_native else "expo")
                    # Typecheck stands in for "build" here — a real
                    # native build (Gradle/Xcode) is a separate,
                    # environment-gated concern handled below via the
                    # android/ios directories or `eas build`, not
                    # something this generic manifest-command path
                    # should silently claim to have verified.
                    if "typecheck" in scripts:
                        commands.append(("typecheck", [manager, "run", "typecheck"], "javascript"))
                    elif (self.cwd / "tsconfig.json").is_file():
                        commands.append(("typecheck", ["npx", "tsc", "--noEmit"], "javascript"))
                    has_android_dir = (self.cwd / "android").is_dir()
                    has_ios_dir = (self.cwd / "ios").is_dir()
                    has_eas_config = (self.cwd / "eas.json").is_file()
                    if not (has_android_dir or has_ios_dir or has_eas_config):
                        blockers.append(
                            "react-native/expo project has no android/ or ios/ native "
                            "project directory and no eas.json — there is no path to a "
                            "real native build from here (bare RN needs android/ios/, "
                            "managed Expo needs eas.json for EAS Build)"
                        )
                    if has_android_dir and not (self.cwd / "android" / "gradlew").is_file() and \
                       not (self.cwd / "android" / "gradlew.bat").is_file():
                        blockers.append(
                            "android/ directory exists but has no gradlew wrapper — "
                            "cannot attempt a Gradle build without it"
                        )
                elif "build" in scripts:
                    commands.append(("build", [manager, "run", "build"], "javascript"))
                elif "typecheck" in scripts:
                    commands.append(("typecheck", [manager, "run", "typecheck"], "javascript"))
                else:
                    blockers.append("package.json has no build or typecheck script")

                if "lint" in scripts:
                    commands.append(("lint", [manager, "run", "lint"], "javascript"))
            except (OSError, json.JSONDecodeError) as exc:
                blockers.append(f"package.json is invalid: {exc}")

        pubspec = self.cwd / "pubspec.yaml"
        if pubspec.is_file():
            manifests.append("pubspec.yaml")
            # Flutter's own toolchain (flutter analyze / flutter test) is
            # the correct verifier here — there's no separate "manager"
            # concept to infer like the npm/yarn/pnpm/bun split above.
            commands.append(("analyze", ["flutter", "analyze"], "flutter"))
            has_test_dir = (self.cwd / "test").is_dir()
            if has_test_dir:
                commands.append(("test", ["flutter", "test"], "flutter"))
            else:
                blockers.append("Flutter project has no test/ directory")
            has_android = (self.cwd / "android").is_dir()
            has_ios = (self.cwd / "ios").is_dir()
            if not (has_android or has_ios):
                blockers.append(
                    "pubspec.yaml found but neither android/ nor ios/ platform "
                    "directory exists — this project has no native target to build "
                    "yet (run `flutter create .` in the project to scaffold them)"
                )

        pyproject = self.cwd / "pyproject.toml"
        requirements = self.cwd / "requirements.txt"
        setup_py = self.cwd / "setup.py"
        if pyproject.is_file() or requirements.is_file() or setup_py.is_file():
            if pyproject.is_file():
                manifests.append("pyproject.toml")
            if requirements.is_file():
                manifests.append("requirements.txt")
            if setup_py.is_file():
                manifests.append("setup.py")
            commands.append(("compile", ["python", "-m", "compileall", "-q", "."], "python"))
            if (self.cwd / "tests").is_dir() or any(
                p.name.startswith("test_") or p.name.endswith("_test.py")
                for p in self.cwd.rglob("*.py")
                if not any(part in self._EXCLUDED_DIRS for part in p.parts)
            ):
                commands.append(("test", ["python", "-m", "pytest", "-q"], "python"))
            else:
                blockers.append("Python project has no tests directory or test modules")

        if (self.cwd / "go.mod").is_file():
            manifests.append("go.mod")
            commands.append(("test", ["go", "test", "./..."], "go"))
        if (self.cwd / "Cargo.toml").is_file():
            manifests.append("Cargo.toml")
            commands.append(("test", ["cargo", "test"], "rust"))

        if not manifests:
            blockers.append(
                "no supported project manifest found (package.json, pyproject.toml, "
                "requirements.txt, setup.py, go.mod, Cargo.toml, or pubspec.yaml)"
            )
        return commands, manifests, blockers

    def execute(self, run_checks: bool = True, timeout: int = 180) -> ToolResult:
        blockers, warnings, source_count, test_count, product_count = self._static_findings()
        commands, manifests, command_blockers = self._manifest_commands()
        blockers.extend(command_blockers)
        checks: List[Dict[str, Any]] = []
        test_digest = ""
        if run_checks:
            runner = RunTool(self.cwd)
            for label, argv, kind in commands:
                program, args = argv[0], argv[1:]
                result = runner.execute(program=program, args=args, timeout=max(10, min(int(timeout), 600)))
                check: Dict[str, Any] = {
                    "name": label,
                    "command": " ".join(argv),
                    "ok": not result.is_error,
                    "output": result.text()[-3000:],
                }
                checks.append(check)
                if label == "test" and result.test_digest:
                    test_digest = result.test_digest
                if result.is_error:
                    blockers.append(f"{label} command failed: {' '.join(argv)}")
        else:
            warnings.append("checks were skipped; run validate_project with run_checks=true")

        report = {
            "version": 1,
            "ready": (
                run_checks
                and not blockers
                and product_count > 0
                and test_count > 0
                and bool(commands)
                and bool(checks)
                and all(bool(check.get("ok")) for check in checks)
            ),
            "manifests": manifests,
            "source_files": source_count,
            "test_files": test_count,
            "blockers": list(dict.fromkeys(blockers)),
            "warnings": list(dict.fromkeys(warnings)),
            "checks": checks,
            "test_digest": test_digest,
        }
        status = "READY" if report["ready"] else "NOT READY"
        return ToolResult(
            json.dumps({"status": status, "report": report}, indent=2, ensure_ascii=False),
            is_error=not report["ready"],
            test_digest=test_digest,
        )


class TaskTool(Tool):
    name = "task"
    description = (
        "Delegate an independent exploration task. Searches and reads relevant files "
        "under the project, returns a structured summary (paths + excerpts). "
        "Use for 'find how X works', 'map module Y', etc. Does not write files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short task description"},
            "prompt": {"type": "string", "description": "Detailed instructions / keywords"},
        },
        "required": ["description", "prompt"],
    }

    def execute(self, description: str, prompt: str) -> ToolResult:
        try:
            text = f"{description or ''}\n{prompt or ''}"
            tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
            stop = {
                "the", "and", "for", "with", "that", "this", "from", "into", "about",
                "find", "how", "what", "where", "please", "should", "must", "task",
                "search", "read", "file", "files", "code", "project", "implement",
            }
            keys = []
            seen = set()
            for tok in tokens:
                low = tok.lower()
                if low in stop or low in seen:
                    continue
                seen.add(low)
                keys.append(tok)
                if len(keys) >= 8:
                    break
            if not keys:
                keys = ["main", "config", "agent"]

            findings: List[str] = []
            paths_hit: List[str] = []
            glob_errors: List[str] = []
            glob_attempts = 0

            globber = GlobTool(self.cwd)
            glob_fail_count = 0
            for pattern in ("**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/package.json", "**/requirements.txt"):
                res = globber.execute(pattern=pattern, path=".")
                glob_attempts += 1
                if res.is_error:
                    glob_fail_count += 1
                    if res.error and res.error not in glob_errors:
                        glob_errors.append(res.error)
                    continue
                if res.output:
                    for line in res.output.splitlines()[:40]:
                        line = line.strip()
                        # GlobTool's own "no results" sentinel isn't a
                        # path — guard the same way as the search loop
                        # below, or it gets indexed as a fake source file.
                        if line == "No files found":
                            continue
                        if line and line not in paths_hit:
                            paths_hit.append(line)
                if len(paths_hit) > 80:
                    break

            searcher = SearchTool(self.cwd)
            match_files: Dict[str, int] = {}
            search_errors: List[str] = []
            search_attempts = 0
            search_fail_count = 0
            for k in keys[:5]:
                res = searcher.execute(pattern=re.escape(k), path=".")
                search_attempts += 1
                if res.is_error:
                    search_fail_count += 1
                    if res.error and res.error not in search_errors:
                        search_errors.append(res.error)
                    continue
                if not res.output:
                    continue
                for line in res.output.splitlines()[:30]:
                    # SearchTool's own "no matches" / "no matches ... single
                    # file" messages contain a ':' but are not a real
                    # "path:line:content" match row — guard against
                    # misreading them as a matched file.
                    if line.startswith("No matches for pattern"):
                        continue
                    path = line.split(":", 1)[0].strip() if ":" in line else ""
                    if path and not path.startswith("["):
                        match_files[path] = match_files.get(path, 0) + 1

            # If every single glob AND search call errored (as opposed to
            # succeeding with zero results), the "0 candidates / 0 matches"
            # numbers below are meaningless — they mean "the sub-search
            # never actually ran", not "this project has no matching
            # content". Surface that distinctly instead of reporting a
            # deceptively clean-looking empty success. Compare against the
            # actual failure COUNT, not the deduped error-message list —
            # several identical failures collapse to one message but must
            # still count as every attempt failing.
            all_globs_failed = glob_attempts > 0 and glob_fail_count >= glob_attempts
            all_searches_failed = search_attempts > 0 and search_fail_count >= search_attempts
            if all_globs_failed and all_searches_failed:
                sample_err = (glob_errors + search_errors)[0]
                return ToolResult(
                    "",
                    error=(
                        f"task sub-agent could not search this project at all — every "
                        f"glob and search call failed (e.g. \"{sample_err}\"). This is "
                        f"NOT \"no matches found\"; the exploration never actually ran. "
                        f"Check that the project path/cwd is valid before retrying."
                    ),
                    is_error=True,
                )

            ranked = sorted(match_files.items(), key=lambda x: -x[1])[:12]
            reader = ReadTool(self.cwd)
            excerpts = []
            for path, score in ranked:
                try:
                    r = reader.execute(path=path, offset=1, limit=40)
                    if not r.is_error:
                        body = (r.output or "")[:1200]
                        excerpts.append(f"### {path} (score={score})\n{body}")
                except Exception:
                    continue

            lines = [
                f"[Sub-agent complete] {description}",
                f"Keywords: {', '.join(keys)}",
                f"Indexed source candidates: {len(paths_hit)}",
                f"Keyword-matched files: {len(match_files)}",
                "",
                "## Top matches",
            ]
            for path, score in ranked[:10]:
                lines.append(f"- {path} (hits≈{score})")
            if excerpts:
                lines.append("")
                lines.append("## Excerpts")
                lines.extend(excerpts[:6])
            else:
                lines.append("")
                lines.append("No keyword hits. Sample paths:")
                for pth in paths_hit[:15]:
                    lines.append(f"- {pth}")
            return ToolResult("\n".join(lines))
        except Exception as e:
            return ToolResult("", error=f"task sub-agent failed: {e}", is_error=True)


class ModulesTool(Tool):
    name = "modules"
    description = (
        "List Python packages/modules under a path and who-imports-whom (shallow). "
        "Use before large refactors to extend existing modules instead of duplicating."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "src"},
            "max_files": {"type": "integer", "default": 80},
        },
        "required": [],
    }

    def execute(self, path: str = "src", max_files: int = 80) -> ToolResult:
        try:
            base = self._safe_path(path if path else ".")
            fell_back = False
            if not base.exists():
                fell_back = True
                base = self.cwd
            py_files = []
            for fp in sorted(base.rglob("*.py")):
                if any(part.startswith(".") for part in fp.relative_to(self.cwd).parts):
                    continue
                py_files.append(fp)
                if len(py_files) >= max(10, min(int(max_files or 80), 200)):
                    break
            import_re = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)
            header = f"Modules under {base.relative_to(self.cwd) if base != self.cwd else '.'} ({len(py_files)} files)"
            if fell_back:
                header = (
                    f"NOTE: path '{path}' does not exist — showing project root instead.\n"
                    + header
                )
            lines = [header, ""]
            for fp in py_files:
                rel = str(fp.relative_to(self.cwd)).replace("\\", "/")
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace")[:8000]
                except Exception:
                    continue
                imps = []
                for m in import_re.finditer(text):
                    mod = m.group(1) or m.group(2)
                    if mod and mod not in imps:
                        imps.append(mod)
                    if len(imps) >= 12:
                        break
                imp_s = ", ".join(imps[:12]) if imps else "(no imports parsed)"
                lines.append(f"- {rel}")
                lines.append(f"    imports: {imp_s}")
            return ToolResult("\n".join(lines))
        except Exception as e:
            return ToolResult("", error=str(e), is_error=True)


class TodoTool(Tool):
    name = "todo"
    description = "Manage a todo list for tracking complex multi-step tasks."
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "update", "remove", "list", "clear"]},
            "id": {"type": "string", "description": "Todo ID (for update/remove)"},
            "content": {"type": "string", "description": "Todo content (for add/update)"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "done"], "description": "Status (for update)"},
        },
        "required": ["action"],
    }

    _todos: Dict[str, Dict[str, Any]] = {}
    _counter = 0

    def execute(self, action: str = "list", id: Optional[str] = None, content: Optional[str] = None, status: Optional[str] = None, **kwargs) -> ToolResult:
        try:
            if content is None:
                for k in ("text", "task", "title", "description", "item", "todo", "body", "msg", "message"):
                    if kwargs.get(k):
                        content = kwargs[k]
                        break
            if id is None:
                id = kwargs.get("todo_id") or kwargs.get("tid") or kwargs.get("todoId")
            if status is None and kwargs.get("state"):
                status = kwargs.get("state")
            action = (action or kwargs.get("op") or "list")
            if isinstance(action, str):
                action = action.strip().lower()
            if action == "add":
                if not content or not str(content).strip():
                    return ToolResult(
                        "",
                        error=(
                            'Missing "content" for todo add — no content field and none of the '
                            'accepted aliases (text/task/title/description/item/todo/body/msg/message) '
                            'were present either. Call again with a content string, e.g.: '
                            '{"action":"add","content":"Write Requirements section in PLAN.md"}'
                        ),
                        is_error=True,
                    )
                content = str(content).strip()
                for tid0, t0 in TodoTool._todos.items():
                    if t0.get("content") == content and t0.get("status") != "done":
                        return ToolResult(f"Todo already exists {tid0}: {content}")
                TodoTool._counter += 1
                tid = f"T{TodoTool._counter:03d}"
                TodoTool._todos[tid] = {"content": content, "status": "pending"}
                return ToolResult(f"Added todo {tid}: {content}")
            elif action == "update":
                if not id or not str(id).strip():
                    return ToolResult(
                        "",
                        error=(
                            'Missing "id" for todo update — call requires an existing todo ID '
                            '(e.g. "T003"), not just action+status/content. Call /todo list or '
                            'todo(action="list") first if you don\'t have the ID, then retry with '
                            'it, e.g.: {"action":"update","id":"T003","status":"done"}'
                        ),
                        is_error=True,
                    )
                if id not in TodoTool._todos:
                    return ToolResult(
                        "",
                        error=(
                            f"Todo {id!r} not found — it doesn't exist or was already removed. "
                            f"Call todo(action=\"list\") to see current IDs before retrying."
                        ),
                        is_error=True,
                    )
                if content:
                    TodoTool._todos[id]["content"] = content
                if status:
                    TodoTool._todos[id]["status"] = status
                return ToolResult(f"Updated todo {id}")
            elif action == "remove":
                if not id or not str(id).strip():
                    return ToolResult(
                        "",
                        error=(
                            'Missing "id" for todo remove — call requires an existing todo ID '
                            '(e.g. "T003"). Call todo(action="list") first if unsure.'
                        ),
                        is_error=True,
                    )
                if id not in TodoTool._todos:
                    return ToolResult(
                        "",
                        error=(
                            f"Todo {id!r} not found — it doesn't exist or was already removed. "
                            f"Call todo(action=\"list\") to see current IDs before retrying."
                        ),
                        is_error=True,
                    )
                del TodoTool._todos[id]
                return ToolResult(f"Removed todo {id}")
            elif action == "list":
                if not TodoTool._todos:
                    return ToolResult("No todos")
                lines = []
                for tid, todo in TodoTool._todos.items():
                    icon = {"pending": "○", "in_progress": "◐", "done": "*"}[todo["status"]]
                    lines.append(f"{icon} {tid}: {todo['content']} [{todo['status']}]")
                return ToolResult("\n".join(lines))
            elif action == "clear":
                TodoTool._todos.clear()
                return ToolResult("All todos cleared")
            else:
                return ToolResult("", error=f"Unknown action: {action}", is_error=True)
        except Exception as e:
            return ToolResult("", error=str(e), is_error=True)



def _strip_html(html: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
    except Exception:
        pass
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def urllib_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def _http_get(url: str, timeout: float = 25.0, headers: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    default_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NeonArchitect/4.2) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        default_headers.update(headers)
    if HAS_HTTPX:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=default_headers) as client:
            r = client.get(url)
            return r.status_code, r.text, str(r.url)
    import urllib.request
    req = urllib.request.Request(url, headers=default_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return getattr(resp, "status", 200), raw.decode(charset, errors="replace"), resp.geturl()


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the public internet. Returns ranked titles, URLs, snippets. "
        "Use BEFORE guessing product domains. Follow with browse_page on best URLs. "
        "Supports site: operators."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "num_results": {"type": "integer", "default": 8},
        },
        "required": ["query"],
    }

    def execute(self, query: str, num_results: int = 8) -> ToolResult:
        try:
            q = (query or "").strip()
            if not q:
                return ToolResult("", error="query is required", is_error=True)
            n = max(1, min(int(num_results or 8), 15))
            results: List[Dict[str, str]] = []
            errors: List[str] = []
            brave_key = (os.environ.get("BRAVE_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY") or "").strip()
            if brave_key:
                try:
                    status, body, _ = _http_get(
                        f"https://api.search.brave.com/res/v1/web/search?q={urllib_quote(q)}&count={n}",
                        timeout=20.0,
                        headers={"Accept": "application/json", "X-Subscription-Token": brave_key},
                    )
                    if status == 200:
                        data = json.loads(body)
                        for item in (data.get("web") or {}).get("results") or []:
                            results.append({"title": item.get("title") or "", "url": item.get("url") or "",
                                            "snippet": item.get("description") or ""})
                    else:
                        errors.append(f"brave:{status}")
                except Exception as e:
                    errors.append(f"brave:{e}")
            if len(results) < n:
                try:
                    status, body, _ = _http_get(f"https://html.duckduckgo.com/html/?q={urllib_quote(q)}", timeout=25.0)
                    if status == 200:
                        results.extend(self._parse_ddg_html(body, n - len(results)))
                    else:
                        errors.append(f"ddg_html:{status}")
                except Exception as e:
                    errors.append(f"ddg_html:{e}")
            if len(results) < n:
                try:
                    status, body, _ = _http_get(f"https://lite.duckduckgo.com/lite/?q={urllib_quote(q)}", timeout=25.0)
                    if status == 200:
                        results.extend(self._parse_ddg_lite(body, n - len(results)))
                    else:
                        errors.append(f"ddg_lite:{status}")
                except Exception as e:
                    errors.append(f"ddg_lite:{e}")
            if len(results) < n:
                try:
                    status, body, _ = _http_get(
                        f"https://api.duckduckgo.com/?q={urllib_quote(q)}&format=json&no_html=1&skip_disambig=1",
                        timeout=15.0,
                    )
                    if status == 200:
                        data = json.loads(body)
                        if data.get("AbstractURL"):
                            results.append({"title": data.get("Heading") or q, "url": data.get("AbstractURL") or "",
                                            "snippet": data.get("AbstractText") or ""})
                        for topic in data.get("RelatedTopics") or []:
                            items = topic.get("Topics") if isinstance(topic, dict) and topic.get("Topics") else [topic]
                            for sub in items:
                                if isinstance(sub, dict) and sub.get("FirstURL"):
                                    results.append({
                                        "title": re.sub(r"<.*?>", "", sub.get("Text") or "")[:120],
                                        "url": sub.get("FirstURL") or "",
                                        "snippet": re.sub(r"<.*?>", "", sub.get("Text") or ""),
                                    })
                except Exception as e:
                    errors.append(f"ddg_api:{e}")
            seen, unique = set(), []
            for r in results:
                u = (r.get("url") or "").strip()
                if not u or u in seen:
                    continue
                seen.add(u)
                unique.append(r)
                if len(unique) >= n:
                    break
            if not unique:
                return ToolResult("", error=(
                    f"No results for {q!r}. Details: {'; '.join(errors) or 'unknown'}. "
                    "Try alternate spelling (oiioii.ai vs oioi.com) or browse_page on a known URL."
                ), is_error=True)
            lines = [f"Web search results for: {q}", f"Returned {len(unique)} result(s).", ""]
            for i, r in enumerate(unique, 1):
                lines.append(f"{i}. {r.get('title') or '(no title)'}")
                lines.append(f"   URL: {r.get('url')}")
                if r.get("snippet"):
                    lines.append(f"   {r['snippet'][:300]}")
                lines.append("")
            return ToolResult("\n".join(lines))
        except Exception as e:
            return ToolResult("", error=str(e), is_error=True)

    @staticmethod
    def _parse_ddg_html(html: str, limit: int) -> List[Dict[str, str]]:
        out = []
        for m in re.finditer(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</(?:a|td|div)',
            html, re.I | re.S,
        ):
            url = WebSearchTool._clean_ddg_url(m.group(1))
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            snippet = re.sub(r"<[^>]+>", "", m.group(3)).strip()
            if url:
                out.append({"title": title, "url": url, "snippet": snippet})
            if len(out) >= limit:
                break
        if not out:
            for m in re.finditer(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
                url = WebSearchTool._clean_ddg_url(m.group(1))
                title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if url:
                    out.append({"title": title, "url": url, "snippet": ""})
                if len(out) >= limit:
                    break
        return out

    @staticmethod
    def _parse_ddg_lite(html: str, limit: int) -> List[Dict[str, str]]:
        out = []
        for m in re.finditer(r' rel="nofollow" href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
            url, title = m.group(1).strip(), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if "duckduckgo.com" in url:
                continue
            out.append({"title": title, "url": url, "snippet": ""})
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _clean_ddg_url(url: str) -> str:
        from urllib.parse import unquote, parse_qs, urlparse
        url = url.replace("&amp;", "&")
        if "uddg=" in url:
            try:
                qs = parse_qs(urlparse(url).query)
                if "uddg" in qs:
                    return unquote(qs["uddg"][0])
            except Exception:
                pass
        if url.startswith("//"):
            url = "https:" + url
        return url


class BrowsePageTool(Tool):
    name = "browse_page"
    description = (
        "Fetch a URL and return readable text (HTML stripped). "
        "Use after web_search for docs, product pages, API references."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "default": 12000},
            "start_line": {"type": "integer", "default": 1},
        },
        "required": ["url"],
    }

    def execute(self, url: str, max_chars: int = 12000, start_line: int = 1) -> ToolResult:
        try:
            u = (url or "").strip()
            if not u:
                return ToolResult("", error="url is required", is_error=True)
            if not re.match(r"^https?://", u, re.I):
                u = "https://" + u
            host = re.sub(r"^https?://", "", u, flags=re.I).split("/")[0].lower()
            allow_local = getattr(self, "allow_local_network", False)
            if not allow_local and (
                host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local")
            ):
                return ToolResult(
                    "",
                    error=f"Refusing local host: {host} (enable full_access / allow_local_network)",
                    is_error=True,
                )
            status, body, final_url = _http_get(u, timeout=30.0)
            if status >= 400:
                return ToolResult("", error=f"HTTP {status} fetching {final_url or u}", is_error=True)
            low = body[:2000].lower()
            text = _strip_html(body) if ("<html" in low or "<!doctype" in low or "<body" in low) else body
            lines = text.splitlines()
            start = max(0, int(start_line or 1) - 1)
            sliced = "\n".join(lines[start:])
            max_c = max(1000, min(int(max_chars or 12000), 50000))
            trunc = len(sliced) > max_c
            if trunc:
                sliced = sliced[:max_c] + f"\n\n… [truncated at {max_c} chars]"
            header = f"URL: {final_url}\nStatus: {status}\nLines: {len(lines)} (from {start+1})\nTruncated: {trunc}\n{'─'*40}\n"
            return ToolResult(header + sliced)
        except Exception as e:
            return ToolResult("", error=f"browse_page failed: {e}", is_error=True)


def build_tools(cwd: Path, full_access: bool = True, allow_outside: bool = False,
                allow_local: bool = True, bash_confirm: bool = False) -> Dict[str, Tool]:
    # `bash_confirm` is kept as a parameter for backward compatibility with
    # existing call sites, but is now inert: RunTool has no shell-string
    # concept of a "destructive command" to gate behind confirm=true. It
    # only ever invokes a fixed enum of known dev programs via argv, with
    # shell=False, so there is no rm-rf-style shell command it could run
    # in the first place. This is intentional — see RunTool's docstring.
    tool_list: List[Tool] = [
        ReadTool(cwd), WriteTool(cwd), EditTool(cwd),
        SearchTool(cwd), GlobTool(cwd), LsTool(cwd),
        RunTool(cwd), WebSearchTool(cwd), BrowsePageTool(cwd),
        ProductionValidatorTool(cwd), TaskTool(cwd), ModulesTool(cwd), TodoTool(cwd),
    ]
    tools = {t.name: t for t in tool_list}
    # Generation layer tools are injected after pool is available (see AgentCore._init_generation_tools)
    for t in tools.values():
        t.allow_outside_project = False
    local = bool(allow_local or full_access)
    if "browse_page" in tools:
        tools["browse_page"].allow_local_network = local
    return tools


def tools_schema(tools: Dict[str, Tool]) -> List[Dict[str, Any]]:
    return [t.schema() for t in tools.values()]


def execute_tool(tools: Dict[str, Tool], name: str, args: Dict[str, Any]) -> ToolResult:
    tool = tools.get(name)
    if not tool:
        return ToolResult("", error=f"Unknown tool: {name}", is_error=True)
    try:
        return tool.execute(**args)
    except TypeError as e:
        return ToolResult("", error=f"Invalid arguments for {name}: {e}", is_error=True)
    except Exception as e:
        return ToolResult("", error=f"{name} failed: {e}", is_error=True)


def sanitize_tool_arguments(raw: Any) -> str:
    if raw is None:
        return "{}"
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    if isinstance(raw, (list, int, float, bool)):
        return json.dumps({"_value": raw}, ensure_ascii=False)
    s = str(raw).strip()
    if not s:
        return "{}"
    try:
        parsed = json.loads(s)
    except Exception:
        return "{}"
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False)
    return json.dumps({"_value": parsed}, ensure_ascii=False)


def sanitize_tool_calls(tool_calls: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not tool_calls:
        return []
    out: List[Dict[str, Any]] = []
    for i, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            continue
        if "function" in tc and isinstance(tc.get("function"), dict):
            fn = dict(tc["function"])
            name = fn.get("name") or "unknown"
            args = sanitize_tool_arguments(fn.get("arguments"))
            tid = tc.get("id") or f"call_{i}"
            out.append({
                "id": tid,
                "type": "function",
                "function": {"name": name, "arguments": args},
            })
        else:
            name = tc.get("name") or "unknown"
            args = sanitize_tool_arguments(tc.get("arguments"))
            tid = tc.get("id") or f"call_{i}"
            out.append({
                "id": tid,
                "type": "function",
                "function": {"name": name, "arguments": args},
            })
    return out


def sanitize_messages_for_api(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        mm = dict(m)
        if mm.get("role") == "assistant" and mm.get("tool_calls"):
            mm["tool_calls"] = sanitize_tool_calls(mm["tool_calls"])
            if not mm["tool_calls"]:
                mm.pop("tool_calls", None)
                if not mm.get("content"):
                    mm["content"] = ""
        cleaned.append(mm)
    return cleaned

class ContextManager:
    def __init__(self, compact_threshold: int = 5000, ctx_window: int = 131072):
        self.messages: List[Dict[str, Any]] = []
        self.compact_threshold = compact_threshold
        self.ctx_window = ctx_window
        self.compaction_summary: Optional[str] = None

    def add_system(self, content: str):
        self.messages = [m for m in self.messages if m.get("role") != "system"]
        self.messages.insert(0, {"role": "system", "content": content})

    def add_user(self, content: str):
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str = "", tool_calls: Optional[List[Dict]] = None):
        msg: Dict[str, Any] = {"role": "assistant", "content": content or None}
        if tool_calls:
            msg["tool_calls"] = sanitize_tool_calls(tool_calls)
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, name: str, content: str):
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
        })

    def estimate_tokens(self) -> int:
        total = 0
        for m in self.messages:
            content = str(m.get("content") or "")
            # m.get("tool_calls", []) only falls back to [] when the key is
            # MISSING — a message with the key present but explicitly set to
            # None (valid JSON `"tool_calls": null`, e.g. from a hand-edited
            # or differently-serialized session file) makes .get() return
            # None itself, and `for tc in None` throws TypeError. Internally
            # constructed messages never do this (add_assistant only sets
            # the key when tool_calls is truthy), but restored session data
            # is external input and isn't guaranteed to keep that shape.
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {})
                content += str(fn.get("name", "")) + str(fn.get("arguments", ""))
            total += max(1, len(content) // 4)
        return total

    def ratio_used(self) -> float:
        return self.estimate_tokens() / self.ctx_window

    def should_compact(self) -> bool:
        return self.estimate_tokens() > self.compact_threshold

    def compact(self, keep_recent: int = 8) -> bool:
        # keep_recent must stay >= 1: Python's rest[-0:] means "from index
        # 0", i.e. the WHOLE list, not "the last 0 items" — so
        # keep_recent=0 would silently make `recent` the entire message
        # list and `old` empty, meaning nothing gets folded even though
        # this still returns True and overwrites compaction_summary. Every
        # current call site passes a hardcoded positive value (4/6/8), so
        # this hasn't fired in practice, but the guard belongs on the
        # function itself since a future caller passing a computed value
        # could hit 0 silently.
        keep_recent = max(1, keep_recent)
        sys_msgs = [m for m in self.messages if m.get("role") == "system"]
        rest = [m for m in self.messages if m.get("role") != "system"]
        if len(rest) <= keep_recent + 2:
            return False

        old, recent = rest[:-keep_recent], rest[-keep_recent:]
        summaries = []
        files_touched: Dict[str, str] = {}

        pending_call_paths: Dict[str, str] = {}
        for m in old:
            role = m.get("role", "")
            if role == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    path = args.get("path")
                    if path and name in ("read", "write", "edit"):
                        pending_call_paths[tc.get("id", "")] = (name, path)
            elif role == "tool":
                call_id = m.get("tool_call_id", "")
                match = pending_call_paths.get(call_id)
                if match:
                    name, path = match
                    is_error = str(m.get("content", "")).startswith("[ERROR]")
                    if not is_error:
                        verb = {"read": "read", "write": "wrote", "edit": "edited"}[name]
                        files_touched[path] = verb

        for m in old:
            role = m.get("role", "")
            content = str(m.get("content", ""))[:120].replace("\n", " ")
            if role == "tool":
                content = f"[tool result from {m.get('name', '?')}]"
            elif role == "assistant" and m.get("tool_calls"):
                names = [tc.get("function", {}).get("name", "?") for tc in m["tool_calls"]]
                content = f"[called tools: {', '.join(names)}]"
            summaries.append(f"  {role}: {content}")

        manifest = ""
        if files_touched:
            lines = [f"  - {p} ({v})" for p, v in sorted(files_touched.items())]
            manifest = (
                "\n\n[FILES ALREADY SEEN THIS SESSION — do NOT re-read unless you "
                "suspect they changed; use the summary above and proceed]\n"
                + "\n".join(lines)
            )

        self.compaction_summary = (
            f"[FOLDED {len(old)} MESSAGES]\n" + "\n".join(summaries[-30:]) + manifest
        )
        self.messages = sys_msgs + [
            {"role": "user", "content": f"[PRIOR CONTEXT SUMMARY]\n{self.compaction_summary}\n\nYou are mid-task. Continue executing the next required tool immediately. Do not re-read files listed as already seen unless you have a specific reason to believe they changed."},
            {"role": "assistant", "content": "Context acknowledged. Proceeding with next tool execution."},
        ] + recent
        return True

    def clear(self):
        self.messages = [m for m in self.messages if m.get("role") == "system"]
        self.compaction_summary = None

    def export(self) -> List[Dict[str, Any]]:
        return [{k: v for k, v in m.items() if not k.startswith("_")} for m in self.messages]


class EnvironmentCapabilities:
    """Detects, on whatever machine this process is actually running on,
    which native mobile build toolchains are genuinely installed —
    rather than letting the model assume based on OS name alone.

    ROOT CAUSE this addresses: an agent operating purely from a system
    prompt has no way to know whether `flutter`, Android SDK/Gradle, or
    Xcode actually exist on the host without either (a) trying a command
    and burning a turn on a possibly-confusing failure, or (b) being told
    up front. This does the detection once, at session start, exactly
    like a human developer would run `flutter doctor` / `xcodebuild
    -version` before starting mobile work — and reports capability
    per-target (web, android, ios) so the model can commit to a scope
    that's actually achievable on THIS machine instead of discovering
    the gap mid-implementation.

    iOS is deliberately reported as impossible on any non-Darwin
    platform.system() outright — this is not a "maybe install it" gap
    like Android's SDK. Xcode requires macOS by Apple's own licensing
    and tooling; there is no code path here or anywhere that changes
    that, so this class hardcodes that fact instead of doing a
    which()-only check that could misleadingly imply it's just a
    missing-install problem on Windows/Linux.
    """

    @staticmethod
    def _which_version(binary: str, version_args: List[str]) -> Optional[str]:
        path = shutil.which(binary)
        if not path:
            return None
        try:
            result = subprocess.run(
                [path, *version_args], capture_output=True, text=True,
                timeout=15, encoding="utf-8", errors="replace",
            )
            first_line = (result.stdout or result.stderr or "").strip().splitlines()
            return first_line[0].strip() if first_line else path
        except Exception:
            return path

    @classmethod
    def detect(cls) -> Dict[str, Any]:
        system = platform.system()  # 'Darwin', 'Linux', 'Windows'
        is_macos = system == "Darwin"

        node_ver = cls._which_version("node", ["--version"])
        web = {
            "available": bool(node_ver),
            "detail": f"node {node_ver}" if node_ver else "node not found on PATH",
        }

        flutter_ver = cls._which_version("flutter", ["--version"])
        java_present = shutil.which("java") is not None
        javac_present = shutil.which("javac") is not None
        gradle_ver = cls._which_version("gradle", ["--version"])
        android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        android_sdk_present = bool(android_home and Path(android_home).is_dir())

        android_reasons = []
        if not (javac_present):
            android_reasons.append(
                "no JDK found (`javac` missing — `java` alone is a runtime, not a "
                "compiler; Gradle needs a full JDK)"
            )
        if not android_sdk_present:
            android_reasons.append(
                "no Android SDK found (ANDROID_HOME/ANDROID_SDK_ROOT not set or "
                "doesn't point at an existing directory)"
            )
        if not gradle_ver:
            android_reasons.append(
                "no standalone `gradle` on PATH (a project's own ./gradlew wrapper "
                "may still work if the JDK/SDK gaps above are fixed — it downloads "
                "its own Gradle version)"
            )
        android = {
            "available": javac_present and android_sdk_present,
            "detail": (
                "; ".join(android_reasons) if android_reasons else
                f"JDK present, SDK at {android_home}, gradle {gradle_ver or '(via gradlew only)'}"
            ),
            "flutter": flutter_ver,
        }

        if not is_macos:
            ios = {
                "available": False,
                "detail": (
                    f"impossible on this host — iOS builds require Xcode, which "
                    f"only runs on macOS (this machine reports '{system}'). This is "
                    f"not a missing-install gap; no version of this agent, on this "
                    f"OS, can ever produce a real iOS build. A Mac (local or CI "
                    f"runner such as GitHub Actions macos-latest / Codemagic / "
                    f"Bitrise) is required."
                ),
            }
        else:
            xcode_ver = cls._which_version("xcodebuild", ["-version"])
            pod_ver = cls._which_version("pod", ["--version"])
            ios_reasons = []
            if not xcode_ver:
                ios_reasons.append("Xcode command-line tools not found (run `xcode-select --install`)")
            if not pod_ver:
                ios_reasons.append("CocoaPods not found (needed for most React Native iOS builds)")
            ios = {
                "available": bool(xcode_ver),
                "detail": "; ".join(ios_reasons) if ios_reasons else f"{xcode_ver}, pod {pod_ver}",
            }

        return {
            "system": system,
            "web": web,
            "android": android,
            "ios": ios,
            "flutter_installed": bool(flutter_ver),
        }

    @classmethod
    def report_text(cls) -> str:
        caps = cls.detect()

        def line(label: str, cap: Dict[str, Any]) -> str:
            mark = "AVAILABLE" if cap["available"] else "NOT AVAILABLE"
            return f"  {label}: {mark} — {cap['detail']}"

        return (
            f"ENVIRONMENT CAPABILITY REPORT (detected on this exact host, OS={caps['system']}):\n"
            f"{line('Web (Node/npm/build/test)', caps['web'])}\n"
            f"{line('Android (Gradle build)', caps['android'])}\n"
            f"{line('iOS (Xcode build)', caps['ios'])}\n"
            "This report reflects THIS machine right now, not a hypothetical one. "
            "Do not claim a mobile build succeeded, was verified, or is "
            "'production ready' for a target marked NOT AVAILABLE above — you may "
            "still WRITE source code for it (Flutter/RN/native), but you cannot "
            "compile, run, sign, or test it, and must say so explicitly rather than "
            "implying otherwise. If the user re-runs this agent on a different "
            "machine, re-check this report there — it can and will differ."
        )


SYSTEM_PROMPT_TEMPLATE = """You are Neon Architect, an expert autonomous software engineering team.
Your mission: research, plan, write, edit, test, and ship code using TDD — with minimal wasted turns.

CRITICAL RULES (anti-fumble):
1. ALWAYS use the native JSON function calling API for tools. Never output raw XML for tool calls.
2. If you need a tool, CALL IT in the same response. Do NOT say "Let me..." and stop.
3. ONE focused tool action per turn when exploring. Do not re-run the same failed command.
4. If a tool fails twice the same way, CHANGE STRATEGY (different tool, query, or path).
5. Prefer web_search + browse_page over the run tool for internet research (run is for local dev programs only, not curl-style fetching).
6. Read before edit. Search before creating duplicate files.
7. Keep working memory accurate: facts you already know need not be rediscovered.
8. Phase completion is decided by the orchestrator via gates (PLAN.md, ARCHITECTURE.md, todos, tests) — do not claim a phase is done without producing those artifacts.
9. AUTOPILOT: NEVER ask the user questions, never wait for confirmation, never say "Shall I…?", "Ready?", "Would you like…?", "Proceed to implementation?". The orchestrator advances phases. CALL TOOLS only — no permission requests.
10. Paths: use project-relative paths only (e.g. src/app.py, PLAN.md). Never use /god_ai/... or other absolute Unix paths on Windows; cwd is already the project folder.
11. todo add ALWAYS needs content: {{"action":"add","content":"short task text"}}. Never call todo with only action.
12. write path must be a plain relative path string (PLAN.md or backend/foo.py). Never put markdown or "content:" inside path.
13. In IMPLEMENTATION: stop editing PLAN.md/ARCHITECTURE.md; write application code under src/, app/, backend/, frontend/, lib/, or tests/. On a FRESH project with no existing package dirs: create the package scaffolding FIRST — the source package dir (e.g. backend/__init__.py) plus a minimal-but-real module — BEFORE writing tests/ files that import from it. TDD's "write a failing test first" means the test should fail on an ASSERTION (wrong behavior), not on ModuleNotFoundError/ImportError (the package doesn't exist yet) — a missing module is not a meaningful RED state, it's a setup gap. If pytest fails with ModuleNotFoundError, that means required __init__.py or module files are missing, not that the code under test is wrong — go create the missing package files, don't just re-run the same test.
14. The run tool takes program (enum) + args (list) — NOT a shell command string. There is no `bash`/`cmd` tool and no shell syntax (pipes, &&, redirection, cd) anywhere in this toolset, on any OS ({os_name} or otherwise). Use the native `read` and `ls` tools for file checks; use `run` only to invoke python/pytest/pip/npm/npx/node/git/cargo/go with explicit argv arguments, e.g. run(program="pytest", args=["-v"]).
15. A green test suite is not proof of a working feature if the test doesn't assert anything real, and a function is not "implemented" if it just returns a literal or does nothing. Do NOT write `assert True`, `assert 1`, `assertTrue(True)`, or a test function with no assertion at all — these pass without checking any behavior and will be rejected. Do NOT write product functions whose entire body is `pass`, `...`, or `return` of a bare literal/empty value with no real logic — that is a stub, not an implementation, even if it doesn't match a TODO/FIXME comment. Every test must assert a specific, checkable outcome (a return value, a raised exception, a side effect) tied to the acceptance criteria in PLAN.md. Every product function must do the actual work its name promises — validation, computation, I/O, error handling — not a placeholder shape of it. validate_project's static scan checks for exactly this pattern and will block the phase if most of a file's functions or tests are this shallow.

TDD: RED (failing test) → GREEN (minimum code) → REFACTOR. Run tests after changes.

PERSONA: Architect | Designer | Engineer | Tester | Debugger | Reviewer | DevOps — stay in the injected persona.

WORKING DIRECTORY: {project_dir}
CURRENT MODEL: {model_name}
CURRENT OS: {os_name}

Tools:
- read / write / edit — local files (edit returns a diff)
- search / glob / ls / modules — local discovery & import graph
- run(program, args, cwd_subpath, timeout) — invoke a known dev program (python, pytest, pip, npm, npx, node, git, cargo, go) with explicit argv arguments. Not a shell: no pipes, no &&, no redirection, no cd.
- web_search(query, num_results) — internet search
- browse_page(url, max_chars, start_line) — fetch page text
- todo — task tracker
- task — specialist explorer (search/read summary; does not write)

External products: web_search first; confirm real domain (e.g. oiioii.ai vs oioi.com) before cloning.
Autopilot: planning → architecture → design → implementation → testing → review → deployment → verification.
"""

def _default_project_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "goal": None,
        "phase": "planning",
        "completed_phases": [],
        "autopilot_active": False,
        "autopilot_resume": False,
        "sdlc_phase_idx": 0,
        "stack": [],
        "entrypoints": [],
        "test_command": None,
        "key_modules": [],
        "last_failures": [],
        "open_todos": [],
        "decisions": [],
        "blockers": [],
        "next_action": None,
        "research_notes": [],
        "src_write_paths": [],
        "acceptance_criteria": [],
        "updated_at": None,
    }


def load_project_state(project_dir: Path, filename: str = ".neon_project_state.json") -> Dict[str, Any]:
    state = _default_project_state()
    fp = project_dir / filename
    try:
        if fp.is_file():
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state.update({k: data[k] for k in state.keys() if k in data})
    except Exception:
        pass
    return state


def save_project_state(project_dir: Path, state: Dict[str, Any], filename: str = ".neon_project_state.json") -> None:
    try:
        state = dict(state)
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        fp = project_dir / filename
        fp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass


# A section-name phrase only counts as a real section marker if it
# appears at the start of a line (after an optional heading marker,
# numbering, bullet, and/or markdown bold-open, in any combination),
# not buried mid-sentence. Without this, prose like "this project has
# no risks section; acceptance criteria: not written yet" satisfies
# both checks by substring match alone, even though it explicitly says
# neither section exists.
def _section_phrase_present(text_low: str, phrase: str) -> bool:
    pattern = re.compile(
        r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:\d+[.)]\s*)?(?:[-*]\s*)?(?:\*\*\s*)?"
        + re.escape(phrase),
        re.IGNORECASE,
    )
    return bool(pattern.search(text_low))


def plan_has_sections(text: str, headers: tuple) -> bool:
    low = (text or "").lower()
    if "auto-filled placeholder" in low or "todo: fill in" in low:
        return False
    synonyms = {
        "acceptance criteria": (
            "acceptance criteria", "acceptance criterion", "success criteria",
            "definition of done", "acceptance:",
        ),
        "requirements": (
            "requirements", "requirement analysis", "functional requirements",
            "product requirements", "scope",
        ),
        "risks": (
            "risks", "risk assessment", "risk & mitigation", "risks &",
            "risk mitigation", "known risks",
        ),
        "components": (
            "components", "component architecture", "system components",
            "modules", "service layout",
        ),
        "data flow": (
            "data flow", "dataflow", "architecture flow", "request flow",
            "system flow", "pipeline",
        ),
    }
    for h in headers:
        key = h.lower().strip()
        opts = synonyms.get(key, (key,))
        if not any(_section_phrase_present(low, o) for o in opts):
            return False
    return True


def plan_missing_sections(text: str, headers: tuple) -> List[str]:
    """Like plan_has_sections but returns the list of headers that are
    actually absent, so callers can deterministically repair the doc
    instead of only reporting pass/fail."""
    low = (text or "").lower()
    synonyms = {
        "acceptance criteria": (
            "acceptance criteria", "acceptance criterion", "success criteria",
            "definition of done", "acceptance:",
        ),
        "requirements": (
            "requirements", "requirement analysis", "functional requirements",
            "product requirements", "scope",
        ),
        "risks": (
            "risks", "risk assessment", "risk & mitigation", "risks &",
            "risk mitigation", "known risks",
        ),
        "components": (
            "components", "component architecture", "system components",
            "modules", "service layout",
        ),
        "data flow": (
            "data flow", "dataflow", "architecture flow", "request flow",
            "system flow", "pipeline",
        ),
    }
    missing = []
    for h in headers:
        key = h.lower().strip()
        opts = synonyms.get(key, (key,))
        if not any(_section_phrase_present(low, o) for o in opts):
            missing.append(h)
    return missing


# Same synonym set plan_has_sections/plan_missing_sections use for
# "acceptance criteria" specifically — kept as a single source of truth
# so heading-detection here and there can't silently drift apart.
_ACCEPTANCE_HEADING_SYNONYMS = (
    "acceptance criteria", "acceptance criterion", "success criteria",
    "definition of done", "acceptance:",
)

# Any markdown heading line (#, ##, ###, or a setext-style underline isn't
# handled here — PLAN.md is generated by the model, which consistently
# uses ATX-style # headings, so that's what both plan_has_sections and
# this extractor assume).
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
# Bullet/numbered list item lines: "- x", "* x", "1. x", "1) x"
_MD_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")


def extract_acceptance_criteria(text: str) -> List[str]:
    """Pull out the list items under PLAN.md's Acceptance Criteria heading
    as a real, structured list — not prose the model re-reads and
    re-judges later. This is the artifact the verification gate checks
    against, so it needs to exist independently of whatever the model
    later claims about "criteria met".

    Tolerates two ways a model commonly writes list items under the
    section: real bullets/numbered items (`- x`, `1. x`), and — since
    some models structure a flat plan doc with sub-headings acting as
    pseudo-bullets instead — a heading that is STRICTLY DEEPER than the
    Acceptance Criteria heading itself (e.g. `###` items under a `##`
    section, or `####` under `###`). A heading at the SAME level or
    shallower still correctly ends the section; only a deeper one is
    read as "this is a sub-point of the section", matching how such
    documents are actually structured even when newlines/list markers
    are inconsistent.

    Returns an empty list if no matching heading is found, or the
    heading exists but has no list items beneath it before the next
    heading (both are legitimate "nothing to extract" states — callers
    should not treat an empty list here as "gate satisfied trivially";
    see the verification gate itself for how it's used).
    """
    lines = (text or "").splitlines()
    in_section = False
    section_depth = 0
    items: List[str] = []
    for line in lines:
        m_head = _MD_HEADING_RE.match(line)
        if m_head:
            depth_m = re.match(r"^\s{0,3}(#{1,6})", line)
            depth = len(depth_m.group(1))
            title = m_head.group(1).strip().lower()
            if any(syn in title for syn in _ACCEPTANCE_HEADING_SYNONYMS):
                in_section = True
                section_depth = depth
                continue
            if in_section:
                if depth > section_depth:
                    # Deeper heading = pseudo-bullet under this section,
                    # not the next section starting. Treat its title text
                    # as the item, same as a real list item would be.
                    item = m_head.group(1).strip()
                    if item:
                        items.append(item)
                    continue
                # Same or shallower heading — the Acceptance Criteria
                # section has genuinely ended.
                break
            continue
        if in_section:
            m_item = _MD_LIST_ITEM_RE.match(line)
            if m_item:
                item = m_item.group(1).strip()
                if item:
                    items.append(item)
    return items


# VERIFICATION.md row format the model is instructed to produce, one row
# per stored acceptance criterion, e.g.:
#   - [x] Criterion text as stored — test: tests/test_auth.py::test_login_rejects_bad_password
# The bracket/check-state isn't load-bearing (a model could mark [ ] or
# [x] either way without it meaning anything reliable) — what's actually
# checked structurally is that a row exists whose text plausibly matches
# a stored criterion AND that row names a non-empty test reference after
# "test:". This is presence-checking, the same philosophy plan_has_sections
# uses for headers — it does not judge whether the named test actually
# exercises the criterion, only that the model was forced to name one
# specific test per criterion instead of asserting "all criteria pass" as
# an unstructured claim.
_VERIFICATION_ROW_RE = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])\s*(?:\[[ xX]\]\s*)?(.+?)\s*[—\-]+\s*test:\s*(\S.*?)\s*$"
)

# Markdown table row: | Criterion text | Status | test/path::name |
# (or any number of columns >= 2 — last non-empty cell is treated as the
# test reference, first non-empty cell as the criterion text). Header/
# separator rows ("| --- | --- |", "| Criterion | Status | Test |") are
# filtered out separately in parse_verification_rows since this regex
# alone can't distinguish a header row from a real data row.
_VERIFICATION_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_VERIFICATION_TABLE_SEPARATOR_RE = re.compile(r"^[\s|:\-]+$")
_VERIFICATION_TABLE_HEADER_WORDS = {
    "criterion", "criteria", "status", "test", "tests", "notes", "ac",
}


def _normalize_criterion_text(text: str) -> str:
    """Loose-match key for comparing a stored criterion against a
    VERIFICATION.md row's leading text — lowercased, punctuation-light,
    whitespace-collapsed. Exact-string matching would break on trivial
    rewording (e.g. the model paraphrasing the criterion slightly when
    copying it into VERIFICATION.md), so this compares on a reduced form
    instead of requiring a byte-for-byte match."""
    t = (text or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def parse_verification_rows(
    text: str, include_placeholders: bool = False
) -> List[Dict[str, str]]:
    """Extract (criterion_text, test_ref) pairs from VERIFICATION.md.
    Returns a list of {"criterion": str, "test_ref": str} dicts, one per
    recognized row. By default, rows that don't match a required format
    (missing the "test:" reference, or an unfilled placeholder like
    TODO_TEST_REF) are not returned — they don't count toward covering
    any criterion for gate-checking purposes.

    Set include_placeholders=True to also return rows whose test_ref is
    an unfilled placeholder. This exists so callers that need to know
    "does a row for this criterion already exist AT ALL on disk"
    (e.g. the auto-repair scaffolder deciding whether to append a new
    placeholder row) can tell a criterion with an existing-but-unfilled
    row apart from one with no row at all — the normal covered/missing
    check for the gate must keep ignoring placeholders, but the
    scaffolder must not treat "still a placeholder" as "still absent",
    or it will keep re-appending duplicate placeholder rows forever.

    Two formats are recognized: the bullet-list form
    ("- [x] <criterion> — test: <ref>") and a markdown table form
    ("| <criterion> | <status> | <ref> |"), since both are reasonable
    choices for this content and a model isn't told which one to use.
    Table header/separator rows are skipped by checking whether the
    first cell looks like one of the conventional header words (a real
    criterion's first cell will essentially never be exactly "Criterion"
    or "AC")."""
    rows = []
    for line in (text or "").splitlines():
        m = _VERIFICATION_ROW_RE.match(line)
        if m:
            criterion_text = m.group(1).strip()
            test_ref = m.group(2).strip()
            if criterion_text and test_ref and (
                include_placeholders or not _looks_like_placeholder_ref(test_ref)
            ):
                rows.append({"criterion": criterion_text, "test_ref": test_ref})
            continue
        m_table = _VERIFICATION_TABLE_ROW_RE.match(line)
        if m_table:
            if _VERIFICATION_TABLE_SEPARATOR_RE.match(m_table.group(1)):
                continue  # "| --- | --- | --- |" separator row
            cells = [c.strip().strip("`") for c in m_table.group(1).split("|")]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            first_key = cells[0].lower().strip("* []xX").strip()
            if first_key in _VERIFICATION_TABLE_HEADER_WORDS:
                continue  # header row, not data
            criterion_text = cells[0]
            test_ref = cells[-1]
            if criterion_text and test_ref and (
                include_placeholders or not _looks_like_placeholder_ref(test_ref)
            ):
                rows.append({"criterion": criterion_text, "test_ref": test_ref})
    return rows


def _looks_like_placeholder_ref(test_ref: str) -> bool:
    """True if a test_ref is (still) an unfilled scaffold value rather
    than a real test identifier — e.g. the literal TODO_TEST_REF this
    module's own auto-repair writes, or an obvious variant of it."""
    key = test_ref.strip().strip("`").upper()
    return key in ("TODO_TEST_REF", "TODO", "TBD", "N/A", "NONE") or key.startswith("TODO")


def match_criteria_to_verification(
    criteria: List[str], verification_rows: List[Dict[str, str]]
) -> Tuple[List[str], List[str]]:
    """Compare stored acceptance criteria against parsed VERIFICATION.md
    rows. Returns (covered, missing) — covered criteria (by original
    text) that have a matching row with a non-empty test reference, and
    missing criteria that don't. Matching is by normalized-text
    containment in either direction, since a criterion may be trimmed or
    lightly reworded when copied into VERIFICATION.md by the model, and
    exact-string matching would produce false "missing" results for
    what's actually the same criterion.

    The contained (shorter) side of a containment match must itself carry
    enough real content — at least 4 words or 20 characters — to count.
    Without this floor, a deliberately vague VERIFICATION.md row like
    "- [x] must — test: ..." or "- [x] test — test: ..." is a substring
    of nearly every real criterion and would spuriously mark all of them
    "covered" by one row, defeating the whole point of the linkage check.
    Genuine trimmed/paraphrased criteria (the case this containment logic
    exists for) are unaffected — they're long enough to clear the floor."""
    MIN_WORDS, MIN_CHARS = 4, 20
    normalized_rows = [
        (_normalize_criterion_text(r["criterion"]), r["test_ref"]) for r in verification_rows
    ]
    covered, missing = [], []
    for c in criteria:
        key = _normalize_criterion_text(c)
        found = False
        for row_key, _ in normalized_rows:
            if not key or not row_key:
                continue
            shorter, longer = (key, row_key) if len(key) <= len(row_key) else (row_key, key)
            if shorter not in longer:
                continue
            if len(shorter.split()) >= MIN_WORDS or len(shorter) >= MIN_CHARS:
                found = True
                break
        (covered if found else missing).append(c)
    return covered, missing


def parse_test_digest(digest: str) -> Dict[str, Any]:
    """Extract real pass/fail/error counts, and coverage percent if
    present, from a RunTool test_digest.

    The gate previously trusted a bare boolean (exit code 0 on any single
    recognized test invocation) as proof the phase's tests are green. That
    boolean says nothing about how many tests ran, so a single trivial
    passing test satisfied the same gate as a full suite. This parses the
    actual summary line each test runner prints so gates can require a
    real count instead of just an exit code.

    Coverage extraction is independent of which runner matched above,
    since coverage tooling (pytest-cov, coverage.py, jest --coverage/nyc,
    go test -cover) prints its own summary line alongside whichever
    runner produced it — it isn't a fifth "runner family", it's an
    optional addition to any of them. "coverage_pct" is None when no
    recognized coverage summary line was found (never 0 by default —
    0.0 is a real, distinct measurement and must not be confused with
    "not measured").

    Returns a dict: {"parsed": bool, "passed": int, "failed": int,
    "errors": int, "coverage_pct": Optional[float]} — "parsed" is False
    if no known test-summary format was recognized (callers should treat
    that as "unknown", not as "passed"). coverage_pct can be populated
    even when "parsed" is False, or absent even when "parsed" is True —
    the two are independent signals.
    """
    result = {
        "parsed": False, "passed": 0, "failed": 0, "errors": 0,
        "coverage_pct": None,
    }
    text = digest or ""
    if not text:
        return result

    # --- coverage line, checked first and independently of test-runner kind ---
    # coverage.py / pytest-cov: "TOTAL    120    15    88%"
    m_cov_total = re.search(r"^TOTAL\s+(?:\S+\s+)*?(\d+(?:\.\d+)?)%\s*$", text, re.MULTILINE)
    # go test -cover: "coverage: 82.4% of statements"
    m_cov_go = re.search(r"coverage:\s*(\d+(?:\.\d+)?)%\s+of\s+statements", text)
    # jest/nyc "All files" summary row: "All files    |   84.2 |..."
    m_cov_jest = re.search(r"All files\s*\|\s*(\d+(?:\.\d+)?)", text)
    # cargo-tarpaulin: "82.40% coverage, 412/500 lines covered"
    m_cov_cargo = re.search(r"(\d+(?:\.\d+)?)%\s+coverage", text)
    for m in (m_cov_total, m_cov_go, m_cov_jest, m_cov_cargo):
        if m:
            try:
                result["coverage_pct"] = float(m.group(1))
            except ValueError:
                pass
            break

    # pytest: "3 passed, 1 failed, 2 errors in 0.42s" (order/subset varies)
    m_passed = re.search(r"(\d+)\s+passed", text)
    m_failed = re.search(r"(\d+)\s+failed", text)
    m_errors = re.search(r"(\d+)\s+error", text)
    if m_passed or m_failed or m_errors:
        result["parsed"] = True
        result["passed"] = int(m_passed.group(1)) if m_passed else 0
        result["failed"] = int(m_failed.group(1)) if m_failed else 0
        result["errors"] = int(m_errors.group(1)) if m_errors else 0
        return result

    # jest: "Tests:       2 failed, 5 passed, 7 total"
    m_jest = re.search(r"Tests:\s*(?:(\d+)\s+failed,\s*)?(\d+)\s+passed", text)
    if m_jest:
        result["parsed"] = True
        result["failed"] = int(m_jest.group(1)) if m_jest.group(1) else 0
        result["passed"] = int(m_jest.group(2))
        return result

    # go test: "ok  	pkg	0.003s" (pass) or "FAIL	pkg	0.003s"
    if re.search(r"^ok\s", text, re.MULTILINE):
        result["parsed"] = True
        result["passed"] = max(1, len(re.findall(r"^ok\s", text, re.MULTILINE)))
        result["failed"] = len(re.findall(r"^FAIL\s", text, re.MULTILINE))
        return result
    if re.search(r"^FAIL\s", text, re.MULTILINE):
        result["parsed"] = True
        result["failed"] = max(1, len(re.findall(r"^FAIL\s", text, re.MULTILINE)))
        return result

    # cargo test: "test result: ok. 4 passed; 0 failed;"
    m_cargo = re.search(r"test result:\s*(ok|FAILED)\.\s*(\d+)\s+passed;\s*(\d+)\s+failed", text)
    if m_cargo:
        result["parsed"] = True
        result["passed"] = int(m_cargo.group(2))
        result["failed"] = int(m_cargo.group(3))
        return result

    return result


def goal_looks_external(goal: str) -> bool:
    g = (goal or "").lower()
    if not g:
        return False
    if re.search(r"\bhttps?://", g):
        return True
    if re.search(r"\b[a-z0-9-]+\.(com|ai|io|dev|app|sh|net|org)\b", g):
        return True
    if any(w in g for w in ("clone of", "clone", "replica of", "like ", "similar to")):
        return True
    return False


class NeonArchitect:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.project_dir = Path(config.get("project_dir", ".")).resolve()
        self.model_key = config.get("default_model", "glm-5.2")
        self.model_cfg = MODELS.get(self.model_key, MODELS["glm-5.2"])

        self.pool = build_pool(config)
        self.full_access = bool(config.get("full_access", True))
        self.tools = build_tools(
            self.project_dir,
            full_access=self.full_access,
            allow_outside=False,
            allow_local=bool(config.get("allow_local_network", True)),
            bash_confirm=bool(config.get("bash_confirm_destructive", False)),
        )
        self.config["allow_path_outside_project"] = False
        # ── Generation Layer: inject generate_app tool ───────────────────────
        try:
            if _GENERATION_LAYER_READY:
                _gen_tool = GenerateAppTool(self.project_dir, self.pool, self.config)
                self.tools[_gen_tool.name] = _gen_tool
                _deploy_tool = DeployTool(self.project_dir, self.config)
                self.tools[_deploy_tool.name] = _deploy_tool
        except Exception:
            pass  # generation layer not yet loaded (e.g. early import)

        self.ctx = ContextManager(
            compact_threshold=config.get("compact_threshold", 5000),
            ctx_window=self.model_cfg.get("ctx_window", 131072),
        )
        self.meter = TokenMeter()
        self.round = 0
        self.nudges = 0
        self._malformed_tool_streak = 0
        self.running = True

        self.goal = None
        self.persona_key = config.get("default_persona", "adaptive")
        self.persona_history: deque[str] = deque(maxlen=10)
        self.autopilot = False
        self._session_wants_resume = False
        self.sdlc_phase_idx = 0
        self.sdlc_completed: set[str] = set()
        self.autopilot_rounds = 0
        self.max_autopilot_rounds = int(config.get("max_autopilot_rounds", 0) or 0)
        self._phase_rounds = 0
        self._max_phase_rounds = int(config.get("max_phase_rounds", 0) or 0)
        self._phase_tool_counts: Dict[str, int] = {}
        self._phase_tool_success_counts: Dict[str, int] = {}
        # Per-path best (max) content length written this phase, keyed by
        # canonicalized project-relative path. _phase_chars_written is
        # derived from this via a property below — see there for why.
        self._phase_chars_by_path: Dict[str, int] = {}
        self._phase_bash_ok = False
        self._verification_scaffolded_criteria: set = set()
        self.current_turn_tool_calls = False
        self._last_persist = 0.0
        self._persist_every = 3
        self.plan_artifact = str(config.get("plan_artifact") or "PLAN.md")
        self.architecture_artifact = str(config.get("architecture_artifact") or "ARCHITECTURE.md")
        self.working_memory_file = str(config.get("working_memory_file") or ".neon_working_memory.md")
        self.max_same_tool_failures = int(config.get("max_same_tool_failures", 3) or 3)
        self.max_same_tool_successes = int(config.get("max_same_tool_successes", 3) or 3)
        self._tool_fail_counts: Dict[str, int] = {}
        self._tool_success_counts: Dict[str, int] = {}
        self._fp_last_error: Dict[str, str] = {}
        self._fp_pending_reflection: set = set()
        self._recent_tool_keys: deque = deque(maxlen=24)
        self._strategy_hints: List[str] = []
        self._rate_limit_until: float = 0.0
        self._recent_429_count: int = 0
        self.project_state_file = str(config.get("project_state_file") or ".neon_project_state.json")
        self.project_state: Dict[str, Any] = load_project_state(self.project_dir, self.project_state_file)
        self._validation_ready = bool(
            (self.project_state.get("validation") or {}).get("ready", False)
        )
        self._src_write_paths: List[str] = list(self.project_state.get("src_write_paths") or [])
        self._last_test_digest: str = ""
        self._research_done: bool = bool(self.project_state.get("research_notes"))
        self._tdd_saw_red: bool = False
        self._tdd_saw_green: bool = False
        self._consecutive_no_tool_turns: int = 0
        # NOTE: no bash-confirm-destructive handling here anymore — RunTool
        # has no shell string to scan for destructive patterns; it only ever
        # invokes a fixed enum of dev programs via argv with shell=False.

        restored = False
        skip_restore = bool(config.get("_fresh", False))
        try:
            if skip_restore:
                UI.info("Fresh session (--fresh / /new) — not restoring prior state")
                self._rebuild_system()
            else:
                prior = load_session(self.project_dir)
                if prior:
                    apply_session(self, prior)
                    restored = True
                else:
                    self._rebuild_system()
        except Exception as e:
            UI.warn(f"Could not restore session: {e}")
            self._rebuild_system()

        if not restored and not skip_restore:
            self._rebuild_system()

        normalize_thinking_policy(self.config)

        UI.separator()
        UI.ok(f"Initialized in {self.project_dir}")
        UI.info(
            "Permissions: project-folder only for files | "
            f"full_in_project={self.full_access} | "
            "run_tool=structured (program+args, no shell) | "
            f"local_net={bool(config.get('allow_local_network', True))} | "
            f"worktree={bool(config.get('git_worktree_on_autopilot'))} | "
            f"tdd={bool(config.get('tdd_enforce_red_green'))}"
        )
        UI.info(
            f"Thinking: {self.config.get('thinking_effort')} | "
            f"show={self.config.get('show_thinking')} | "
            f"first_token={self.config.get('first_token_timeout')}s"
        )
        UI.info(f"Model: {self.model_cfg['name']} ({self.model_cfg['id']})")
        UI.info(f"Persona: {PERSONAS[self.persona_key]['name']} (auto-switching)" if PERSONAS[self.persona_key]['auto_switch'] else f"Persona: {PERSONAS[self.persona_key]['name']}")
        try:
            caps = EnvironmentCapabilities.detect()
            UI.separator()
            UI.info(f"Build capability on THIS machine ({caps['system']}):")
            UI.ok(f"  Web:     {caps['web']['detail']}") if caps['web']['available'] else UI.warn(f"  Web:     {caps['web']['detail']}")
            UI.ok(f"  Android: {caps['android']['detail']}") if caps['android']['available'] else UI.warn(f"  Android: {caps['android']['detail']}")
            UI.ok(f"  iOS:     {caps['ios']['detail']}") if caps['ios']['available'] else UI.warn(f"  iOS:     {caps['ios']['detail']}")
            if not caps['android']['available'] or not caps['ios']['available']:
                UI.info(
                    "  Mobile targets marked above will get real source code, but "
                    "NOT a verified build, on this machine as configured."
                )
        except Exception as e:
            UI.warn(f"Could not run environment capability detection: {e}")
        if restored:
            UI.ok("Session restored from previous launch")
            if self.goal:
                UI.info(f"Goal: {self.goal}")
            if self.sdlc_completed or self.sdlc_phase_idx:
                phase = SDLC_PHASES[self.sdlc_phase_idx] if self.sdlc_phase_idx < len(SDLC_PHASES) else "COMPLETE"
                UI.info(f"SDLC phase: {phase.upper()} | completed: {', '.join(sorted(self.sdlc_completed)) or 'none'}")
            if TodoTool._todos:
                UI.info(f"Todos: {len(TodoTool._todos)} item(s) restored")
            n_msgs = len([m for m in self.ctx.messages if m.get('role') != 'system'])
            if n_msgs:
                UI.info(f"Conversation: {n_msgs} message(s) restored")
            est = self.ctx.estimate_tokens()
            floor = int(self.config.get("startup_compact_tokens", 40000))
            if est > floor:
                before = est
                keep = 4 if est > 200_000 else 6
                if self.ctx.compact(keep_recent=keep):
                    after = self.ctx.estimate_tokens()
                    UI.warn(f"Startup compact: ~{before:,} → ~{after:,} tokens")
                    self._rebuild_system()
                est2 = self.ctx.estimate_tokens()
                if est2 > floor * 2:
                    self.ctx.messages = [m for m in self.ctx.messages if m.get("role") == "system"]
                    self.ctx.compaction_summary = (
                        f"[Prior session truncated: was ~{before:,} tokens. Goal and todos kept.]"
                    )
                    self._rebuild_system()
                    UI.warn("Context still large — conversation history cleared (goal/todos kept). Use /reset for full wipe.")
            UI.info(f"Tokens so far: {self.meter.report()}")
        elif self.goal:
            UI.info(f"Goal (from config): {self.goal}")

    def _rebuild_system(self):
        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            project_dir=str(self.project_dir),
            model_name=self.model_cfg["name"],
            os_name=platform.system(),
        )
        # Detected fresh each time the system prompt is rebuilt (not
        # cached at import time) so it reflects the actual host this
        # process is running on right now — this matters because the
        # same agent file may be copied to and run on a different
        # machine than it was written/tested on, per the user's explicit
        # "whatever environment" requirement.
        prompt += f"\n\n[{EnvironmentCapabilities.report_text()}]"
        persona = PERSONAS.get(self.persona_key, PERSONAS["adaptive"])
        prompt += f"\n\n[CURRENT PERSONA: {persona['name']}]\n{persona['prompt']}"
        if self.goal:
            prompt += f"\n\n[CURRENT GOAL]\n{self.goal}\nFocus all actions on this goal."
        try:
            ps = self.project_state or {}
            snap = []
            if ps.get("test_command"):
                snap.append(f"test_command: {ps['test_command']}")
            if ps.get("entrypoints"):
                snap.append("entrypoints: " + ", ".join(ps["entrypoints"][:8]))
            if ps.get("key_modules"):
                snap.append("key_modules: " + ", ".join(ps["key_modules"][:12]))
            if ps.get("next_action"):
                snap.append(f"next_action: {ps['next_action']}")
            if ps.get("blockers"):
                snap.append("blockers: " + "; ".join(str(b) for b in ps["blockers"][-3:]))
            if snap:
                prompt += "\n\n[PROJECT STATE — durable]\n" + "\n".join(snap)
        except Exception:
            pass
        if self.autopilot and self.sdlc_phase_idx < len(SDLC_PHASES):
            phase = SDLC_PHASES[self.sdlc_phase_idx]
            phase_prompt = SDLC_PHASE_PROMPTS.get(phase, "")
            completed = ", ".join(self.sdlc_completed) if self.sdlc_completed else "none"
            prompt += (
                f"\n\n[SDLC PHASE: {phase.upper()}]\n{phase_prompt}\n"
                f"Completed: {completed}\n"
                "Turns are NOT limited by a fixed count. Produce required artifacts; orchestrator advances gates."
            )
            handoff = self._phase_handoff_block(phase)
            if handoff:
                prompt += f"\n\n{handoff}"
        wm = self._read_working_memory()
        if wm:
            prompt += f"\n\n[WORKING MEMORY — do not rediscover these facts]\n{wm}"
        if self._strategy_hints:
            prompt += "\n\n[STRATEGY CORRECTIONS — obey these]\n" + "\n".join(f"- {h}" for h in self._strategy_hints[-5:])
        if self._last_test_digest:
            prompt += f"\n\n[LAST TEST DIGEST — fix these failures]\n{self._last_test_digest[:3000]}"
        if self.ctx.compaction_summary:
            prompt += f"\n\n[COMPACTED HISTORY]\n{self.ctx.compaction_summary}"
        self.ctx.add_system(prompt)

    def _sync_project_state(self) -> None:
        try:
            st = self.project_state
            st["goal"] = self.goal
            st["phase"] = SDLC_PHASES[self.sdlc_phase_idx] if self.sdlc_phase_idx < len(SDLC_PHASES) else "complete"
            st["completed_phases"] = sorted(self.sdlc_completed)
            st["sdlc_phase_idx"] = int(self.sdlc_phase_idx)
            st["autopilot_active"] = bool(self.autopilot)
            if self.autopilot:
                st["autopilot_resume"] = True
            st["open_todos"] = [
                {"id": tid, **todo} for tid, todo in list(TodoTool._todos.items())[:40]
            ]
            st["src_write_paths"] = list(self._src_write_paths)[-40:]
            if self.project_state.get("validation") is not None:
                st["validation"] = self.project_state["validation"]
            if self._last_test_digest:
                fails = list(st.get("last_failures") or [])
                fails.append(self._last_test_digest[:500])
                st["last_failures"] = fails[-10:]
            save_project_state(self.project_dir, st, self.project_state_file)
        except Exception:
            pass

    def _record_validation_result(self, result: ToolResult) -> None:
        """Persist validator evidence, including failed reports.

        Validation intentionally returns a non-zero tool result when the
        project is not ready. Recording the JSON before the normal tool error
        bookkeeping keeps the exact blocker list available to the next model
        turn instead of reducing it to a generic tool failure.
        """
        try:
            payload = json.loads(result.text())
            report = payload.get("report") if isinstance(payload, dict) else None
            if not isinstance(report, dict):
                self._validation_ready = False
                return
            self.project_state["validation"] = report
            self._validation_ready = bool(report.get("ready"))
            checks = report.get("checks") or []
            test_checks = [c for c in checks if c.get("name") == "test"]
            if test_checks and all(bool(c.get("ok")) for c in test_checks):
                self._phase_bash_ok = True
                digest = str(report.get("test_digest") or "")
                if digest:
                    self._last_test_digest = digest
            if self._validation_ready:
                self._update_working_memory(
                    "production validation passed: static checks and discovered commands are green",
                    section="Decisions",
                )
            else:
                blockers = report.get("blockers") or []
                self._update_working_memory(
                    "production validation blocked: " + "; ".join(str(x) for x in blockers[:3]),
                    section="Blockers",
                )
        except (TypeError, ValueError, json.JSONDecodeError):
            self._validation_ready = False

    def _apply_phase_model(self, phase: str):
        """Swap the primary model for phases configured in phase_models
        (e.g. testing/verification -> a stronger model than default_model).
        Falls back to the user's chosen default_model for any phase without
        an override, and never touches self.model_key/config['default_model']
        itself — this is a temporary per-phase swap, not a user model change,
        so /model and this don't fight each other over what "the" model is.
        """
        phase_models = self.config.get("phase_models") or {}
        override = phase_models.get(phase)
        stale_keys = [k for k, v in phase_models.items() if v is not None and v not in MODELS]
        if stale_keys:
            for k in stale_keys:
                UI.warn(
                    f"phase_models['{k}'] = '{phase_models[k]}' is not a known model; "
                    "removing this stale override from saved config so it stops "
                    "reappearing on every run."
                )
            phase_models = {k: v for k, v in phase_models.items() if k not in stale_keys}
            self.config["phase_models"] = phase_models
            try:
                project_dir = Path(self.config.get("project_dir") or ".").resolve()
                root_cfg = self.config.get("_root")
                if not isinstance(root_cfg, dict):
                    root_cfg = load_config()
                    self.config["_root"] = root_cfg
                save_project_settings(root_cfg, project_dir, phase_models=phase_models)
            except Exception:
                pass
            if phase in stale_keys:
                override = None
        target_key = override or self.model_key
        current_id = self.pool.current().model_id if self.pool.providers else None
        target_id = MODELS.get(target_key, {}).get("id")
        if current_id == target_id:
            return
        try:
            self.pool = build_pool(self.config, model_key_override=override)
            label = MODELS.get(target_key, {}).get("name", target_key)
            if override:
                UI.info(f"Phase model: {phase.upper()} -> {label} (phase_models override)")
            else:
                UI.info(f"Phase model: {phase.upper()} -> {label} (default)")
        except Exception as e:
            UI.warn(f"Phase model switch for {phase} failed, keeping current pool: {e}")

    def _advance_sdlc(self) -> bool:
        current = SDLC_PHASES[self.sdlc_phase_idx]
        self.sdlc_completed.add(current)
        self.sdlc_phase_idx += 1
        self._phase_rounds = 0
        self._phase_tool_counts = {}
        self._phase_tool_success_counts = {}
        self._phase_chars_written = 0
        self._tool_fail_counts = {}
        self._tool_success_counts = {}
        self._fp_pending_reflection = set()
        # NOTE: _fp_last_error is deliberately NOT cleared here — it's a
        # cross-phase diagnostic trail so a later halt can still show what
        # actually failed, even if the phase gate reset the active counters.
        self._phase_bash_ok = False
        if current == "planning":
            # Capture PLAN.md's Acceptance Criteria as a real, structural
            # list now, at the moment planning's gate is confirmed met —
            # not re-derived later from prose when verification runs. This
            # is what closes the gap where the same model that wrote easy
            # acceptance criteria also certified them met, with nothing
            # structural linking the two: from here on, "criteria" means
            # this stored list, and verification is checked against it.
            plan_artifact = self.config.get("plan_artifact", "PLAN.md")
            plan_text = self._read_artifact_snippet(plan_artifact, max_chars=50000)
            criteria = extract_acceptance_criteria(plan_text)
            self.project_state["acceptance_criteria"] = criteria
            if criteria:
                self._update_working_memory(
                    f"captured {len(criteria)} acceptance criteria from {plan_artifact}",
                    section="Decisions",
                )
        self._sync_project_state()
        self._update_working_memory(f"phase complete: {current} → next")
        
        # Force a hard compaction at phase boundaries. This ensures the new phase
        # doesn't carry the dead weight/noise of the previous phase's trial-and-error
        # tool calls, giving the model a clean slate focused strictly on the new goal.
        before = self.ctx.estimate_tokens()
        if self.ctx.compact(keep_recent=4):
            after = self.ctx.estimate_tokens()
            UI.info(f"Phase transition forced compaction: ~{before:,} → ~{after:,} tokens")
            self._rebuild_system()

        if self.sdlc_phase_idx >= len(SDLC_PHASES):
            UI.ok("All SDLC phases complete — goal pipeline finished.")
            if self.autopilot:
                UI.ok("Autopilot stopping: full SDLC cycle done. Review output or /autopilot to continue.")
                self.autopilot = False
            try:
                self.project_state["autopilot_active"] = False
                self.project_state["autopilot_resume"] = False
                self._sync_project_state()
            except Exception:
                pass
            return False
        next_phase = SDLC_PHASES[self.sdlc_phase_idx]
        next_persona = SDLC_PERSONA_MAP.get(next_phase, "adaptive")
        self._switch_persona(next_persona, reason="auto")
        self._apply_phase_model(next_phase)
        UI.ok(f"SDLC: {current.upper()} complete → {next_phase.upper()}")
        if next_phase == "implementation":
            UI.info(
                "IMPLEMENTATION: write/edit real application code under the project dir "
                "(≥8 write/edit calls required before phase can complete). "
                "No more planning-only exploration."
            )
        elif next_phase == "testing":
            UI.info(
                "TESTING: use the run tool (program='pytest' or program='npm' args=['test']) "
                "to exit 0. No CI yaml spam; circuit-breaker after testing_max_phase_rounds."
            )
        self._rebuild_system()
        self.persist(force=True)
        return True

    def _start_autopilot(self, restart: bool = False):
        if not self.goal:
            UI.err("No goal set. Use /goal <description> first.")
            return False
        if self.sdlc_phase_idx >= len(SDLC_PHASES) and not restart:
            UI.ok(f"Goal is already complete — all {len(SDLC_PHASES)} SDLC phases finished.")
            UI.info("Use /autopilot restart for a full restart, or /goal <description> to set a new goal.")
            self.autopilot = False
            return False
        self.autopilot = True
        try:
            self.project_state["autopilot_active"] = True
            self.project_state["autopilot_resume"] = True
            self._sync_project_state()
        except Exception:
            pass
        if restart:
            self.sdlc_phase_idx = 0
            self.sdlc_completed.clear()
            self.autopilot_rounds = 0
            self.round = 0
            self._phase_tool_counts = {}
            self._phase_tool_success_counts = {}
            self._phase_chars_written = 0
            self._phase_bash_ok = False
            self._verification_scaffolded_criteria = set()
            if hasattr(self, "_tool_fail_counts"):
                self._tool_fail_counts = {}
            if hasattr(self, "_tool_success_counts"):
                self._tool_success_counts = {}
            if hasattr(self, "_strategy_hints"):
                self._strategy_hints = []
            if hasattr(self, "_fp_last_error"):
                self._fp_last_error = {}
            if hasattr(self, "_fp_pending_reflection"):
                self._fp_pending_reflection = set()
            UI.info("Autopilot starting from PLANNING (fresh cycle).")
        else:
            UI.info(
                f"Autopilot resuming at {SDLC_PHASES[self.sdlc_phase_idx].upper()} "
                f"(completed: {', '.join(sorted(self.sdlc_completed)) or 'none'}). "
                "Use /autopilot restart for a full restart."
            )
        self._phase_rounds = 0
        phase = SDLC_PHASES[min(self.sdlc_phase_idx, len(SDLC_PHASES) - 1)]
        initial_persona = SDLC_PERSONA_MAP.get(phase, "adaptive")
        self._switch_persona(initial_persona, reason="auto")
        self._apply_phase_model(phase)
        self._rebuild_system()
        UI.separator("GOD AUTOPILOT ENGAGED")
        UI.ok(f"Goal: {self.goal}")
        UI.info(f"Current SDLC phase: {phase.upper()}")
        UI.info("SDLC: planning → architecture → design → implementation → testing → review → deployment → verification")
        UI.info(
            f"Gates: {getattr(self, 'plan_artifact', 'PLAN.md')} + todos; "
            f"{getattr(self, 'architecture_artifact', 'ARCHITECTURE.md')} + stubs. "
            "No fixed turn force-advance."
        )
        UI.info("Type /stop to halt, /status for progress")
        return True

    def _stop_autopilot(self):
        if not self.autopilot:
            UI.info("Autopilot is not active.")
            return
        self.autopilot = False
        phase = SDLC_PHASES[self.sdlc_phase_idx] if self.sdlc_phase_idx < len(SDLC_PHASES) else "COMPLETE"
        try:
            self.project_state["autopilot_active"] = False
            self.project_state["autopilot_resume"] = self.sdlc_phase_idx < len(SDLC_PHASES) and bool(self.goal)
            self._sync_project_state()
        except Exception:
            pass
        UI.separator("AUTOPILOT HALTED")
        UI.info(f"Stopped at phase: {phase.upper()}")
        UI.info(f"Completed: {', '.join(self.sdlc_completed) if self.sdlc_completed else 'none'}")
        recent_errors = list(dict.fromkeys(self._fp_last_error.values()))[-5:]
        if recent_errors:
            UI.info("Last distinct errors seen before halt (most recent last):")
            for e in recent_errors:
                UI.info(f"  - {e}")
        UI.info("Type /autopilot to resume from current phase (also offered on next launch).")
        self.persist(force=True)

    def _autopilot_status(self):
        if not self.goal:
            UI.info("No goal set.")
            return
        if not self.autopilot:
            UI.info("Autopilot is OFF. Goal is set but not being actively pursued.")
        current = SDLC_PHASES[self.sdlc_phase_idx] if self.sdlc_phase_idx < len(SDLC_PHASES) else "COMPLETE"
        UI.separator("AUTOPILOT STATUS")
        UI.info(f"Goal: {self.goal}")
        UI.info(f"Active: {'YES' if self.autopilot else 'NO'}")
        UI.info(f"Current phase: {current.upper()}")
        UI.info(f"Rounds: {self.autopilot_rounds}")
        UI.info(f"Completed: {', '.join(self.sdlc_completed) if self.sdlc_completed else 'none'}")
        remaining = [p for p in SDLC_PHASES if p not in self.sdlc_completed]
        UI.info(f"Remaining: {', '.join(remaining)}")

    def _read_artifact_snippet(self, relative_path: str, max_chars: int = 3500) -> str:
        try:
            fp = (self.project_dir / relative_path).resolve()
            fp.relative_to(self.project_dir.resolve())
            if not fp.is_file():
                return ""
            text = fp.read_text(encoding="utf-8", errors="replace").strip()
            return text if len(text) <= max_chars else text[:max_chars] + f"\n… [{len(text)} chars total]"
        except Exception:
            return ""

    def _todo_handoff_text(self) -> str:
        if not TodoTool._todos:
            return "(no todos yet)"
        return "\n".join(
            f"- [{t.get('status','pending')}] {tid}: {t.get('content','')}"
            for tid, t in TodoTool._todos.items()
        )

    def _phase_handoff_block(self, phase: str) -> str:
        parts: List[str] = []
        plan_name = getattr(self, "plan_artifact", "PLAN.md")
        arch_name = getattr(self, "architecture_artifact", "ARCHITECTURE.md")
        if phase != "planning":
            plan = self._read_artifact_snippet(plan_name, max_chars=2500)
            parts.append(f"[PLAN — follow]\nSource: {plan_name}\n{plan}" if plan else f"[PLAN MISSING] {plan_name}")
        if phase not in ("planning", "architecture"):
            arch = self._read_artifact_snippet(arch_name, max_chars=2500)
            parts.append(f"[ARCHITECTURE — follow]\nSource: {arch_name}\n{arch}" if arch else f"[ARCHITECTURE MISSING] {arch_name}")
        parts.append(f"[TODOS]\n{self._todo_handoff_text()}")
        return "\n\n".join(parts)

    def _working_memory_path(self) -> Path:
        return self.project_dir / getattr(self, "working_memory_file", ".neon_working_memory.md")

    def _read_working_memory(self) -> str:
        try:
            fp = self._working_memory_path()
            if fp.is_file():
                return fp.read_text(encoding="utf-8", errors="replace").strip()[:4000]
        except Exception:
            pass
        return ""

    def _update_working_memory(self, note: str, section: str = "Facts") -> None:
        try:
            fp = self._working_memory_path()
            prev = self._read_working_memory()
            sections = {"Facts": [], "Decisions": [], "Blockers": [], "Next action": []}
            cur = "Facts"
            if prev:
                for ln in prev.splitlines():
                    s = ln.strip()
                    if s.startswith("## "):
                        title = s[3:].strip()
                        if title in sections:
                            cur = title
                        continue
                    if s.startswith("#"):
                        continue
                    if s:
                        sections.setdefault(cur, []).append(s)
            stamp = datetime.now().strftime("%H:%M:%S")
            entry = f"- [{stamp}] {note.strip()[:300]}"
            sec = section if section in sections else "Facts"
            sections[sec].append(entry)
            for k in list(sections.keys()):
                sections[k] = sections[k][-15:]
            body_lines = ["# Neon working memory (auto)", ""]
            for title in ("Facts", "Decisions", "Blockers", "Next action"):
                body_lines.append(f"## {title}")
                body_lines.extend(sections.get(title) or ["- (none)"])
                body_lines.append("")
            fp.write_text("\n".join(body_lines), encoding="utf-8")
            with ReadTool._cache_lock:
                ReadTool._cache.pop(str(fp.resolve()), None)
            if section == "Decisions":
                self.project_state.setdefault("decisions", []).append(note[:200])
                self.project_state["decisions"] = self.project_state["decisions"][-20:]
            elif section == "Blockers":
                self.project_state.setdefault("blockers", []).append(note[:200])
                self.project_state["blockers"] = self.project_state["blockers"][-10:]
            elif section == "Next action":
                self.project_state["next_action"] = note[:300]
        except Exception:
            pass

    def _normalize_bash_command(self, cmd: str) -> str:
        c = (cmd or "").strip().lower()
        c = re.split(r"\s+\|\s*", c)[0]
        c = re.split(r"\s+2>&1\b", c)[0]
        c = re.split(r"\s+>\s*", c)[0]
        c = re.sub(r"\s+", " ", c).strip()
        if " && " in c:
            parts = [p.strip() for p in c.split(" && ") if p.strip()]
            parts = [p for p in parts if not re.match(r"^cd\s+", p)]
            c = " && ".join(parts) if parts else c
        return c[:200]

    def _suspicious_bare_args_reason(self, name: str, args: Dict[str, Any]) -> str:
        """Detect a tool call whose JSON parsed cleanly but where only one
        required field (out of several) actually has a non-empty value —
        e.g. path populated, content left "". This is the shape seen when a
        model (typically a weaker fallback under provider-switch stress)
        garbles a multi-field tool call by dumping everything into one slot
        instead of filling in each argument properly. Returns a reason
        string if suspicious, else ''.
        """
        tool_obj = self.tools.get(name)
        if tool_obj is None or not isinstance(args, dict):
            return ""
        required = list((tool_obj.parameters or {}).get("required") or [])
        if len(required) < 2:
            return ""

        def _empty(k: str, v: Any) -> bool:
            if k == "content" and v == "":
                # An explicit empty string is a deliberate value (e.g. write(...,
                # content="") to truncate/clear a file) — not evidence of a
                # garbled call. Missing/None/"null" content is still suspicious.
                return False
            return str(v if v is not None else "").strip() in ("", "{}", "null", "None")

        populated = sum(1 for k in required if not _empty(k, args.get(k)))
        if populated <= 1:
            empty_fields = [k for k in required if _empty(k, args.get(k))]
            return (
                f"only {populated}/{len(required)} required fields populated "
                f"(empty: {empty_fields}). Looks like a malformed tool call, not "
                f"an intentional one — re-issue with all required fields filled in."
            )
        return ""

    # A canonical path already survived _sanitize_rel_path but may still carry
    # a junk suffix bolted on by a model that's dodging a write/edit refusal
    # by renaming rather than fixing content (test_ac1_auth.py.newtmp2023...,
    # ...lock20240801103257-0-003..., ...locked...). None of these ever reach
    # disk (_degenerate_path_reason rejects them), but each one is still a
    # DIFFERENT string, so keying the spam fingerprint on the raw canonical
    # path lets an unlimited number of them dodge the 3-strikes counter.
    # Strip back to "<original stem>.<original ext>" so every mutation of the
    # same intended file collapses onto one fingerprint.
    _PATH_JUNK_SUFFIX_RE = re.compile(
        r"\.(newtmp|tmp|lock|locked|backup|dump|bak)[\w.\-]*$", re.IGNORECASE
    )

    @classmethod
    def _path_family_root(cls, path: str) -> str:
        prev = None
        cur = path
        # Strip repeatedly: "foo.py.locked2024...tmp" needs more than one pass.
        while prev != cur:
            prev = cur
            cur = cls._PATH_JUNK_SUFFIX_RE.sub("", cur)
        return cur or path

    def _tool_fingerprint(self, name: str, args: Dict[str, Any]) -> str:
        key_args: Dict[str, Any] = {}
        if name == "run":
            # Structured fingerprint: keyed directly on the program enum and
            # the argv list, rather than normalizing a shell command string
            # (there is no command string in this tool's schema at all).
            prog = str(args.get("program") or "")
            run_args = args.get("args") or []
            key_args["program"] = prog
            key_args["args"] = json.dumps(run_args, sort_keys=True, ensure_ascii=False)[:150]
            cwd_sub = args.get("cwd_subpath")
            if cwd_sub:
                key_args["cwd_subpath"] = str(cwd_sub)[:100]
            return f"{name}:{json.dumps(key_args, sort_keys=True, ensure_ascii=False)}"
        for k in ("path", "command", "query", "url", "pattern", "action", "id"):
            if k in args and args[k] is not None:
                val = str(args[k])[:200]
                if k == "command":
                    val = self._normalize_bash_command(val)
                if k == "path" and name in ("write", "edit", "read"):
                    # Canonicalize hallucinated/malformed paths the same way the
                    # Tool base class does before touching disk. Without this,
                    # dozens of cosmetically-different mangled variants of the
                    # same real target (e.g. "PLAN.md, content: ...",
                    # "PLAN.md#Risks:", "PLAN.md, risks: ...") each get their
                    # own fresh fingerprint and their own 2-3 retry budget,
                    # letting the model loop past the anti-spam breaker
                    # indefinitely without ever fixing the real problem.
                    canon = Tool._sanitize_rel_path(val)
                    val = canon or val
                    # Further collapse to the FILENAME ROOT: a model dodging a
                    # rejection by mutating the path itself (test_ac1_auth.py
                    # -> test_ac1_auth.py.newtmp2023... -> ...lock2024...)
                    # produces a different canonical path on every single
                    # attempt, so the fingerprint above never repeats and the
                    # 3-strikes counter never accumulates. Strip any accreted
                    # junk suffix back to the original stem+ext so all such
                    # mutations collapse onto ONE fingerprint and actually
                    # trip the spam breaker.
                    val = self._path_family_root(val)
                key_args[k] = val
        if name in ("write", "todo") and args.get("content") is not None:
            c = str(args.get("content") or "")
            # Key on shape, not exact text. A model retrying a rejected
            # collapsed-source write rarely resends byte-identical content —
            # it re-orders imports, adds/removes a trailing space, etc. Exact
            # text (or even a raw prefix) gives each cosmetic variant its own
            # fingerprint and lets it dodge the failure counter indefinitely.
            # "Collapsed-ness" (near-zero newlines relative to length) is the
            # actual failure mode, so key on that shape plus a coarse length
            # bucket instead of the literal text.
            newline_bucket = "collapsed" if (len(c) > 40 and c.count("\n") < 3) else "normal"
            key_args["content"] = f"{newline_bucket}|len~{len(c) // 50 * 50}"
        if name == "edit":
            if args.get("old_text") is not None:
                ot = str(args.get("old_text") or "")
                key_args["old_text"] = ot[:60] + f"|len={len(ot)}"
            if args.get("new_text") is not None:
                nt = str(args.get("new_text") or "")
                key_args["new_text"] = nt[:60] + f"|len={len(nt)}"
        return f"{name}:{json.dumps(key_args, sort_keys=True, ensure_ascii=False)}"

    def _effective_inter_turn_delay(self) -> float:
        base = float(self.config.get("inter_turn_delay", 0.35) or 0.35)
        now = time.monotonic()
        if now < float(getattr(self, "_rate_limit_until", 0.0) or 0.0):
            return max(base, 5.0)
        n429 = int(getattr(self, "_recent_429_count", 0) or 0)
        if n429 > 0:
            return max(base, min(20.0, 3.0 * n429))
        return base

    def _note_provider_success(self) -> None:
        n = int(getattr(self, "_recent_429_count", 0) or 0)
        if n > 0:
            self._recent_429_count = max(0, n - 1)

    def _autopilot_prompt(self) -> str:
        if self.sdlc_phase_idx >= len(SDLC_PHASES):
            return "SDLC complete. Verify PLAN.md acceptance criteria and summarize deliverables."
        phase = SDLC_PHASES[self.sdlc_phase_idx]
        plan_name = getattr(self, "plan_artifact", "PLAN.md")
        arch_name = getattr(self, "architecture_artifact", "ARCHITECTURE.md")
        prompts = {
            "planning": (
                f"AUTOPILOT PLANNING — do NOT ask the user anything. "
                f"(1) web_search external product + browse_page top URLs. "
                f"(2) Write {plan_name} that MUST contain headings: Requirements, Risks, Acceptance Criteria "
                f"(exact words help the gate). (3) todo add ≥5. Do NOT implement app code. "
                f"If {plan_name} already exists and is complete, do NOT summarize for the user — "
                f"call a tool to fix any missing section headers, or wait for orchestrator advance. "
                f"Call a tool NOW."
            ),
            "architecture": (
                f"AUTOPILOT ARCHITECTURE — do NOT ask the user. Follow {plan_name}. "
                f"Write {arch_name} with headings Components and Data Flow + interface stubs (≥2 write/edit). "
                f"No full features. Call a tool NOW."
            ),
            "design": f"DESIGN following {plan_name}/{arch_name}. ≥3 write/edit. Call a tool NOW.",
            "implementation": (
                f"IMPLEMENT following {plan_name}/{arch_name} with TDD (RED→GREEN). "
                f"Write REAL application source under src/, backend/, frontend/, tests/ — "
                f"NOT more DESIGN.md/SPEC.md essays. "
                f"Each write MUST be SMALL (<4000 chars). Use append=true only to continue the SAME file. "
                f"Never dump an entire design system in one write. "
                f"≥8 write/edit with ≥4 under src/app/backend/frontend/lib/tests. "
                f"If providers time out, write an even smaller next file. Call a tool NOW."
            ),
            "testing": (
                f"TEST against {plan_name}. "
                f"FORBIDDEN: rewriting CI yaml repeatedly; re-running the same install/test invocation; "
                f"tiny fragment overwrites of existing files. "
                f"(1) Ensure package.json has a working test script OR use run(program='pytest'/'npx', args=[...]) once. "
                f"(2) Call run ONCE to execute tests. (3) On failure, fix the failing SOURCE with edit/full write, "
                f"then run again. Do NOT touch .github/workflows unless tests already pass. "
                f"Call a tool NOW."
            ),
            "review": f"REVIEW vs {plan_name}/{arch_name}. ≥3 read/edit/search. Call a tool NOW.",
            "deployment": f"DEPLOY aligned with {arch_name}. Dockerfile/compose/CI. Call a tool NOW.",
            "verification": (
                f"VERIFY all {plan_name} criteria. Run tests to exit 0. "
                f"Do not spam the same command — fix source then re-test. Call a tool NOW."
            ),
        }
        body = prompts.get(phase, "Continue the goal. Call a tool NOW.")
        handoff = self._phase_handoff_block(phase)
        hints = ""
        if self._strategy_hints:
            hints = "\n\n[STRATEGY — do not repeat failed approaches]\n" + "\n".join(
                f"- {h}" for h in self._strategy_hints[-4:]
            )
        return f"{body}\n\n{handoff}{hints}"

    def _detect_persona(self, user_input: str) -> Optional[str]:
        lower = user_input.lower()
        scores: Dict[str, int] = {}
        for persona_key, triggers in PERSONA_TRIGGERS.items():
            score = sum(1 for trigger in triggers if trigger in lower)
            if score > 0:
                scores[persona_key] = score
        if not scores:
            return None
        return max(scores, key=scores.get)

    def _switch_persona(self, new_persona: str, reason: str = "manual"):
        if new_persona not in PERSONAS:
            return False
        old_name = PERSONAS[self.persona_key]["name"]
        self.persona_key = new_persona
        self.persona_history.append(new_persona)
        self._rebuild_system()
        new_name = PERSONAS[new_persona]["name"]
        if reason == "auto":
            UI.info(f"Auto-switched: {old_name} → {new_name}")
        else:
            UI.ok(f"Switched to {new_name}")
            self.config["default_persona"] = new_persona
            self.persist(force=True)
        return True

    def persist(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_persist) < 2.0:
            return
        try:
            self._sync_project_state()
        except Exception:
            pass
        if save_session(self, also_config=True):
            self._last_persist = now

    def _build_payload(self) -> Dict[str, Any]:
        force_tool = bool(
            self.autopilot
            and (self._consecutive_no_tool_turns >= 1 or self._phase_rounds >= 1)
        )
        model_cfg = self.pool.current().model_cfg
        temperature = model_cfg.get("temperature")
        if temperature is None:
            temperature = self.config.get("temperature", 0.2)
        top_p = model_cfg.get("top_p")
        payload = {
            "model": self.pool.current().model_id,
            "messages": sanitize_messages_for_api(self.ctx.export()),
            "temperature": temperature,
            "max_tokens": model_cfg.get("max_tokens", 8192),
            "stream": True,
            "tools": tools_schema(self.tools),
            "tool_choice": "required" if force_tool else "auto",
        }
        if top_p is not None:
            payload["top_p"] = top_p
        effort = str(self.config.get("thinking_effort") or "off").lower()
        base_extra = model_cfg.get("extra_body")
        if base_extra:
            extra = dict(base_extra)
            ctk = dict(extra.get("chat_template_kwargs") or {})
            if effort in ("low", "medium", "high"):
                ctk["enable_thinking"] = True
            extra["chat_template_kwargs"] = ctk
            payload["extra_body"] = extra
        return payload

    def _consume_stream(self, provider: "Provider", payload: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]], Optional[str]]:
        # CRITICAL: deep-copy the payload for this attempt. The caller's retry loop
        # mutates its `payload` dict IN PLACE for the next attempt (new model_id,
        # temperature, etc.) as soon as this call returns/raises. The background
        # reader thread below closes over `payload` by reference — if that thread
        # is still alive (e.g. blocked in a slow HTTP call) when the next attempt
        # starts, it would otherwise send the WRONG provider's client with the
        # NEXT attempt's model_id already written into the shared dict. This is
        # confirmed by mismatched model/payload["model"] pairs found in dumped
        # failed_payloads/*.json. Working on an isolated copy makes each attempt
        # fully independent regardless of what the outer loop does afterward.
        payload = copy.deepcopy(payload)
        content = ""
        reasoning = ""
        tool_calls: List[Dict[str, Any]] = []
        finish_reason = None
        stream_timeout = float(self.config.get("stream_timeout", 300.0))
        content_timeout = float(self.config.get("stream_content_timeout", 90.0))
        
        try:
            _prompt_chars = sum(len(json.dumps(m, default=str)) for m in payload.get("messages", []))
            _prompt_chars += len(json.dumps(payload.get("tools") or [], default=str))
            est_prompt_tokens = _prompt_chars // 4
        except Exception:
            est_prompt_tokens = 0
        base_first_token_timeout = float(self.config.get("first_token_timeout", 8.0))
        size_allowance = max(0.0, (est_prompt_tokens - 4000) / 200.0)
        first_token_timeout = min(base_first_token_timeout + size_allowance, base_first_token_timeout + 300.0)
        stream_timeout = max(stream_timeout, first_token_timeout + 60.0)
        stream_start = time.monotonic()
        last_content_time = time.monotonic()
        in_think = False
        printed_len = 0
        last_heartbeat = 0
        saw_any_token = False

        chunk_q: "queue.Queue" = __import__("queue").Queue(maxsize=256)
        SENTINEL = object()
        reader_err: List[BaseException] = []
        resp_holder: List[Any] = []

        def _reader():
            try:
                resp = provider.client.chat.completions.create(**payload)
                resp_holder.append(resp)
                for chunk in resp:
                    chunk_q.put(chunk)
            except BaseException as ex:
                reader_err.append(ex)
                try:
                    dump_dir = Path.home() / ".neon_architect" / "failed_payloads"
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    dump_path = dump_dir / f"{int(time.time())}_{provider.model_id.replace('/', '_')}.json"
                    with open(dump_path, "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "model": provider.model_id,
                                "error": repr(ex),
                                "payload": payload,
                            },
                            f,
                            indent=2,
                            default=str,
                        )
                except Exception:
                    pass
            finally:
                chunk_q.put(SENTINEL)

        # Prune/track orphaned reader threads from prior attempts that timed out
        # app-side but never returned (e.g. still blocked in the HTTP call).
        # These no longer corrupt shared state (payload is deep-copied above),
        # but leaving them completely unmonitored can leak sockets/threads under
        # a sustained provider outage, so we keep a bounded, visible registry.
        orphans = getattr(self, "_orphan_reader_threads", None)
        if orphans is None:
            orphans = self._orphan_reader_threads = []
        orphans[:] = [th for th in orphans if th.is_alive()]
        if len(orphans) >= 6:
            UI.warn(
                f"{len(orphans)} abandoned provider-stream threads still running "
                "in the background (likely a stuck connection pool)."
            )

        t = threading.Thread(target=_reader, daemon=True, name="neon-stream-reader")
        t.start()

        try:
            while True:
                elapsed = time.monotonic() - stream_start
                if elapsed > stream_timeout:
                    raise Exception(
                        f"Stream hard timeout after {elapsed:.0f}s (limit: {stream_timeout:.0f}s)"
                    )
                if not saw_any_token and elapsed > first_token_timeout:
                    raise Exception(
                        f"First-token timeout after {elapsed:.0f}s (limit: {first_token_timeout:.0f}s) — "
                        f"backend is likely buffering the full reasoning phase server-side "
                        f"(thinking_effort={self.config.get('thinking_effort')}). Retrying."
                    )
                if not saw_any_token and elapsed >= 1.5 and int(elapsed) - last_heartbeat >= 1:
                    last_heartbeat = int(elapsed)
                    try:
                        UI.thinking_live(elapsed)
                    except Exception:
                        pass
                elif saw_any_token and elapsed > 20 and int(elapsed) - last_heartbeat >= 15:
                    last_heartbeat = int(elapsed)
                    try:
                        UI.streaming_pulse(elapsed)
                    except Exception:
                        pass

                idle = time.monotonic() - last_content_time
                if saw_any_token and idle > content_timeout:
                    raise Exception(
                        f"Content stall — no tokens for {content_timeout:.0f}s "
                        f"(thinking={in_think})"
                    )

                poll = min(2.0, content_timeout / 4.0)
                try:
                    chunk = chunk_q.get(timeout=poll)
                except Exception:
                    if not t.is_alive() and chunk_q.empty():
                        break
                    continue

                if chunk is SENTINEL:
                    break

                if not getattr(chunk, "choices", None):
                    continue

                delta = chunk.choices[0].delta
                if chunk.choices[0].finish_reason is not None:
                    finish_reason = chunk.choices[0].finish_reason

                if delta is None:
                    continue

                has_content = False

                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    has_content = True
                    saw_any_token = True
                    try:
                        UI.clear_live_line()
                    except Exception:
                        pass
                    if not in_think:
                        if self.config.get("show_thinking", True):
                            if HAS_RICH:
                                console.print(UI.thinking_block(""))
                            else:
                                print("* THINKING...")
                        in_think = True
                    reasoning += delta.reasoning_content
                    if self.config.get("show_thinking", True):
                        UI.stream_write(delta.reasoning_content, style="cc_thinking")

                if delta.content:
                    has_content = True
                    saw_any_token = True
                    if in_think:
                        UI.stream_flush_line()
                        in_think = False
                        if self.config.get("show_thinking", True) and HAS_RICH:
                            console.print(UI.thinking_panel(reasoning))
                    content += delta.content
                    new_text = content[printed_len:]
                    UI.stream_write(new_text, style="cc_body")
                    printed_len = len(content)

                if delta.tool_calls:
                    has_content = True
                    saw_any_token = True
                    if in_think:
                        UI.stream_flush_line()
                        in_think = False
                        if self.config.get("show_thinking", True) and HAS_RICH:
                            console.print(UI.thinking_panel(reasoning))
                    else:
                        UI.stream_flush_line()
                    for tc in delta.tool_calls:
                        idx = tc.index
                        while len(tool_calls) <= idx:
                            tool_calls.append({"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            tool_calls[idx]["id"] += tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls[idx]["name"] += tc.function.name
                                name = tool_calls[idx]["name"]
                                try:
                                    UI.clear_live_line()
                                except Exception:
                                    pass
                                if HAS_RICH:
                                    console.print(UI.tool_call(name, "..."))
                                else:
                                    print(f"\n* {name}(...)")
                            if tc.function.arguments:
                                tool_calls[idx]["arguments"] += tc.function.arguments

                if has_content:
                    last_content_time = time.monotonic()

            if reader_err:
                raise reader_err[0]

            if in_think:
                UI.stream_flush_line()
                in_think = False
                if self.config.get("show_thinking", True) and HAS_RICH:
                    console.print(UI.thinking_panel(reasoning))

        finally:
            try:
                if resp_holder:
                    resp = resp_holder[0]
                    close = getattr(resp, "close", None) or getattr(resp, "http_response", None)
                    if callable(close):
                        close()
                    elif close is not None and hasattr(close, "close"):
                        close.close()
            except Exception:
                pass
            if t.is_alive():
                orphans.append(t)

        print()
        raw_tool_call_count = len(tool_calls)
        for tc in tool_calls:
            if isinstance(tc, dict):
                tc["arguments"] = sanitize_tool_arguments(tc.get("arguments"))
                if not tc.get("name"):
                    tc["name"] = "unknown"
        tool_calls = [tc for tc in tool_calls if isinstance(tc, dict) and tc.get("name") and tc.get("name") != "unknown"]
        if raw_tool_call_count and not tool_calls:
            # The backend streamed one or more tool_calls deltas (so the
            # model clearly intended to call something — we saw an index
            # slot get created for it), but not one of them ever received a
            # function.name fragment, so every entry got filtered out above
            # as nameless. If we returned this as-is, the caller would see
            # a non-None finish_reason and treat the turn as a normal
            # successful no-tool-call response — the model's actual intent
            # silently vanishes with no retry, no warning, and no signal to
            # the model that its call didn't go through. This happens in
            # practice with non-conformant OpenAI-compatible backends
            # (local vLLM/SGLang servers, etc.) that don't reliably stream
            # function.name. Route it into the same retry path as a
            # dropped stream by clearing finish_reason, since it's the same
            # underlying failure: the backend claimed completion without
            # delivering a usable result.
            finish_reason = None
        return content, reasoning, tool_calls, finish_reason

    def _is_template_error(self, e: Exception) -> bool:
        s = str(e).lower()
        specific_markers = (
            "failed to apply prompt temp",
            "failed to apply prompt template",
            "prompt template",
            "apply_chat_template",
            "chat_template",
            "jinja",
            "tool call validation",
            "tools schema",
            "invalid tools",
        )
        return any(m in s for m in specific_markers)

    def _is_transient_error(self, e: Exception) -> bool:
        if self._is_template_error(e):
            return False
        status_code = getattr(e, "status_code", None)
        if status_code is None:
            resp = getattr(e, "response", None)
            status_code = getattr(resp, "status_code", None) if resp is not None else None
        if status_code in (404, 410):
            # Permanent misses — model not found / route gone. Never
            # transient regardless of which OpenAI-SDK exception class
            # wrapped them (APIStatusError has no dedicated subclass for
            # 410, so isinstance(e, OPENAI_ERRORS) below would otherwise
            # blanket-classify it as transient and this would never be
            # caught by the permanent-miss handling in _parse_stream).
            return False
        err_str = str(e).lower().replace(" ", "")
        transient_markers = [
            "timeout", "timedout", "apitimeout", "apiconnection",
            "429", "toomanyrequests", "ratelimit", "resourceexhausted",
            "overloaded", "503", "502", "504", "workerlocal",
            "finish_reason", "dropped", "stall", "degraded",
            "badgateway", "unavailable", "gateway", "first-token", "firsttoken",
            "capacity", "internalservererror",
        ]
        if any(m in err_str for m in transient_markers):
            return True
        if "500" in err_str and "template" not in err_str:
            return True
        if isinstance(e, OPENAI_ERRORS):
            return True
        return False

    def _parse_stream(self, payload: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
        max_total_attempts = int(self.config.get("retry_max", 2))
        max_per_provider = int(self.config.get("max_retries_per_provider", 2))
        base_delay = float(self.config.get("retry_base_delay", 1.5))
        total_attempts = 0
        empty_pool_spins = 0

        effort = str(self.config.get("thinking_effort") or "off").lower()
        payload = dict(payload)
        if effort in ("low", "medium", "high"):
            extra = dict(payload.get("extra_body") or {})
            ctk = dict(extra.get("chat_template_kwargs") or {})
            ctk["enable_thinking"] = True
            ctk["thinking_effort"] = effort
            extra["chat_template_kwargs"] = ctk
            payload["extra_body"] = extra
        else:
            extra = dict(payload.get("extra_body") or {})
            ctk = dict(extra.get("chat_template_kwargs") or {})
            ctk["enable_thinking"] = False
            extra["chat_template_kwargs"] = ctk
            payload["extra_body"] = extra

        empty_pool_waited = 0.0
        max_empty_wait = float(self.config.get("empty_pool_max_wait", 120.0))
        tools_stripped = False

        rl_until = float(getattr(self, "_rate_limit_until", 0.0) or 0.0)
        if rl_until > time.monotonic():
            wait_rl = rl_until - time.monotonic()
            UI.warn(f"Session rate-limit pause {wait_rl:.0f}s (post-429 backoff)...")
            time.sleep(min(wait_rl, 60.0))

        _last_cooldown_print = 0.0
        while total_attempts < max_total_attempts:
            provider = self.pool.next_available()
            if not provider:
                empty_pool_spins += 1
                wait = float(self.pool.shortest_wait())
                if wait <= 0:
                    wait = 2.0
                # Consolidate short cooldowns into one sleep instead of many
                # 2s slices, each of which used to print its own warning line.
                slice_w = min(wait, 30.0) if wait > 5.0 else wait
                if empty_pool_waited + slice_w > max_empty_wait and empty_pool_spins > 2:
                    alive = [
                        p.name for p in self.pool.providers
                        if not getattr(p, "permanently_disabled", False)
                    ]
                    raise RuntimeError(
                        "All providers unavailable after waiting "
                        f"{empty_pool_waited:.0f}s (cooldowns). "
                        f"Active pool: {alive or 'none'}. "
                        "Check API key / rate limits / network, or /model to switch."
                    )
                now_m = time.monotonic()
                if now_m - _last_cooldown_print >= 5.0 or empty_pool_spins <= 1:
                    UI.warn(
                        f"All providers cooling down. Waiting {slice_w:.1f}s "
                        f"(cooldown≈{wait:.0f}s, waited {empty_pool_waited:.0f}s total, "
                        f"spin #{empty_pool_spins})..."
                    )
                    _last_cooldown_print = now_m
                time.sleep(slice_w)
                empty_pool_waited += slice_w
                continue
            empty_pool_spins = 0
            empty_pool_waited = 0.0

            wt = provider.bucket.wait_time()
            if wt > 0.5:
                UI.info(f"Rate limit pacing ({wt:.1f}s)...")
                time.sleep(min(wt, 5.0))

            total_attempts += 1

            try:
                if HAS_RICH:
                    console.print(
                        f"  [neon_dim]* {UI.esc(provider.name)} attempt {total_attempts}/{max_total_attempts}...[/neon_dim]",
                        end="\r"
                    )
                else:
                    print(f"  * {provider.name} attempt {total_attempts}/{max_total_attempts}...", end="\r", flush=True)
                sys.stdout.flush()

                payload["model"] = provider.model_id
                pt = provider.model_cfg.get("temperature")
                payload["temperature"] = pt if pt is not None else self.config.get("temperature", 0.2)
                p_top_p = provider.model_cfg.get("top_p")
                if p_top_p is not None:
                    payload["top_p"] = p_top_p
                else:
                    payload.pop("top_p", None)
                payload["max_tokens"] = provider.model_cfg.get("max_tokens", 8192)
                
                p_extra = provider.model_cfg.get("extra_body")
                if not tools_stripped:
                    # Previously this branch only fired `if p_extra`, so any
                    # model without a static extra_body template in MODELS
                    # (glm-5.2, glm-5.2-fp8, minimax-m3, laguna-xs-2.1) fell
                    # into the `else: payload.pop("extra_body", None)` branch
                    # below, silently discarding the enable_thinking value the
                    # top of this function had just set from thinking_effort.
                    # That meant the "off"/"low"/"medium"/"high" config never
                    # reached those models' requests at all — they ran
                    # whatever the backend's default is (GLM-5.2 defaults to
                    # Think Max). Build chat_template_kwargs here for every
                    # provider, layering the provider's own static template
                    # on top so it's preserved, instead of choosing one or
                    # the other.
                    merged_extra = dict(p_extra) if p_extra else {}
                    ctk = dict(merged_extra.get("chat_template_kwargs") or {})
                    effort_now = str(self.config.get("thinking_effort") or "off").lower()
                    if effort_now in ("low", "medium", "high"):
                        ctk["enable_thinking"] = True
                        ctk["reasoning_effort"] = effort_now
                    else:
                        ctk["enable_thinking"] = False
                    merged_extra["chat_template_kwargs"] = ctk
                    payload["extra_body"] = merged_extra
                else:
                    payload.pop("extra_body", None)

                content, reasoning, tool_calls, finish_reason = self._consume_stream(provider, payload)

                if finish_reason is not None:
                    provider.record_success()
                    self._note_provider_success()
                    return content, reasoning, tool_calls
                else:
                    raise Exception("Stream completed without finish_reason — backend dropped")

            except Exception as e:
                err_l = str(e).lower()
                err_c = err_l.replace(" ", "")
                status_code = getattr(e, "status_code", None)
                if status_code is None:
                    resp = getattr(e, "response", None)
                    status_code = getattr(resp, "status_code", None) if resp is not None else None

                if (
                    status_code in (404, 410)
                    or "404" in err_l or "not found" in err_l or "model_not_found" in err_c
                    or "410" in err_l or "'gone'" in err_l or '"gone"' in err_l
                ):
                    if status_code == 410 or (status_code is None and ("410" in err_l or "gone" in err_l) and "404" not in err_l):
                        reason = "410 Gone"
                    else:
                        reason = "404"
                    UI.warn(f"{provider.name} permanent miss ({reason}) — removed from pool")
                    provider.record_failure(cooldown=600.0, permanent=True)
                    _pd = self.config.get("project_dir")
                    if _pd:
                        try:
                            mark_model_dead(Path(_pd).resolve(), provider.model_id)
                        except Exception:
                            pass
                    self.pool.rotate()
                    if total_attempts < max_total_attempts:
                        time.sleep(0.3)
                        continue
                    raise RuntimeError(f"All providers failed. Last error: {e}")

                if self._is_template_error(e):
                    UI.warn(
                        f"{provider.name} prompt-template/tools failure — "
                        f"disabling this model for the session ({str(e)[:120]})"
                    )
                    provider.record_failure(cooldown=600.0, permanent=True)
                    self.pool.rotate()
                    if not tools_stripped and payload.get("tools"):
                        tools_stripped = True
                        payload = dict(payload)
                        payload.pop("tools", None)
                        payload["tool_choice"] = "none"
                        UI.info("Retrying once without tools schema (template workaround)...")
                        if total_attempts < max_total_attempts:
                            time.sleep(0.5)
                            continue
                    if total_attempts < max_total_attempts:
                        time.sleep(0.5)
                        continue
                    raise RuntimeError(f"All providers failed (template). Last error: {e}")

                if "401" in err_l or "403" in err_l or "unauthorized" in err_l or "forbidden" in err_l:
                    UI.err(f"Auth failure: {e}")
                    UI.warn("NVIDIA rejected this project's API key (403/401).")
                    UI.info("Fix:  /apikey <new-nvapi-key-from-https://build.nvidia.com>")
                    UI.info("Check: key must have access to model z-ai/glm-5.2 on NVIDIA NIM.")
                    UI.info("Or:    /model minimax-m3   if that model is enabled on your key.")
                    raise

                if (
                    "resourceexhausted" in err_c
                    or "limit reached" in err_l
                    or "429" in err_l
                    or "ratelimit" in err_c
                ):
                    retry_after = None
                    for attr in ("response", "http_response"):
                        resp = getattr(e, attr, None)
                        headers = getattr(resp, "headers", None) if resp is not None else None
                        if headers:
                            ra = headers.get("retry-after") or headers.get("Retry-After")
                            if ra:
                                try:
                                    retry_after = float(ra)
                                except (TypeError, ValueError):
                                    retry_after = None
                            break
                    cooldown = retry_after if retry_after is not None else 15.0
                    UI.warn(
                        f"{provider.name} at capacity (429) — "
                        f"{'Retry-After='+str(cooldown)+'s' if retry_after is not None else 'no Retry-After header, defaulting 15s'}, "
                        f"pausing shared key budget"
                    )
                    provider.record_failure(cooldown=cooldown)
                    provider.bucket.penalize(cooldown)
                    sibling_names = self.pool.propagate_shared_cooldown(provider, cooldown)
                    if sibling_names:
                        UI.warn(
                            f"Same account key as {provider.name} — also cooling down "
                            f"{', '.join(sibling_names)} for {cooldown:.0f}s "
                            f"(avoids each one round-tripping to its own 429)."
                        )
                    backoff = max(
                        cooldown,
                        float(self.config.get("post_429_backoff", 25.0) or 25.0),
                    )
                    self._rate_limit_until = max(
                        getattr(self, "_rate_limit_until", 0.0),
                        time.monotonic() + backoff,
                    )
                    self._recent_429_count = int(getattr(self, "_recent_429_count", 0)) + 1
                elif not self._is_transient_error(e):
                    UI.err(f"Fatal API error: {e}")
                    raise
                else:
                    UI.warn(f"{provider.name} transient: {str(e)[:100]}")
                    err_l2 = str(e).lower()
                    if any(x in err_l2 for x in ("first-token", "firsttoken", "first token", "stall", "buffering")):
                        provider.record_failure(cooldown=4.0)
                    else:
                        provider.record_failure(cooldown=8.0)
                    if self.pool.should_rotate(max_per_provider):
                        self.pool.rotate()

                if total_attempts < max_total_attempts:
                    delay = min(base_delay + random.uniform(0, 1.0), 5.0)
                    UI.info(f"Retrying in {delay:.1f}s...")
                    time.sleep(delay)
                else:
                    UI.err(f"Max retries ({max_total_attempts}) exceeded.")
                    raise RuntimeError(f"All providers failed. Last error: {e}")

        raise RuntimeError("Exhausted all retry attempts")

    def _execute_tools(self, tool_calls: List[Dict[str, Any]]) -> bool:
        if not tool_calls:
            return False

        MAX_PARALLEL_TOOLS = 7
        if len(tool_calls) > MAX_PARALLEL_TOOLS:
            dropped = tool_calls[MAX_PARALLEL_TOOLS:]
            tool_calls = tool_calls[:MAX_PARALLEL_TOOLS]
            UI.warn(f"Truncating {len(dropped) + len(tool_calls)} parallel tools to {MAX_PARALLEL_TOOLS} to prevent sprawl.")
            # Previously this just sliced the list with no signal anywhere:
            # the dropped calls never even entered context (add_assistant
            # runs on the truncated list below), so the model had no way to
            # know some of its own requested actions never happened at all —
            # from its side it asked for N things, saw results for 7, and
            # had no reason to think the rest weren't simply "still coming."
            # Surface exactly which calls got dropped so it can decide
            # whether to re-request any of them next turn instead of
            # silently assuming they succeeded or will appear later.
            dropped_desc = []
            for tc in dropped:
                dname = tc.get("name") or "?"
                try:
                    dargs = json.loads(tc.get("arguments") or "{}")
                except Exception:
                    dargs = {}
                dpreview = json.dumps(dargs, ensure_ascii=False)[:80]
                dropped_desc.append(f"{dname}({dpreview})")
            self.ctx.add_user(
                f"SYSTEM: {len(dropped)} of your {len(dropped) + len(tool_calls)} tool calls this turn "
                f"were dropped (only the first {MAX_PARALLEL_TOOLS} ran) to prevent call sprawl. "
                f"None of these ran — they are NOT pending, NOT queued, and will NOT appear later: "
                f"{'; '.join(dropped_desc)}. If any of these are still needed, call them again "
                f"explicitly in a future turn; do not assume they happened."
            )

        formatted_input = [
            {
                "id": tc.get("id") or "",
                "name": tc.get("name") or "",
                "arguments": tc.get("arguments") or "{}",
            }
            for tc in tool_calls
        ]
        formatted = sanitize_tool_calls(formatted_input)
        # sanitize_tool_calls() fills a missing/empty name with the literal
        # placeholder "unknown" (so downstream formatting always has a
        # non-empty string to show), which means checking
        # f["function"]["name"] here can never be falsy and this filter
        # could never actually drop anything — a genuinely nameless tool
        # call would still be added to context as a call to a tool
        # literally named "unknown", confusing the model on later turns
        # even though execute_tool() separately handles an empty name
        # safely at call time. Filter on the pre-sanitization names
        # instead. Correlating by id doesn't work here: providers that
        # omit tool-call ids leave every real entry's id as "" too, so an
        # id-based filter drops legitimate calls right along with the
        # malformed one (worse than the bug being fixed). formatted_input
        # is built 1:1 from tool_calls immediately above with no
        # intervening filtering, and every element is a dict literal, so
        # sanitize_tool_calls's own "isinstance(tc, dict): continue" skip
        # can never fire on it — output stays positionally aligned with
        # formatted_input/tool_calls, so a straight zip is safe here.
        formatted = [
            f for f, orig in zip(formatted, formatted_input)
            if str(orig.get("name") or "").strip()
        ]
        self.ctx.add_assistant(content="", tool_calls=formatted)

        spam_blocks = 0

        for tc in tool_calls:
            name = tc.get("name", "")
            raw_args = tc.get("arguments", "{}")
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}

            preview = json.dumps(args, ensure_ascii=False)[:120]
            if len(preview) > 120:
                preview = preview[:117] + "..."
            if HAS_RICH:
                console.print(UI.tool_call(name, preview))
            else:
                print(f"* {name}({preview})")

            fp_key = self._tool_fingerprint(name, args)
            prior_fails = self._tool_fail_counts.get(fp_key, 0)
            prior_ok = self._tool_success_counts.get(fp_key, 0)
            max_ok = int(getattr(self, "max_same_tool_successes", 3) or 3)
            force_override = name == "write" and bool(args.get("force"))

            if prior_fails >= self.max_same_tool_failures and not force_override:
                spam_blocks += 1
                msg = (
                    f"[BLOCKED REPEAT] {name} with the same arguments already failed "
                    f"{prior_fails} times. Change strategy: different path, query, URL, or tool. "
                    + ("Add force=true if you deliberately mean this exact content."
                       if name == "write" else "")
                )
                UI.warn(msg)
                self._strategy_hints.append(msg)
                result = ToolResult("", error=msg, is_error=True)
            elif (bare_reason := self._suspicious_bare_args_reason(name, args)):
                msg = f"[SUSPICIOUS ARGS] {name}: {bare_reason}"
                UI.warn(msg)
                self._strategy_hints.append(msg)
                result = ToolResult("", error=msg, is_error=True)
            elif prior_ok >= max_ok and name in ("write", "edit"):
                spam_blocks += 1
                gate_hint = self._gate_missing_hint() if self.autopilot else ""
                msg = (
                    f"[BLOCKED SPAM] {name} succeeded {prior_ok}x with the same fingerprint "
                    f"but the phase gate is still open. Do something DIFFERENT — "
                    + (gate_hint if gate_hint else
                       "new path, fix source, run a real test command, or stop repeating the same write.")
                )
                UI.warn(msg)
                self._strategy_hints.append(msg)
                result = ToolResult("", error=msg, is_error=True)
            else:
                # Interactive confirm gate for destructive git operations.
                # Reuses the same detection RunTool._git_destructive_reason
                # already applies as a hard block — here, in an interactive
                # (non-autopilot) session, we ask the human instead of
                # refusing outright, since a person is actually present to
                # answer. Autopilot has no one to ask, so it keeps the
                # existing hard-block behavior inside RunTool unchanged —
                # this gate is deliberately skipped there rather than ever
                # blocking on input() with nobody watching.
                if (
                    not self.autopilot
                    and name == "run"
                    and str(args.get("program", "")).strip().lower() == "git"
                ):
                    reason = RunTool._git_destructive_reason(args.get("args") or [])
                    if reason:
                        action = f"run git {' '.join(str(a) for a in (args.get('args') or []))}"
                        prompt = UI.permission_prompt(action, reason)
                        if HAS_RICH:
                            console.print(prompt)
                        else:
                            print(prompt)
                        try:
                            ans = input("  Allow? [y/N]: ").strip().lower()
                        except (KeyboardInterrupt, EOFError):
                            ans = "n"
                        if ans not in ("y", "yes"):
                            msg = f"User declined: {reason}"
                            UI.warn(msg)
                            result = ToolResult("", error=msg, is_error=True)
                        else:
                            result = execute_tool(self.tools, name, {**args, "_confirmed_destructive": True})
                    else:
                        result = execute_tool(self.tools, name, args)
                else:
                    result = execute_tool(self.tools, name, args)
                if name == "validate_project":
                    # Validation reports are useful whether the project is
                    # ready or blocked; preserve both before generic tool
                    # failure handling potentially reduces the result to a
                    # short error string.
                    self._record_validation_result(result)
                if result.is_error:
                    self._tool_fail_counts[fp_key] = prior_fails + 1
                    self._fp_last_error[fp_key] = str(result.error or "")[:300]
                    if self._tool_fail_counts[fp_key] >= 2:
                        hint = (
                            f"Tool {name} failed {self._tool_fail_counts[fp_key]}x with the SAME "
                            f"arguments. Actual error: {self._fp_last_error[fp_key]!r} "
                            f"Do NOT retry with a cosmetically different filename/argument — that "
                            f"still counts as the same failure. Before calling {name} again, your "
                            f"next message must state, in plain text, which different tool or "
                            f"genuinely different approach (edit vs write, append=true, force=true, "
                            f"reading the file first, etc.) you are switching to and why."
                        )
                        self._strategy_hints.append(hint)
                        self._fp_pending_reflection.add(fp_key)
                        UI.warn(hint)
                else:
                    self._tool_fail_counts[fp_key] = 0
                    self._tool_success_counts[fp_key] = prior_ok + 1
                    self._phase_tool_success_counts[name] = self._phase_tool_success_counts.get(name, 0) + 1
                    if name == "web_search":
                        self._research_done = True
                        q = str(args.get("query", ""))[:120]
                        self._update_working_memory(f"web_search: {q}")
                        self.project_state.setdefault("research_notes", []).append(f"search:{q}")
                    elif name == "browse_page":
                        self._research_done = True
                        u = str(args.get("url", ""))[:160]
                        self._update_working_memory(f"browsed: {u}")
                        self.project_state.setdefault("research_notes", []).append(f"browse:{u}")
                    elif name in ("write", "edit"):
                        self._validation_ready = False
                        self.project_state.pop("validation", None)
                        rel = str(args.get("path") or "").replace("\\", "/").lstrip("./")
                        self._update_working_memory(f"{name}: {rel}")
                        # Chars actually written on a successful call — used by
                        # gates that need to distinguish "wrote real content"
                        # from "made N calls" (see DESIGN/DEPLOYMENT gates).
                        # Credited per-path as a MAX, not summed across
                        # repeats: rewriting the same file with the same (or
                        # smaller) content adds no new credit, so a model
                        # can't satisfy a min_chars gate by writing the same
                        # placeholder stub to one file multiple times.
                        content_len = len(str(args.get("content") or "")) if name == "write" else len(str(args.get("new_text") or ""))
                        path_key = Tool._sanitize_rel_path(rel) or rel
                        prior_credit = self._phase_chars_by_path.get(path_key, 0)
                        if content_len > prior_credit:
                            self._phase_chars_by_path[path_key] = content_len
                        prefixes = self.config.get("implementation_path_prefixes") or [
                            "src/", "app/", "backend/", "frontend/", "lib/", "tests/"
                        ]
                        low = rel.lower()
                        if any(low.startswith(p.lower()) or f"/{p.lower()}" in f"/{low}" for p in prefixes):
                            is_valid_py = True
                            if rel.endswith(".py"):
                                try:
                                    ast.parse((self.project_dir / rel).read_text(encoding="utf-8", errors="replace"))
                                except Exception:
                                    is_valid_py = False
                            if is_valid_py:
                                # Count toward src_write once per unique path, not
                                # once per call — otherwise editing the same file
                                # N times satisfies a min_src_writes gate exactly
                                # like N genuinely different files would, letting
                                # the gate pass without real implementation breadth.
                                if rel not in self._src_write_paths:
                                    self._src_write_paths.append(rel)
                                    self._phase_tool_counts["src_write"] = self._phase_tool_counts.get("src_write", 0) + 1
                    elif name == "todo" and args.get("action") == "add":
                        self._update_working_memory(f"todo: {str(args.get('content',''))[:120]}", section="Next action")
                    elif name == "run":
                        digest = getattr(result, "test_digest", "") or ""
                        if digest:
                            self._last_test_digest = digest
                            if result.is_error:
                                self._update_working_memory("test failure — see LAST TEST DIGEST", section="Blockers")
                                # Inject an immediate self-correction directive into the
                                # conversation so the next LLM turn acts on the failure
                                # rather than continuing to write unrelated code.
                                # Extract which test files failed from the digest so the
                                # agent knows exactly where to look.
                                failed_lines = [
                                    l for l in digest.splitlines()
                                    if "FAILED" in l or "ERROR" in l or "FileNotFoundError" in l
                                    or "ImportError" in l or "AssertionError" in l
                                ]
                                failed_summary = "\n".join(failed_lines[:20]) if failed_lines else digest[:600]
                                self.ctx.add_user(
                                    "SELF-CORRECTION REQUIRED: The test run just failed. "
                                    "You MUST now act as a self-correcting autonomous engine:\n"
                                    "STEP 1 — Read the failing test file(s) shown in the stacktrace below using the read tool.\n"
                                    "STEP 2 — Read the stacktrace carefully and identify the EXACT root cause "
                                    "(wrong path, missing file, wrong import, wrong assertion, missing __init__.py, etc.).\n"
                                    "STEP 3 — Fix the root cause: rewrite the offending source file OR the test file "
                                    "if the test has a bug (e.g. wrong relative path, wrong assumption). "
                                    "Do NOT re-run without fixing first. Do NOT write unrelated files.\n"
                                    "STEP 4 — Re-run the exact same test command to confirm the fix.\n"
                                    "Do NOT ask the user. Do NOT write PLAN.md or ARCHITECTURE.md. "
                                    "Call ONE read tool NOW on the failing test file.\n\n"
                                    f"FAILING TESTS/ERRORS:\n{failed_summary}"
                                )

                            else:
                                prog = str(args.get("program") or "")
                                run_args = args.get("args") or []
                                cmd = (prog + " " + " ".join(str(a) for a in run_args))[:120]
                                self.project_state["test_command"] = cmd
                                self._update_working_memory(f"tests ok: {cmd}", section="Decisions")

            self._recent_tool_keys.append(fp_key)
            out_preview = result.text()[:800] if not result.is_error else result.text()[:500]
            if HAS_RICH:
                console.print(UI.tool_result(name, out_preview, result.is_error))
            else:
                print(UI.tool_result(name, out_preview, result.is_error))

            self.current_turn_tool_calls = True
            self._phase_tool_counts[name] = self._phase_tool_counts.get(name, 0) + 1
            if name == "todo" and args.get("action") == "add" and not result.is_error:
                self._phase_tool_counts["todo_add"] = self._phase_tool_counts.get("todo_add", 0) + 1
            if name == "run":
                prog = str(args.get("program") or "")
                run_args = args.get("args") or []
                is_test = RunTool._is_test_invocation(prog, run_args)
                if is_test and result.is_error:
                    self._tdd_saw_red = True
                    if getattr(result, "test_digest", ""):
                        self._last_test_digest = result.test_digest
                if is_test and not result.is_error:
                    self._phase_bash_ok = True
                    if self._tdd_saw_red:
                        self._tdd_saw_green = True
                        self._update_working_memory("TDD green after red", section="Decisions")

            ctx_tokens = self.ctx.estimate_tokens()
            max_tool_len = max(2000, (self.model_cfg["ctx_window"] - ctx_tokens) * 4)
            self.ctx.add_tool_result(tc["id"], name, result.text()[:max_tool_len])

        if spam_blocks > 0 and spam_blocks == len(tool_calls):
            warning = (
                "SYSTEM CRITICAL: ALL your tool calls this turn were rejected as repetitive spam. "
                "You are stuck in an infinite loop doing the exact same failing action. "
                "STOP. Review the context, read the actual files, and try a completely DIFFERENT approach or path."
            )
            self.ctx.add_user(warning)
            UI.warn("Total spam turn detected — injected hard circuit-breaker prompt.")

        # Immediate gate short-circuit: if this turn's tool results already
        # satisfy the current phase's gate (most commonly _phase_bash_ok
        # flipping True on a clean test run), advance right now instead of
        # waiting for the model to notice on some later round. Without this,
        # a model that keeps calling Read/Todo/etc. after tests already
        # passed can spin for many rounds narrating "let me verify" before
        # the existing end-of-turn check (further down in run_turn) happens
        # to coincide with an advance — and if a later `run` call for the
        # SAME test command gets rejected by the write/edit/run spam guard,
        # that narration could continue indefinitely. This check is cheap
        # (pure state inspection, no side effects) and safe to run every
        # turn regardless of whether this turn's own tool calls succeeded.
        if self.autopilot and self.sdlc_phase_idx < len(SDLC_PHASES):
            if self._phase_requirements_met():
                phase_name = SDLC_PHASES[self.sdlc_phase_idx]
                UI.info(f"Gate satisfied mid-turn ({phase_name.upper()}) — advancing immediately.")
                if not self._advance_sdlc():
                    UI.separator("GOAL ACHIEVED")
                    UI.ok("All SDLC phases complete!")
                    self._stop_autopilot()
                else:
                    self.ctx.add_user(self._autopilot_prompt())

        self._sync_project_state()
        return True

    def _asks_user(self, content: str) -> bool:
        lower = (content or "").lower()
        markers = [
            "shall i", "should i", "would you like", "do you want me",
            "ready to proceed", "may i proceed", "can i proceed",
            "let me know", "please confirm", "awaiting your",
            "your approval", "want me to continue", "shall we",
            "ready for the next", "proceed to the next phase",
            "move to the next phase", "ok to continue",
            "start phase", "begin phase", "shall i start",
            "ready for **", "gate satisfied. ready",
            "proceeding to **implementation**?",
        ]
        if any(m in lower for m in markers):
            return True
        if "?" in content and any(
            w in lower for w in ("you", "shall", "should", "ready", "proceed", "continue")
        ):
            return True
        return False

    def _should_nudge(self, content: str) -> bool:
        if not self.config.get("auto_nudge", True):
            return False
        if self.nudges >= self.config.get("max_nudges", 2):
            return False
        if self.autopilot and self._asks_user(content):
            return True
        if len(content) > 800 and not self.autopilot:
            return False
        lower = content.lower()
        phrases = [
            "let me", "i will", "i'll", "i need to", "i'm going to",
            "allow me to", "i should", "first, i", "next, i",
            "i can", "i shall", "i intend to", "i am going to",
            "going to search", "going to read", "going to check",
            "will now", "now i will", "planning phase is complete",
            "ready to proceed", "gate requirements are satisfied",
        ]
        return any(p in lower for p in phrases)

    def _test_status_hint_bits(self, phase: str) -> List[str]:
        """Shared by the bash_success and verification_criteria_complete
        hint branches so both report test/coverage status identically
        instead of the wording drifting apart between the two phases."""
        bits: List[str] = []
        if not self._phase_bash_ok:
            bits.append("test_invoked: NO — call a test runner (pytest/npm test/cargo test/go test)")
            return bits
        stats = parse_test_digest(self._last_test_digest)
        if not stats["parsed"]:
            bits.append(
                "test_invoked: yes, but digest format unrecognized — cannot confirm pass count; "
                "re-run with a standard test runner that prints a summary line"
            )
            return bits
        bits.append(
            f"tests: passed={stats['passed']} failed={stats['failed']} errors={stats['errors']}"
        )
        if stats["failed"] > 0 or stats["errors"] > 0:
            bits.append("Fix the failing/erroring tests shown in LAST TEST DIGEST, then re-run.")
        elif stats["passed"] < int(self.config.get("min_tests_passed", 1) or 1):
            bits.append("Write and run more real tests — a single trivial test is not enough coverage.")
        min_cov = float(self.config.get("min_coverage_pct", 0) or 0)
        if min_cov > 0 and phase == "verification":
            if stats["coverage_pct"] is None:
                bits.append(
                    f"coverage: NOT MEASURED (need ≥{min_cov}%) — re-run with coverage enabled, "
                    "e.g. `pytest --cov`, `go test -cover`, `npx jest --coverage`"
                )
            else:
                bits.append(
                    f"coverage: {stats['coverage_pct']}% "
                    f"({'ok' if stats['coverage_pct'] >= min_cov else f'MISSING — need ≥{min_cov}%'})"
                )
        return bits

    @property
    def _phase_chars_written(self) -> int:
        """Total content-written credit for the current phase, summed
        per-path using each path's BEST (max) content length seen so far —
        not a running total across every write call. Without this, writing
        the same tiny placeholder to the same file 3x could sum to a large
        enough total to satisfy a min_chars gate on repeated junk alone
        (e.g. three 65-char stub writes to tests/test_ac1_auth.py summing
        to 195, plus one 136-char real attempt, crossing a 300-char
        threshold without ever producing a real file). Keying on path and
        taking the max means only genuinely growing content counts, and
        rewriting the same stub adds nothing."""
        return sum(self._phase_chars_by_path.values())

    @_phase_chars_written.setter
    def _phase_chars_written(self, value: int) -> None:
        # Only ever called with 0 (phase/session resets) — clears all
        # per-path credit. Any nonzero external set is ignored on purpose;
        # real writes must go through _phase_chars_by_path so per-path
        # max-tracking can't be bypassed by direct assignment.
        if not value:
            self._phase_chars_by_path.clear()

    def _gate_missing_hint(self) -> str:
        if not self.autopilot or self.sdlc_phase_idx >= len(SDLC_PHASES):
            return ""
        phase = SDLC_PHASES[self.sdlc_phase_idx]
        reqs = SDLC_PHASE_REQUIREMENTS.get(phase) or {}
        kind = reqs.get("kind")
        bits = [f"Phase={phase}. Gate NOT met. Do NOT ask the user."]
        if kind == "plan_complete":
            art = reqs.get("artifact") or "PLAN.md"
            min_chars = int(reqs.get("min_chars", 400))
            ok, n = self._artifact_ok(art, min_chars)
            if ok:
                bits.append(f"{art}: ok ({n} chars)")
            else:
                bits.append(f"{art}: TOO SHORT — {n}/{min_chars} chars required, write MORE content (not a rewrite of the same text)")
            bits.append(f"todos_added={self._phase_tool_counts.get('todo_add', 0)} need ≥{reqs.get('min_todos', 5)}")
            text = self._read_artifact_snippet(art, max_chars=20000)
            headers = tuple(h.lower() for h in (reqs.get("require_sections") or PLAN_REQUIRED_HEADERS))
            for h in headers:
                bits.append(f"section '{h}': {'ok' if plan_has_sections(text, (h,)) else 'MISSING — edit PLAN.md to add heading'}")
            research = (
                self._research_done
                or self._phase_tool_success_counts.get("web_search", 0) > 0
                or self._phase_tool_success_counts.get("browse_page", 0) > 0
            )
            bits.append(f"research: {'ok' if research else 'need web_search/browse_page'}")
        elif kind == "architecture_complete":
            art = reqs.get("artifact") or "ARCHITECTURE.md"
            min_chars = int(reqs.get("min_chars", 300))
            ok, n = self._artifact_ok(art, min_chars)
            if ok:
                bits.append(f"{art}: ok ({n} chars)")
            else:
                bits.append(f"{art}: TOO SHORT — {n}/{min_chars} chars required, write MORE content (not a rewrite of the same text)")
            arch_text = self._read_artifact_snippet(art, max_chars=50000)
            headers = tuple(h.lower() for h in (reqs.get("require_sections") or ARCH_REQUIRED_HEADERS))
            sections_ok = plan_has_sections(arch_text, headers) if arch_text else False
            if not sections_ok:
                bits.append(f"missing required section header(s): {', '.join(headers)} — add as '## <header>' headings")
            bits.append(f"write/edit (successful)={sum(self._phase_tool_success_counts.get(x,0) for x in ('write','edit'))}")
        elif kind == "bash_success":
            # This phase's whole job is confirming correctness, so the hint
            # needs to say more than a generic tool-count dump — report
            # whether a recognized test command has even run, and what the
            # parsed digest actually shows (not just the boolean).
            bits.extend(self._test_status_hint_bits(phase))
        elif kind == "verification_criteria_complete":
            bits.extend(self._test_status_hint_bits(phase))
            criteria = self.project_state.get("acceptance_criteria") or []
            artifact = reqs.get("artifact") or "VERIFICATION.md"
            if not criteria:
                bits.append(
                    "acceptance_criteria: none captured from planning — gate only requires tests to pass"
                )
            else:
                ok, _n = self._artifact_ok(artifact, min_chars=1)
                text = self._read_artifact_snippet(artifact, max_chars=50000) if ok else ""
                rows = parse_verification_rows(text) if text else []
                covered, missing = match_criteria_to_verification(criteria, rows)
                bits.append(f"criteria_linked: {len(covered)}/{len(criteria)} in {artifact}")
                if missing:
                    bits.append(
                        f"MISSING test link for: {'; '.join(missing[:3])}"
                        + (f" (+{len(missing)-3} more)" if len(missing) > 3 else "")
                    )
                    bits.append(
                        f"For each, add a row to {artifact}: "
                        "`- [x] <criterion text> — test: <path/to/test.py::test_name>`, "
                        "naming a REAL test you have actually run and confirmed passes."
                    )
        elif kind == "tool_calls":
            tools = reqs.get("any_tool", [])
            total = sum(self._phase_tool_success_counts.get(x, 0) for x in tools)
            min_calls = int(reqs.get("min_calls", 1))
            bits.append(
                f"successful {'/'.join(tools)} calls: {total}/{min_calls} required"
                + ("" if total >= min_calls else " — call one of these tools with real changes")
            )
            min_chars = int(reqs.get("min_chars_written", 0) or 0)
            if min_chars:
                bits.append(
                    f"chars written this phase: {self._phase_chars_written}/{min_chars} required"
                    + ("" if self._phase_chars_written >= min_chars else " — write MORE actual content, not just successful calls")
                )
        elif kind == "implementation_complete":
            total = sum(self._phase_tool_success_counts.get(x, 0) for x in reqs.get("any_tool", ["write", "edit"]))
            src_n = self._phase_tool_counts.get("src_write", 0)
            min_calls = int(reqs.get("min_calls", 8))
            min_src = int(reqs.get("min_src_writes", self.config.get("min_implementation_src_writes", 4)))
            bits.append(f"write/edit calls: {total}/{min_calls} required")
            bits.append(f"source writes (src/app/backend/frontend/lib/tests prefix): {src_n}/{min_src} required")
            if src_n >= 1 and not self._phase_bash_ok:
                bits.append(
                    "If the code is already correct, run the real test command now — "
                    "a passing digest (0 failed, 0 errors) can satisfy this gate without "
                    "hitting the raw call quota. Do NOT re-write files that are already correct."
                )
            elif src_n >= 1 and self._phase_bash_ok:
                bits.append(
                    "Tests already ran — if the last run showed 0 failed/0 errors, the gate "
                    "should already be satisfied via the already-complete shortcut; re-run the "
                    "test command once more to confirm rather than re-writing unchanged files."
                )
            else:
                bits.append(
                    "No source write recorded yet this phase — write a REAL source file under "
                    "src/ OR app/ OR backend/ OR frontend/ OR lib/ OR tests/ before anything else."
                )
        elif kind == "deployment_complete":
            art = reqs.get("artifact") or "docker-compose.yml"
            min_chars = int(reqs.get("min_chars", 500))
            ok, n = self._artifact_ok(art, min_chars)
            if ok:
                bits.append(f"{art}: ok ({n} chars)")
            else:
                bits.append(f"{art}: MISSING OR TOO SHORT — {n}/{min_chars} chars required")
            text = self._read_artifact_snippet(art, max_chars=50000) if ok else ""
            headers = tuple(h.lower() for h in (reqs.get("require_sections") or ["frontend", "backend", "database", "services"]))
            for h in headers:
                found = h.lower() in text.lower() if text else False
                bits.append(f"service '{h}': {'ok' if found else 'MISSING — add to docker-compose.yml'}")
            # Also check for Dockerfile
            dockerfile_ok, _ = self._artifact_ok("Dockerfile", 100)
            bits.append(f"Dockerfile: {'ok' if dockerfile_ok else 'MISSING'}")
        elif kind == "product_validation":
            bits.extend(self._test_status_hint_bits(phase))
            criteria = self.project_state.get("acceptance_criteria") or []
            artifact = reqs.get("artifact") or "VERIFICATION.md"
            if not criteria:
                bits.append("acceptance_criteria: none captured from planning")
            else:
                ok, _n = self._artifact_ok(artifact, min_chars=1)
                text = self._read_artifact_snippet(artifact, max_chars=50000) if ok else ""
                rows = parse_verification_rows(text) if text else []
                covered, missing = match_criteria_to_verification(criteria, rows)
                bits.append(f"criteria_linked: {len(covered)}/{len(criteria)} in {artifact}")
                if missing:
                    bits.append(f"MISSING test link for: {'; '.join(missing[:3])}")
            # Product validation checks
            product_checks = reqs.get("product_checks", [])
            for check in product_checks:
                if check == "docker-compose.yml":
                    ok, n = self._artifact_ok("docker-compose.yml", 500)
                    bits.append(f"docker-compose.yml: {'ok' if ok else f'MISSING ({n} chars)'}")
                elif check == "Dockerfile":
                    ok, _ = self._artifact_ok("Dockerfile", 100)
                    bits.append(f"Dockerfile: {'ok' if ok else 'MISSING'}")
                elif check == "frontend":
                    # Check for frontend directory with package.json
                    ok = Path(self.project_dir).joinpath("frontend", "package.json").exists()
                    bits.append(f"frontend: {'ok' if ok else 'MISSING'}")
                elif check == "backend":
                    ok = Path(self.project_dir).joinpath("backend", "main.py").exists() or Path(self.project_dir).joinpath("backend", "app", "main.py").exists()
                    bits.append(f"backend: {'ok' if ok else 'MISSING'}")
                elif check == "live_preview":
                    # Check for preview infrastructure
                    ok = Path(self.project_dir).joinpath("src", "core", "preview", "dev_server.py").exists()
                    bits.append(f"live_preview: {'ok' if ok else 'MISSING'}")
                elif check == "project_persistence":
                    ok = Path(self.project_dir).joinpath("src", "core", "projects").exists() or Path(self.project_dir).joinpath("backend", "app", "models").exists()
                    bits.append(f"project_persistence: {'ok' if ok else 'MISSING'}")
        else:
            bits.append(f"counts={dict(self._phase_tool_counts)}")
        if self._phase_tool_counts.get("todo_add", 0) < 5 and phase == "planning":
            bits.append(
                'TODO EXAMPLE (copy structure): todo action=add content="Research oiioii.ai feature list"'
            )
        if phase == "implementation" and self._phase_tool_counts.get("src_write", 0) < 1:
            bits.append(
                "IMPLEMENT: write a REAL source file under src/ OR app/ OR backend/ OR frontend/ OR lib/ OR tests/. "
                "Do NOT edit PLAN.md. Example path=backend/nim_client.py with full file content."
            )
        bits.append("Call ONE tool now to fix the missing gate item.")
        return " | ".join(bits)

    def run_turn(self, user_input: str):
        if user_input:
            if user_input.lower() == "/stop":
                self._stop_autopilot()
                return
            if user_input.lower() == "/status":
                self._autopilot_status()
                return
            if not self.autopilot and PERSONAS[self.persona_key]["auto_switch"]:
                detected = self._detect_persona(user_input)
                if detected and detected != self.persona_key:
                    self._switch_persona(detected, reason="auto")
            self.ctx.add_user(user_input)
            if HAS_RICH:
                console.print(UI.user_message(user_input))
            else:
                print(f">>> {user_input}")

        _max_rounds = int(self.config.get("max_rounds", 0) or 0)
        _max_ap = int(self.config.get("max_autopilot_rounds", self.max_autopilot_rounds) or 0)
        while True:
            if not self.autopilot and _max_rounds > 0 and self.round >= _max_rounds:
                UI.warn(f"Interactive max_rounds ({_max_rounds}) reached. Stopping.")
                break
            if self.autopilot and _max_ap > 0 and self.autopilot_rounds >= _max_ap:
                UI.warn(
                    f"Autopilot advisory limit ({_max_ap}) reached — stopping without force-advancing. "
                    "Raise max_autopilot_rounds or /autopilot to resume."
                )
                self._stop_autopilot()
                break

            self.round += 1
            self.current_turn_tool_calls = False
            if self.autopilot:
                self.autopilot_rounds += 1

            if self.round % max(1, self._persist_every) == 0:
                self.persist()

            if self.ctx.should_compact():
                before = self.ctx.estimate_tokens()
                if self.ctx.compact(keep_recent=8):
                    after = self.ctx.estimate_tokens()
                    UI.info(f"Context compacted: ~{before:,} → ~{after:,} tokens")
                    self._rebuild_system()

            tokens = self.ctx.estimate_tokens()
            phase_tag = ""
            if self.autopilot and self.sdlc_phase_idx < len(SDLC_PHASES):
                phase_tag = f" | {SDLC_PHASES[self.sdlc_phase_idx].upper()}"
            if HAS_RICH:
                console.print(UI.turn_indicator(self.round, tokens, phase_tag))
            else:
                UI.separator(f"TURN {self.round} | ~{tokens} tokens{phase_tag}")

            payload = self._build_payload()

            try:
                content, reasoning, tool_calls = self._parse_stream(payload)
            except Exception as e:
                UI.err(f"Turn failed: {e}")
                if self.autopilot:
                    self.persist(force=True)
                    self._consecutive_turn_failures = getattr(self, "_consecutive_turn_failures", 0) + 1
                    n_fail = self._consecutive_turn_failures
                    err_l = str(e).lower()
                    wait = 25.0 if any(x in err_l for x in ("timeout", "capacity", "429", "resource")) else 12.0
                    # Escalate backoff once it's clear this isn't a one-off blip,
                    # instead of hammering the pool every 12-25s indefinitely
                    # (the turns-006-009 style cascade in the session log).
                    if n_fail >= 3:
                        wait = min(wait * n_fail, 180.0)
                    phase = SDLC_PHASES[min(self.sdlc_phase_idx, len(SDLC_PHASES) - 1)].upper()
                    if n_fail == 3:
                        UI.warn(
                            f"Sustained provider outage: {n_fail} turns in a row have failed "
                            f"(phase={phase}). Backing off to {wait:.0f}s between attempts. "
                            "/stop to halt, or check API key / network / provider status."
                        )
                    else:
                        UI.warn(
                            f"Provider hiccup — sleeping {wait:.0f}s then continuing "
                            f"(phase={phase}, round={self.autopilot_rounds}, "
                            f"consecutive_failures={n_fail}). /stop to halt."
                        )
                    if self.autopilot:
                        self.ctx.add_user(
                            "SYSTEM: Provider stalled. Do NOT rewrite huge DESIGN.md/SPEC.md. "
                            "Call write on ONE small source file (<200 lines) under src/ or backend/ or frontend/. "
                            "path required. No questions."
                        )
                    time.sleep(wait)
                    continue
                break

            self._consecutive_turn_failures = 0
            self.meter.record(tokens, len(content) // 4 + len(reasoning) // 4)

            malformed_tool_syntax = bool(
                re.search(r"</(tool_call|arg_value|function|parameter|invoke)>", content)
                or re.search(r'\[\{"name":\s*"\w+",\s*"parameters":', content)
                or re.search(r"\]<\]\w+\[>\[", content)
            )
            if not tool_calls and (
                re.search(r"<(tool_call|function|parameter)", content) or malformed_tool_syntax
            ):
                self._malformed_tool_streak += 1
                if self._malformed_tool_streak >= 3:
                    UI.warn(
                        f"Malformed tool-call syntax repeated {self._malformed_tool_streak}x "
                        "(likely a serving-side parser mismatch for this model) — "
                        "forcing a model switch instead of re-prompting the same way."
                    )
                    self._malformed_tool_streak = 0
                    self.ctx.add_assistant(content)
                    self.ctx.add_user(
                        "SYSTEM: Your last several responses emitted broken pseudo-tool-call "
                        "syntax (stray </tool_call>, </arg_value>, or a JSON array written as "
                        "prose) instead of a real function call. Do not repeat that pattern. "
                        "Call the tool using the native function-calling API only — no text "
                        "before or after describing the call."
                    )
                    try:
                        stuck_provider = self.pool.current()
                        stuck_provider.record_failure(cooldown=120.0)
                        self.pool.rotate()
                        UI.warn(
                            f"Rotated off {stuck_provider.name} "
                            f"({stuck_provider.model_id}) — cooling down 120s after "
                            "repeated malformed tool-call output."
                        )
                    except Exception:
                        pass
                    time.sleep(0.5)
                    continue
                UI.warn("Detected XML/malformed tool-call hallucination. Correcting...")
                self.ctx.add_assistant(content)
                self.ctx.add_user(
                    "SYSTEM: You output raw XML or malformed pseudo-JSON for a tool call. This is forbidden. "
                    "Use the native JSON function calling API. Re-issue correctly."
                )
                time.sleep(0.5)
                continue
            self.nudges = 0
            if tool_calls:
                self._execute_tools(tool_calls)
                self._consecutive_no_tool_turns = 0
                tool_names = {tc.get("name", "") for tc in tool_calls}
                is_degenerate_write = False
                if tool_names == {"write"} and len(tool_calls) == 1:
                    tc = tool_calls[0]
                    args = tc.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                    write_len = len(str(args.get("content", "")))
                    stated_intent = content.lower()
                    # A tiny write (<50 chars) to a source/test file, in the
                    # same turn where the model's own prose says it's about
                    # to write "the complete"/"full" file, is a stub — the
                    # same stalled pattern as malformed tool syntax, just
                    # wrapped in a call that technically succeeds. Treat it
                    # as a continuation of the streak, not a reset, so it
                    # doesn't mask the loop from the rotation logic below.
                    if write_len < 50 and (
                        "complete" in stated_intent or "full test" in stated_intent
                        or "full implementation" in stated_intent
                    ):
                        is_degenerate_write = True
                if not is_degenerate_write:
                    # Reset only on a genuinely healthy turn. A prior version
                    # reset this streak unconditionally (before this
                    # degenerate-write check even ran), which wiped out any
                    # malformed-tool-call streak accumulated on earlier turns
                    # the instant a degenerate-write turn followed it — so a
                    # mixed sequence (e.g. 2 malformed-syntax turns then 1
                    # degenerate write) could never reach the streak-of-3
                    # rotation threshold, directly contradicting the
                    # "continuation, not a reset" intent below.
                    self._malformed_tool_streak = 0
            else:
                self._malformed_tool_streak = 0

            if tool_calls:
                if is_degenerate_write:
                    self._malformed_tool_streak += 1
                    UI.warn(
                        f"Tiny stub write ({write_len} chars) after saying "
                        f"'complete/full' file — treating as stalled "
                        f"(streak={self._malformed_tool_streak})."
                    )
                    if self._malformed_tool_streak >= 3:
                        try:
                            stuck_provider = self.pool.current()
                            stuck_provider.record_failure(cooldown=120.0)
                            self.pool.rotate()
                            UI.warn(
                                f"Rotated off {stuck_provider.name} "
                                f"({stuck_provider.model_id}) — cooling down 120s "
                                "after repeated stub writes."
                            )
                        except Exception:
                            pass
                        self._malformed_tool_streak = 0
                if tool_names & {"todo", "write", "edit"}:
                    self.persist(force=True)
                delay = self._effective_inter_turn_delay()
                time.sleep(max(0.1, delay))
                if self.autopilot:
                    self._phase_rounds += 1
                    phase_name = SDLC_PHASES[min(self.sdlc_phase_idx, len(SDLC_PHASES) - 1)]
                    if self._phase_requirements_met():
                        if not self._advance_sdlc():
                            UI.separator("GOAL ACHIEVED")
                            UI.ok("All SDLC phases complete!")
                            self._stop_autopilot()
                            break
                    else:
                        max_pr = int(self.config.get("max_phase_rounds", 150) or 0)
                        if (
                            max_pr > 0
                            and self._phase_rounds >= max_pr
                            and phase_name not in ("testing", "verification")
                        ):
                            # Previously this force-advanced the phase
                            # ("soft-advancing") even though its gate was
                            # never met — the same accidental-leniency
                            # pattern as the testing circuit-breaker and the
                            # plan/architecture soft-pass blocks. Stop
                            # autopilot and record a blocker instead of
                            # silently marking the phase complete; a human
                            # decides whether to raise max_phase_rounds,
                            # intervene manually, or resume.
                            UI.warn(
                                f"Phase circuit-breaker: {phase_name.upper()} hit {self._phase_rounds} "
                                f"rounds without gate. Stopping autopilot — phase is NOT being marked complete."
                            )
                            try:
                                self.project_state.setdefault("blockers", []).append(
                                    f"{phase_name}: gate not met after {self._phase_rounds} rounds — autopilot halted"
                                )
                                self.project_state["blockers"] = self.project_state["blockers"][-10:]
                                self._update_working_memory(
                                    f"{phase_name} STALLED: gate not met after {self._phase_rounds} rounds. "
                                    "Autopilot halted; phase not marked complete.",
                                    section="Blockers",
                                )
                            except Exception:
                                pass
                            self._stop_autopilot()
                            break
                        elif self._phase_rounds % 5 == 0:
                            hint = self._gate_missing_hint()
                            UI.info(
                                f"Still in {phase_name.upper()} (turn {self._phase_rounds}). "
                                f"{hint}"
                            )
                            self.ctx.add_user(
                                "AUTOPILOT: Gate still open. " + hint + " Call ONE tool now. Do not ask the user."
                            )
                    continue
                continue

            self._consecutive_no_tool_turns += 1
            self.ctx.add_assistant(content)

            if self.autopilot and (
                self._phase_requirements_met() or self._should_advance_phase(content)
            ):
                if not self._advance_sdlc():
                    UI.separator("GOAL ACHIEVED")
                    UI.ok("All SDLC phases complete!")
                    self._stop_autopilot()
                    break
                self.ctx.add_user(self._autopilot_prompt())
                self.nudges = 0
                time.sleep(max(0.1, self._effective_inter_turn_delay()))
                continue

            if self.autopilot and self._asks_user(content):
                UI.warn("Autopilot blocked a user-facing question — forcing tool action.")
                hint = self._gate_missing_hint()
                self.ctx.add_user(
                    "SYSTEM OVERRIDE (AUTOPILOT): Do NOT ask the user. Do NOT wait for confirmation. "
                    "Never say Shall I / Ready / Would you like. "
                    f"{hint} "
                    "Call exactly ONE tool now."
                )
                self.nudges = 0
                time.sleep(0.3)
                continue

            if self._should_nudge(content):
                self.nudges += 1
                if self.nudges >= self.config.get("max_nudges", 2) and self.autopilot:
                    UI.warn("Nudge cap — hard tool command.")
                    hint = self._gate_missing_hint()
                    self.ctx.add_user(
                        "SYSTEM OVERRIDE: Do not explain. Do not ask the user. "
                        f"Call exactly ONE tool now. {hint}"
                    )
                    self.nudges = 0
                else:
                    UI.warn("Paused without tool — nudging.")
                    msg = "Call the tool you need NOW. No preamble. One tool call only."
                    if self.autopilot:
                        msg += " " + self._gate_missing_hint()
                    self.ctx.add_user(msg)
                time.sleep(0.5)
                continue

            self.nudges = 0

            if self.autopilot:
                self.ctx.add_user(self._autopilot_prompt() + "\n" + self._gate_missing_hint())
                delay = self._effective_inter_turn_delay()
                time.sleep(max(0.1, delay))
                continue

            break

    def _artifact_ok(self, relative_path: str, min_chars: int = 200):
        try:
            fp = (self.project_dir / relative_path).resolve()
            fp.relative_to(self.project_dir.resolve())
            if not fp.is_file():
                return False, 0
            try:
                n = len(fp.read_text(encoding="utf-8", errors="replace").strip())
            except Exception:
                n = fp.stat().st_size
            return n >= min_chars, n
        except Exception:
            return False, 0

    def _auto_repair_missing_sections(self, artifact: str, headers: tuple, text: str) -> bool:
        """Deterministically append any genuinely-missing required headers to
        `artifact` with a clearly-marked placeholder body, instead of just
        flipping the gate to "passed" while the document stays incomplete.
        Returns True if the file was modified. This runs in code, not via the
        LLM's write tool, so it can't get stuck in the same malformed-path /
        overwrite-guard loop the model does."""
        missing = plan_missing_sections(text, headers)
        if not missing:
            return False
        try:
            fp = (self.project_dir / artifact).resolve()
            fp.relative_to(self.project_dir.resolve())
        except Exception:
            return False
        blocks = []
        for h in missing:
            title = h.strip().title()
            blocks.append(
                f"\n\n## {title}\n"
                f"_Auto-filled placeholder — this section was missing after "
                f"{self._phase_rounds} planning rounds and was inserted "
                f"automatically so the document is structurally complete. "
                f"Review and replace with real content._\n"
                f"- TODO: fill in {h}.\n"
            )
        try:
            with open(fp, "a", encoding="utf-8", newline="\n") as f:
                f.write("".join(blocks))
            UI.warn(
                f"Auto-repaired {artifact}: appended placeholder section(s) for "
                f"{', '.join(missing)} (model failed to add them after repeated attempts)."
            )
            try:
                with ReadTool._cache_lock:
                    ReadTool._cache.pop(str(fp), None)
            except Exception:
                pass
            return True
        except Exception as e:
            UI.warn(f"Auto-repair of {artifact} failed: {e}")
            return False

    def _check_tests_pass_gate(self, phase: str) -> bool:
        """Shared by kind == "bash_success" (testing) and
        kind == "verification_criteria_complete" (verification) — both
        need "tests genuinely pass" as a precondition; verification adds
        the acceptance-criteria linkage on top rather than replacing this
        check. Kept as one function so the two phases can't drift into
        checking pass/fail differently."""
        reqs = SDLC_PHASE_REQUIREMENTS.get(phase) or {}
        if self._phase_bash_ok:
            if self.config.get("tdd_enforce_red_green") and phase in ("testing", "verification"):
                if self._tdd_saw_red and not self._tdd_saw_green:
                    UI.info("TDD: saw RED but not GREEN yet — keep fixing until tests pass after a failure.")
                    return False
            # _phase_bash_ok only proves *some* recognized test command
            # exited 0 — it says nothing about how many tests ran or
            # whether any failed alongside a 0 exit (e.g. a runner that
            # exits 0 while reporting failures in a sub-step). Parse the
            # actual digest and require a real, non-zero passed count
            # with no failures/errors before trusting the gate.
            digest_stats = parse_test_digest(self._last_test_digest)
            min_passed = int(self.config.get("min_tests_passed", 1) or 1)
            if digest_stats["parsed"]:
                if digest_stats["failed"] > 0 or digest_stats["errors"] > 0:
                    UI.info(
                        f"Gate NOT met: digest shows {digest_stats['passed']} passed, "
                        f"{digest_stats['failed']} failed, {digest_stats['errors']} errors."
                    )
                    return False
                if digest_stats["passed"] < min_passed:
                    UI.info(
                        f"Gate NOT met: only {digest_stats['passed']} test(s) passed "
                        f"(need ≥{min_passed})."
                    )
                    return False
                # Coverage requirement: OFF by default (min_coverage_pct
                # unset/0). Turning it on is a real product decision —
                # most projects never invoke a coverage tool at all, so
                # enforcing this unconditionally would make the gate
                # impossible to pass for any project that doesn't run
                # `pytest --cov` / `go test -cover` / etc. When a
                # threshold IS configured, only apply it to the
                # verification phase — testing's job is "tests pass",
                # verification's job is "tests actually cover the
                # acceptance criteria", and coverage-of-code is a
                # (partial, imperfect) proxy for coverage-of-criteria.
                # Applying it to both phases would double-count the
                # same requirement rather than checking two things.
                min_cov = float(self.config.get("min_coverage_pct", 0) or 0)
                if min_cov > 0 and phase == "verification":
                    cov = digest_stats["coverage_pct"]
                    if cov is None:
                        UI.info(
                            f"Gate NOT met: min_coverage_pct={min_cov} is configured but no "
                            "coverage summary was found in the test digest — re-run tests with "
                            "coverage enabled (e.g. `pytest --cov`, `go test -cover`)."
                        )
                        return False
                    if cov < min_cov:
                        UI.info(
                            f"Gate NOT met: coverage {cov}% is below required {min_cov}%."
                        )
                        return False
                    UI.info(
                        f"Tests pass: {digest_stats['passed']} passed, 0 failed, "
                        f"coverage {cov}% ≥ {min_cov}%"
                    )
                    return True
                UI.info(f"Tests pass: {digest_stats['passed']} passed, 0 failed")
                return True
            # Digest didn't match a known runner summary format — we
            # can't confirm real coverage, so don't silently trust the
            # bare exit code. Let the round loop keep going (and the
            # missing-gate hint tell the model to produce a recognizable
            # test run) rather than pass on an unparseable signal.
            UI.info(
                "Gate NOT met: test exited 0 but digest format wasn't recognized "
                "(can't confirm pass count)."
            )
            return False
        max_t = int(self.config.get("testing_max_phase_rounds", 150) or 150)
        if max_t > 0 and self._phase_rounds >= max_t:
            # Previously this force-passed the phase after the round
            # limit ("soft-pass with blocker recorded"), which is the
            # same silent-pass pattern as the plan/architecture soft-pass
            # blocks — accidental leniency, not an intended product
            # behavior. Record the blocker and stop autopilot instead of
            # pretending the gate was met; a human decides whether to
            # raise the limit, fix tests, or proceed manually.
            UI.warn(
                f"TESTING circuit-breaker: {self._phase_rounds} rounds without green tests. "
                f"Stopping autopilot — this phase is NOT being marked complete."
            )
            try:
                self.project_state.setdefault("blockers", []).append(
                    f"{phase}: no green tests after {self._phase_rounds} rounds — autopilot halted"
                )
                self.project_state["blockers"] = self.project_state["blockers"][-10:]
                self._update_working_memory(
                    f"{phase} STALLED: no green tests after {self._phase_rounds} rounds. "
                    "Autopilot halted; phase not marked complete.",
                    section="Blockers",
                )
            except Exception:
                pass
            self._stop_autopilot()
            return False
        return False

    def _auto_repair_verification_criteria(
        self, artifact: str, missing: List[str], all_criteria: List[str],
        existing_rows: List[Dict[str, str]],
    ) -> bool:
        """Deterministically scaffold VERIFICATION.md rows for criteria
        that have no matching test reference yet, same pattern as
        _auto_repair_missing_sections: give the model a concrete
        structure to fill in rather than letting the gate stay a vague
        "not met" forever, but never fill in a fake test reference on
        the model's behalf — a placeholder TODO is left for the model to
        replace with a real one, and the row still won't count as
        covered by match_criteria_to_verification until it does.

        Scaffolds each missing criterion AT MOST ONCE per session — a
        set of already-scaffolded (normalized) criteria is tracked so
        repeated gate checks (which happen every turn now, not just
        periodically) don't keep re-appending the same placeholder rows
        and inflating the file without bound. If every remaining missing
        criterion has already been scaffolded once, this is a no-op and
        returns False so the caller doesn't loop expecting a re-parse to
        suddenly find new rows.

        Returns True if the file was modified (which does not by itself
        mean the gate now passes — the caller re-checks after this)."""
        if not hasattr(self, "_verification_scaffolded_criteria"):
            self._verification_scaffolded_criteria: set = set()
        # Bug fix: the in-memory _verification_scaffolded_criteria set only
        # remembers what THIS function has scaffolded before. It does not
        # know about rows the model itself already wrote (e.g. its own
        # initial TODO_TEST_REF pass), nor about scaffolded rows that
        # survived from an earlier session. Re-checking only against that
        # set caused the same placeholder rows to be appended again on
        # every gate re-check, silently duplicating the whole criteria
        # list on disk. Cross-check against a full parse of the file's
        # CURRENT on-disk rows (placeholders included) so a criterion that
        # already has *any* row — TODO or real — is never scaffolded again.
        existing_placeholder_or_real = set()
        try:
            fp_check = (self.project_dir / artifact).resolve()
            fp_check.relative_to(self.project_dir.resolve())
            if fp_check.is_file():
                raw = fp_check.read_text(encoding="utf-8", errors="ignore")
                for row in parse_verification_rows(raw, include_placeholders=True):
                    existing_placeholder_or_real.add(
                        _normalize_criterion_text(row["criterion"])
                    )
        except Exception:
            pass
        to_scaffold = [
            c for c in missing
            if _normalize_criterion_text(c) not in self._verification_scaffolded_criteria
            and _normalize_criterion_text(c) not in existing_placeholder_or_real
        ]
        # Whether or not we're about to write anything new, any criterion
        # that already has a row on disk should be treated as scaffolded
        # going forward, so a stale in-memory set (e.g. after a session
        # restore) doesn't cause a fresh duplicate later.
        self._verification_scaffolded_criteria |= existing_placeholder_or_real
        if not to_scaffold:
            return False
        try:
            fp = (self.project_dir / artifact).resolve()
            fp.relative_to(self.project_dir.resolve())
        except Exception:
            return False
        header_needed = not fp.is_file() or fp.stat().st_size == 0
        blocks = []
        if header_needed:
            blocks.append(
                "# Criteria Verification\n\n"
                "One row per PLAN.md Acceptance Criteria item. Each row MUST name a "
                "specific test that exercises it — replace TODO_TEST_REF with a real "
                "test path/name (e.g. `tests/test_auth.py::test_rejects_bad_password`) "
                "and re-run that test before considering this criterion done. Either "
                "a bullet list (`- [x] <criterion> — test: <ref>`) or a markdown table "
                "(`| <criterion> | <status> | <ref> |`) is accepted.\n\n"
            )
        for c in to_scaffold:
            blocks.append(f"- [ ] {c} — test: TODO_TEST_REF\n")
            self._verification_scaffolded_criteria.add(_normalize_criterion_text(c))
        try:
            with open(fp, "a", encoding="utf-8", newline="\n") as f:
                f.write("".join(blocks))
            UI.warn(
                f"Auto-repaired {artifact}: scaffolded {len(to_scaffold)} criterion row(s) "
                f"needing a real test reference (model hadn't linked them after repeated attempts)."
            )
            try:
                with ReadTool._cache_lock:
                    ReadTool._cache.pop(str(fp), None)
            except Exception:
                pass
            return True
        except Exception as e:
            UI.warn(f"Auto-repair of {artifact} failed: {e}")
            return False

    def _phase_requirements_met(self) -> bool:
        if not self.autopilot or self.sdlc_phase_idx >= len(SDLC_PHASES):
            return False
        
        # Ensure no placeholders exist in artifacts relevant to current or prior phases
        phase = SDLC_PHASES[self.sdlc_phase_idx]
        relevant_docs = []
        if phase == "planning":
            relevant_docs = ["PLAN.md"]
        elif phase == "architecture":
            relevant_docs = ["PLAN.md", "ARCHITECTURE.md"]
        elif phase == "design":
            relevant_docs = ["PLAN.md", "ARCHITECTURE.md", "DESIGN.md"]
        else:
            relevant_docs = ["PLAN.md", "ARCHITECTURE.md", "DESIGN.md", "VERIFICATION.md"]

        for name in relevant_docs:
            fp = self.project_dir / name
            if fp.exists():
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace").lower()
                    if "auto-filled placeholder" in text or "todo: fill in" in text:
                        UI.info(f"Gate NOT met: {name} contains placeholders. The agent must rewrite it with real content.")
                        return False
                except Exception:
                    pass

        reqs = SDLC_PHASE_REQUIREMENTS.get(phase)
        if not reqs:
            return False
        kind = reqs.get("kind")
        if kind == "plan_complete":
            artifact = reqs.get("artifact") or getattr(self, "plan_artifact", "PLAN.md")
            ok, n = self._artifact_ok(artifact, int(reqs.get("min_chars", 400)))
            added = self._phase_tool_counts.get("todo_add", 0)
            board = len(TodoTool._todos) if TodoTool._todos else 0
            min_todos = int(reqs.get("min_todos", 5))
            todo_ok = added >= min_todos or board >= min_todos
            plan_text = self._read_artifact_snippet(artifact, max_chars=50000)
            headers = tuple(h.lower() for h in (reqs.get("require_sections") or PLAN_REQUIRED_HEADERS))
            sections_ok = plan_has_sections(plan_text, headers) if plan_text else False
            if ok and n >= 1500 and not sections_ok:
                # Genuine repair attempt: this actually edits the artifact
                # to add the missing headers, so it's kept. What's removed
                # is the old fallback that set sections_ok = True even when
                # repair failed or was skipped — that was an unconditional
                # silent pass, not a real check. If repair doesn't succeed,
                # sections_ok stays whatever plan_has_sections actually
                # found, and the gate is allowed to fail; a stuck phase is
                # now handled once, by the round circuit-breaker, instead
                # of being quietly waved through here too.
                if self._auto_repair_missing_sections(artifact, headers, plan_text):
                    ok, n = self._artifact_ok(artifact, int(reqs.get("min_chars", 400)))
                    plan_text = self._read_artifact_snippet(artifact, max_chars=50000)
                    sections_ok = plan_has_sections(plan_text, headers) if plan_text else False
            research_ok = True
            if reqs.get("require_research") and self.config.get("require_research_for_external", True):
                if goal_looks_external(self.goal or ""):
                    research_ok = (
                        self._research_done
                        or self._phase_tool_success_counts.get("web_search", 0) > 0
                        or self._phase_tool_success_counts.get("browse_page", 0) > 0
                        or bool(self.project_state.get("research_notes"))
                    )
            # NOTE: the two round-count "soft-pass"/"hard soft-pass" blocks
            # that used to live here (forcing todo_ok/sections_ok True after
            # 12 or 18 rounds) have been removed for the same reason as
            # above — they were a second, independent silent-pass mechanism
            # duplicating what the round circuit-breaker now handles
            # explicitly (halt autopilot, record a blocker, don't advance).
            # Require at least one real, extractable acceptance criterion
            # under the heading — not merely that the heading exists.
            # plan_has_sections only checks heading presence; without this,
            # a PLAN.md with "## Acceptance Criteria" and zero list items
            # could pass planning, leave project_state["acceptance_criteria"]
            # empty, and force verification into the silent-pass path.
            criteria_populated = bool(extract_acceptance_criteria(plan_text)) if plan_text else False
            if not criteria_populated:
                UI.info(
                    f"Gate NOT met: {artifact} has the Acceptance Criteria heading "
                    f"but no extractable list items — add at least one concrete "
                    f"criterion (e.g. '- [ ] Users can log in with valid credentials') "
                    f"before planning can complete."
                )
            met = ok and todo_ok and sections_ok and research_ok and criteria_populated
            if met:
                UI.info(
                    f"Gate met: {artifact} ({n}c) + todos={max(added, board)} "
                    f"+ sections + research + criteria → {phase.upper()}"
                )
            return met
        if kind == "architecture_complete":
            artifact = reqs.get("artifact") or getattr(self, "architecture_artifact", "ARCHITECTURE.md")
            ok, n = self._artifact_ok(artifact, int(reqs.get("min_chars", 300)))
            total = sum(self._phase_tool_success_counts.get(x, 0) for x in reqs.get("any_tool", ["write", "edit"]))
            if ok and n >= 300:
                total = max(total, 1)
            arch_text = self._read_artifact_snippet(artifact, max_chars=50000)
            headers = tuple(h.lower() for h in (reqs.get("require_sections") or ARCH_REQUIRED_HEADERS))
            sections_ok = plan_has_sections(arch_text, headers) if arch_text else False
            if ok and n >= 1500 and not sections_ok:
                # Same principle as the plan gate above: keep the genuine
                # repair attempt, drop the unconditional force-True that
                # used to follow it regardless of whether repair worked.
                if self._auto_repair_missing_sections(artifact, headers, arch_text):
                    ok, n = self._artifact_ok(artifact, int(reqs.get("min_chars", 300)))
                    arch_text = self._read_artifact_snippet(artifact, max_chars=50000)
                    sections_ok = plan_has_sections(arch_text, headers) if arch_text else False
            met = ok and total >= int(reqs.get("min_calls", 2)) and sections_ok
            if met:
                UI.info(f"Gate met: {artifact} ({n}c) + writes={total} + sections → {phase.upper()}")
            return met
        if kind == "implementation_complete":
            total = sum(self._phase_tool_success_counts.get(x, 0) for x in reqs.get("any_tool", ["write", "edit"]))
            src_n = self._phase_tool_counts.get("src_write", 0)
            min_src = int(reqs.get("min_src_writes", self.config.get("min_implementation_src_writes", 4)))
            validation_ok = not reqs.get("require_validation") or self._validation_ready
            if reqs.get("require_validation") and not validation_ok:
                report = self.project_state.get("validation") or {}
                blockers = report.get("blockers") or []
                UI.info(
                    "Gate NOT met: production validation is missing or blocked"
                    + (f" — {'; '.join(str(x) for x in blockers[:2])}" if blockers else
                       " — call validate_project(run_checks=true)")
                )
            # min_calls/min_src_writes are pure COUNTS — a model could
            # satisfy both with 8 tiny writes of a few bytes each and pass,
            # since nothing here previously looked at how much was actually
            # written. _phase_chars_by_path already tracks the best (max)
            # content length seen per path this phase; use it to require a
            # real average size across the src/ paths that were touched, so
            # the count thresholds can't be satisfied with near-empty files.
            # An average alone is gameable (pad one file, leave the rest at
            # a few bytes and let the padded file carry the mean), so this
            # also enforces a much lower per-file floor — every touched src
            # path must clear a minimal size on its own, not just contribute
            # to the group average. This does not replace validate_project's
            # placeholder/stub/shallow-body scan — it's a cheaper, earlier
            # filter that stops the phase from reaching "gate met" on
            # quantity (or a gamed average) alone.
            min_avg_chars = int(reqs.get("min_avg_src_chars", self.config.get("min_avg_src_chars", 200)))
            min_file_floor = int(reqs.get("min_src_file_chars", self.config.get("min_src_file_chars", 40)))
            src_chars_ok = True
            avg_src_chars = 0
            thin_files: List[str] = []
            if src_n > 0 and (min_avg_chars > 0 or min_file_floor > 0):
                src_path_keys = [
                    (p, Tool._sanitize_rel_path(p) or p) for p in self._src_write_paths
                ]
                touched = [
                    (orig, self._phase_chars_by_path.get(key, 0))
                    for orig, key in src_path_keys
                ]
                touched = [(p, n) for p, n in touched if n > 0]
                if touched:
                    lens = [n for _, n in touched]
                    avg_src_chars = sum(lens) // len(lens)
                    thin_files = [p for p, n in touched if n < min_file_floor]
                    src_chars_ok = (
                        (avg_src_chars >= min_avg_chars if min_avg_chars > 0 else True)
                        and not thin_files
                    )
            if not src_chars_ok:
                reason = []
                if min_avg_chars > 0 and avg_src_chars < min_avg_chars:
                    reason.append(f"average only {avg_src_chars} chars (need >={min_avg_chars})")
                if thin_files:
                    reason.append(f"{len(thin_files)} file(s) under {min_file_floor} chars: {', '.join(thin_files[:3])}")
                UI.info(
                    "Gate NOT met: src writes look too thin to be real implementation "
                    f"({'; '.join(reason)}) — add actual logic, not stub-sized files."
                )
            met = (
                total >= int(reqs.get("min_calls", 8))
                and src_n >= min_src
                and validation_ok
                and src_chars_ok
            )
            if met:
                UI.info(
                    f"Gate met: write/edit={total}, src_writes={src_n}, "
                    f"avg src size={avg_src_chars}c, "
                    f"production validation=ready → {phase.upper()}"
                )
                return True
            # Escape hatch for "nothing meaningful left to change": if this
            # phase has already produced at least one real source write/edit
            # (src_n >= 1, so this isn't a no-op restart of an untouched
            # phase) AND a real test command has already exited 0 with a
            # parsed digest showing passed>0/failed==0/errors==0 during this
            # phase, the raw call-count quota is waived. Without this, a
            # goal that's already fully implemented (e.g. restored from a
            # prior session) has no legitimate work left to satisfy
            # min_calls/min_src_writes, so the model is forced into
            # cosmetic re-writes of already-correct files, which the
            # anti-spam blocker then blocks, producing the exact
            # BLOCKED SPAM / BLOCKED REPEAT deadlock the round
            # circuit-breaker has to bail out of. Requiring src_n >= 1
            # keeps this from ever firing on a phase where the model has
            # done zero implementation work at all — it isn't a way to
            # skip the phase, only a way to stop demanding busy-work once
            # genuine work already happened and tests already prove it.
            if src_n >= 1 and self._phase_bash_ok and validation_ok and src_chars_ok:
                digest_stats = parse_test_digest(self._last_test_digest)
                min_passed = int(self.config.get("min_tests_passed", 1) or 1)
                if (
                    digest_stats["parsed"]
                    and digest_stats["failed"] == 0
                    and digest_stats["errors"] == 0
                    and digest_stats["passed"] >= min_passed
                ):
                    UI.info(
                        f"Gate met (already-complete shortcut): src_writes={src_n} "
                        f"+ tests passing ({digest_stats['passed']} passed, 0 failed) "
                        f"→ {phase.upper()}"
                    )
                    return True
            return False
        if kind == "todo_count":
            added = self._phase_tool_counts.get("todo_add", 0)
            met = added >= reqs.get("min_todos", 3)
            if met:
                UI.info(f"Gate met: {added} todos → {phase.upper()}")
            return met
        if kind == "tool_calls":
            total = sum(self._phase_tool_success_counts.get(x, 0) for x in reqs.get("any_tool", []))
            calls_ok = total >= reqs.get("min_calls", 1)
            min_chars = int(reqs.get("min_chars_written", 0) or 0)
            chars_ok = self._phase_chars_written >= min_chars if min_chars else True
            met = calls_ok and chars_ok
            if met:
                UI.info(
                    f"Gate met: {total} successful tool calls"
                    + (f" + {self._phase_chars_written} chars written" if min_chars else "")
                    + f" → {phase.upper()}"
                )
            elif calls_ok and not chars_ok:
                UI.info(
                    f"Gate NOT met: {total} successful tool calls but only "
                    f"{self._phase_chars_written}/{min_chars} chars actually written "
                    f"— calls succeeded without producing real content."
                )
            return met
        if kind == "bash_success":
            if reqs.get("require_validation") and not self._validation_ready:
                UI.info("Gate NOT met: production validation has not passed; call validate_project and fix every blocker.")
                return False
            return self._check_tests_pass_gate(phase)
        if kind == "verification_criteria_complete":
            # Verification needs two independent things to be true, not
            # one standing in for the other: (1) tests actually pass
            # (same check as bash_success), and (2) each acceptance
            # criterion captured from PLAN.md at the end of planning is
            # linked, in a structural artifact, to a specific test — not
            # just re-certified by the model's own prose. Both are
            # required; passing tests alone no longer means "verified".
            if reqs.get("require_validation") and not self._validation_ready:
                UI.info("Gate NOT met: production validation has not passed; call validate_project and fix every blocker.")
                return False
            tests_ok = self._check_tests_pass_gate(phase)
            if not tests_ok:
                return False
            criteria = self.project_state.get("acceptance_criteria") or []
            artifact = reqs.get("artifact") or "VERIFICATION.md"
            if not criteria:
                # Safety-net recovery for the staleness race: planning's
                # gate may have latched before PLAN.md's Acceptance
                # Criteria section was fully written, so the snapshot
                # stored at phase-advance time is empty even though the
                # file on disk now has real items. Re-extract once from
                # the current PLAN.md — same trusted parser as the
                # original snapshot — and persist the recovered list so
                # this only fires once. Deliberately does NOT re-extract
                # when the stored list is already non-empty: that would
                # reopen the gaming loophole the snapshot was designed
                # to close (model writing easy criteria late and
                # self-certifying against them).
                plan_artifact = self.config.get("plan_artifact", "PLAN.md")
                plan_ok, _ = self._artifact_ok(plan_artifact, min_chars=1)
                if plan_ok:
                    plan_text = self._read_artifact_snippet(plan_artifact, max_chars=50000)
                    recovered = extract_acceptance_criteria(plan_text) if plan_text else []
                    if recovered:
                        criteria = recovered
                        self.project_state["acceptance_criteria"] = recovered
                        try:
                            self._sync_project_state()
                            self._update_working_memory(
                                f"verification recovery: re-extracted {len(recovered)} "
                                f"acceptance criteria from {plan_artifact} "
                                f"(planning-time snapshot was empty)",
                                section="Decisions",
                            )
                        except Exception:
                            pass
                        UI.info(
                            f"Recovered {len(recovered)} acceptance criteria from "
                            f"{plan_artifact} (stored snapshot was empty) — proceeding "
                            f"to linkage check."
                        )
            if not criteria:
                # Recovery also came up empty — either PLAN.md genuinely
                # has no extractable criteria, or the file is missing.
                # Don't block verification forever on a list that will
                # never exist; but also don't silently treat "nothing to
                # check" as "everything checked" without at least a
                # passing test run, which tests_ok above already required.
                UI.info(
                    f"Gate met: tests pass, no acceptance criteria were captured from planning "
                    f"→ {phase.upper()}"
                )
                return True
            ok, n = self._artifact_ok(artifact, min_chars=1)
            text = self._read_artifact_snippet(artifact, max_chars=50000) if ok else ""
            rows = parse_verification_rows(text) if text else []
            covered, missing = match_criteria_to_verification(criteria, rows)
            if missing:
                if self._auto_repair_verification_criteria(artifact, missing, criteria, rows):
                    text = self._read_artifact_snippet(artifact, max_chars=50000)
                    rows = parse_verification_rows(text) if text else []
                    covered, missing = match_criteria_to_verification(criteria, rows)
                if missing:
                    # _check_tests_pass_gate above has its own circuit
                    # breaker, but it only guards the "tests pass" half of
                    # this gate — once tests are green, tests_ok returns
                    # True immediately and that breaker never runs again.
                    # Without a separate one here, a VERIFICATION.md that
                    # never gets criteria linked (e.g. the model repeatedly
                    # editing the wrong rows, or a stuck auto-repair) can
                    # loop this phase indefinitely even with passing tests,
                    # since nothing else bounds _phase_rounds for this half
                    # of the check. Mirror the testing breaker's behavior:
                    # halt autopilot and record a blocker rather than
                    # looping forever or silently soft-passing.
                    max_v = int(self.config.get("verification_max_phase_rounds", 150) or 150)
                    if max_v > 0 and self._phase_rounds >= max_v:
                        UI.warn(
                            f"VERIFICATION circuit-breaker: {self._phase_rounds} rounds without "
                            f"all acceptance criteria linked to tests in {artifact}. "
                            f"Stopping autopilot — this phase is NOT being marked complete."
                        )
                        try:
                            self.project_state.setdefault("blockers", []).append(
                                f"{phase}: {len(missing)}/{len(criteria)} acceptance criteria "
                                f"still unlinked in {artifact} after {self._phase_rounds} rounds "
                                "— autopilot halted"
                            )
                            self.project_state["blockers"] = self.project_state["blockers"][-10:]
                            self._update_working_memory(
                                f"{phase} STALLED: {len(missing)}/{len(criteria)} acceptance "
                                f"criteria unlinked in {artifact} after {self._phase_rounds} "
                                "rounds. Autopilot halted; phase not marked complete.",
                                section="Blockers",
                            )
                        except Exception:
                            pass
                        self._stop_autopilot()
                        return False
                    UI.info(
                        f"Gate NOT met: {len(missing)}/{len(criteria)} acceptance criteria have no "
                        f"matching test reference in {artifact}."
                    )
                    return False
            UI.info(
                f"Gate met: tests pass + {len(covered)}/{len(criteria)} acceptance criteria "
                f"linked to tests in {artifact} → {phase.upper()}"
            )
            return True
        if kind == "deployment_complete":
            artifact = reqs.get("artifact") or "docker-compose.yml"
            ok, n = self._artifact_ok(artifact, int(reqs.get("min_chars", 500)))
            if not ok or n < 500:
                UI.info(f"Gate NOT met: {artifact} missing or too short ({n} chars)")
                return False
            # Check required services
            text = self._read_artifact_snippet(artifact, max_chars=50000)
            headers = tuple(h.lower() for h in (reqs.get("require_sections") or ["frontend", "backend", "database", "services"]))
            for h in headers:
                if h.lower() not in text.lower():
                    UI.info(f"Gate NOT met: {artifact} missing required service '{h}'")
                    return False
            # Check Dockerfile
            dockerfile_ok, _ = self._artifact_ok("Dockerfile", 100)
            if not dockerfile_ok:
                UI.info("Gate NOT met: Dockerfile missing")
                return False
            UI.info(f"Gate met: {artifact} has all required services + Dockerfile → {phase.upper()}")
            return True
        if kind == "product_validation":
            # Product validation needs: (1) tests pass, (2) criteria linked, (3) product exists
            if reqs.get("require_validation") and not self._validation_ready:
                UI.info("Gate NOT met: production validation has not passed; scaffold or placeholder code remains.")
                return False
            tests_ok = self._check_tests_pass_gate(phase)
            if not tests_ok:
                return False
            criteria = self.project_state.get("acceptance_criteria") or []
            artifact = reqs.get("artifact") or "VERIFICATION.md"
            if criteria:
                ok, n = self._artifact_ok(artifact, min_chars=1)
                text = self._read_artifact_snippet(artifact, max_chars=50000) if ok else ""
                rows = parse_verification_rows(text) if text else []
                covered, missing = match_criteria_to_verification(criteria, rows)
                if missing:
                    UI.info(f"Gate NOT met: {len(missing)}/{len(criteria)} acceptance criteria unlinked")
                    return False
            # Product existence checks
            product_checks = reqs.get("product_checks", [])
            for check in product_checks:
                if check == "docker-compose.yml":
                    ok, n = self._artifact_ok("docker-compose.yml", 500)
                    if not ok:
                        UI.info("Gate NOT met: docker-compose.yml missing")
                        return False
                elif check == "Dockerfile":
                    ok, _ = self._artifact_ok("Dockerfile", 100)
                    if not ok:
                        UI.info("Gate NOT met: Dockerfile missing")
                        return False
                elif check == "frontend":
                    ok = Path(self.project_dir).joinpath("frontend", "package.json").exists()
                    if not ok:
                        UI.info("Gate NOT met: frontend missing (frontend/package.json)")
                        return False
                elif check == "backend":
                    ok = Path(self.project_dir).joinpath("backend", "main.py").exists() or Path(self.project_dir).joinpath("backend", "app", "main.py").exists()
                    if not ok:
                        UI.info("Gate NOT met: backend missing")
                        return False
                elif check == "live_preview":
                    ok = Path(self.project_dir).joinpath("src", "core", "preview", "dev_server.py").exists()
                    if not ok:
                        UI.info("Gate NOT met: live_preview infrastructure missing")
                        return False
                elif check == "project_persistence":
                    ok = Path(self.project_dir).joinpath("src", "core", "projects").exists() or Path(self.project_dir).joinpath("backend", "app", "models").exists()
                    if not ok:
                        UI.info("Gate NOT met: project_persistence missing")
                        return False
            UI.info(f"Gate met: product validation passed → {phase.upper()}")
            return True

    def _should_advance_phase(self, content: str) -> bool:
        if not self.autopilot:
            return False
        phase = SDLC_PHASES[self.sdlc_phase_idx] if self.sdlc_phase_idx < len(SDLC_PHASES) else ""
        self._phase_rounds += 1
        if phase in ("planning", "architecture", "implementation", "testing", "verification"):
            return False
        lower = (content or "").lower()
        signals = {
            "design": ["design complete", "ui designed", "design is complete"],
            "review": ["review complete", "review is complete", "code review done"],
            "deployment": ["deployment ready", "deployment complete", "dockerfile created", "docker-compose created"],
        }
        for signal in signals.get(phase, []):
            if signal in lower and self._phase_requirements_met():
                UI.info(f"Signal+gate: '{signal}' → {phase.upper()}")
                return True
        return False

    def cmd_files(self):
        tree = UI.file_tree(self.project_dir, self.project_dir)
        if HAS_RICH:
            box_style = ASCII if LEGACY_WIN_CONSOLE else ROUNDED
            console.print(Panel(tree, title="[neon_cyan]Project Files[/neon_cyan]", border_style="neon_magenta", padding=(1, 2), box=box_style))
        else:
            print(tree)

    def cmd_cost(self):
        UI.info(self.meter.report())

    def cmd_compact(self):
        before = self.ctx.estimate_tokens()
        self._update_working_memory(
            f"compact at phase={SDLC_PHASES[self.sdlc_phase_idx] if self.sdlc_phase_idx < len(SDLC_PHASES) else 'done'}; "
            f"goal={(self.goal or '')[:120]}",
            section="Facts",
        )
        self._sync_project_state()
        if self.ctx.compact(keep_recent=8):
            after = self.ctx.estimate_tokens()
            UI.ok(f"Compacted: ~{before:,} → ~{after:,} tokens (state + working memory preserved)")
            self._rebuild_system()
        else:
            UI.info("Nothing to compact yet.")

    def cmd_model(self, name: str):
        if name in MODELS:
            self.model_key = name
            self.model_cfg = MODELS[name]
            self.config["default_model"] = name
            # self.model_cfg alone is cosmetic: _build_payload() sources the
            # actual API model/params from self.pool.current(), and the pool
            # is a fixed provider list built once at startup from whatever
            # default_model was at that time. Without rebuilding it here,
            # /model <name> updates the display/config but every real
            # request keeps hitting whichever model was primary at launch.
            try:
                self.pool = build_pool(self.config)
                UI.ok(f"Switched to {self.model_cfg['name']}")
            except Exception as e:
                UI.err(f"Switched config to {self.model_cfg['name']} but pool rebuild failed: {e}")
            self._rebuild_system()
            self.persist(force=True)
        else:
            available = ", ".join(MODELS.keys())
            UI.err(f"Unknown model '{name}'. Available: {available}")

    def cmd_todo(self):
        todo_tool = TodoTool(self.project_dir)
        result = todo_tool.execute(action="list")
        if HAS_RICH:
            box_style = ASCII if LEGACY_WIN_CONSOLE else ROUNDED
            console.print(Panel(
                Text.from_markup(f"[neon_yellow]{UI.esc(result.text())}[/neon_yellow]"),
                title="[neon_cyan]Task Tracker[/neon_cyan]",
                border_style="neon_magenta",
                box=box_style
            ))
        else:
            print(result.text())


    def offer_resume_autopilot(self) -> bool:
        if self.autopilot:
            return False
        if not self.goal:
            return False
        if self.sdlc_phase_idx >= len(SDLC_PHASES):
            return False
        wants = bool(getattr(self, "_session_wants_resume", False))
        try:
            wants = wants or bool(self.project_state.get("autopilot_resume"))
            wants = wants or bool(self.project_state.get("autopilot_active"))
        except Exception:
            pass
        if not wants and not self.sdlc_completed and self.sdlc_phase_idx == 0:
            return False
        phase = SDLC_PHASES[self.sdlc_phase_idx]
        UI.separator("RESUME AUTOPILOT?")
        UI.info(f"Goal: {self.goal}")
        UI.info(f"Last phase: {phase.upper()} | completed: {', '.join(sorted(self.sdlc_completed)) or 'none'}")
        UI.info("Press Enter or y to resume autopilot from this phase.")
        UI.info("Type n to stay in interactive mode (goal kept).")
        try:
            ans = input("  Resume autopilot? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            ans = "n"
        if ans in ("", "y", "yes"):
            return bool(self._start_autopilot(restart=False))
        UI.info("Staying interactive. /autopilot when ready, /autopilot restart for a full restart.")
        try:
            self.project_state["autopilot_active"] = False
            self._sync_project_state()
        except Exception:
            pass
        return False

    def cmd_goal(self, goal_text: str = ""):
        if not goal_text.strip():
            if self.goal:
                UI.info(f"Current goal: {self.goal}")
                if not self.autopilot:
                    UI.info("Type /autopilot to start autonomous development toward this goal.")
            else:
                UI.info("No goal set. Use /goal <description> to set one, then /autopilot to start.")
            return
        new_goal = goal_text.strip()
        if self.goal == new_goal and self.sdlc_phase_idx > 0:
            UI.info(
                f"[SDLC SAFEGUARD] Goal re-asserted without changes. "
                f"Retaining active SDLC phase: {SDLC_PHASES[self.sdlc_phase_idx].upper()} "
                f"(completed: {', '.join(sorted(self.sdlc_completed)) or 'none'})."
            )
            self._rebuild_system()
            self.persist(force=True)
            return
        self.goal = new_goal
        self.autopilot = False
        if not self.sdlc_completed and self.sdlc_phase_idx == 0:
            self.sdlc_phase_idx = 0
            self.sdlc_completed.clear()
        else:
            UI.info(
                f"[SDLC SAFEGUARD] Preserving active SDLC phase: {SDLC_PHASES[self.sdlc_phase_idx].upper()} "
                f"(completed: {', '.join(sorted(self.sdlc_completed)) or 'none'}) for updated goal. "
                "Use /reset to restart SDLC from planning."
            )
        self._rebuild_system()
        self.persist(force=True)
        UI.ok(f"Goal set: {self.goal}")
        UI.info("Type /autopilot to enter GOD MODE — the agent will autonomously run full SDLC with TDD until the goal is reached.")
        UI.info("Type /status to check progress, /stop to halt.")

    def cmd_persona(self, persona_key: str = ""):
        if not persona_key.strip():
            current = PERSONAS[self.persona_key]
            UI.info(f"Current: {current['name']} — {current['description']}")
            UI.info("Available: " + ", ".join(f"{k} ({v['name']})" for k, v in PERSONAS.items()))
            if self.persona_history:
                UI.info("Recent: " + " → ".join(PERSONAS[p]["name"] for p in self.persona_history))
            return
        if self._switch_persona(persona_key.strip().lower()):
            pass
        else:
            available = ", ".join(PERSONAS.keys())
            UI.err(f"Unknown persona '{persona_key}'. Available: {available}")

    def cmd_autopilot(self, restart: bool = False):
        if self.autopilot:
            UI.info("Autopilot is already running.")
            self._autopilot_status()
            return
        if not self.goal:
            UI.err("No goal set. Use /goal <description> first.")
            return
        if not self._start_autopilot(restart=restart):
            return
        if self.config.get("git_worktree_on_autopilot"):
            self.cmd_worktree(auto=True)
        self.run_turn(self._autopilot_prompt())

    def cmd_permission(self, mode: str = "status") -> None:
        mode = (mode or "status").strip().lower()
        if mode in ("status", "show", ""):
            UI.separator("PERMISSIONS")
            UI.info("File access: PROJECT FOLDER ONLY (hard limit)")
            UI.info(f"full_access (in-project) = {self.full_access}")
            UI.info(f"allow_local_network = {bool(self.config.get('allow_local_network'))}")
            UI.info("run_tool = structured program+args (no shell string; destructive git ops blocked outright)")
            UI.info(f"git_worktree_on_autopilot = {bool(self.config.get('git_worktree_on_autopilot'))}")
            UI.info(f"tdd_enforce_red_green = {bool(self.config.get('tdd_enforce_red_green'))}")
            UI.info("Commands: /permission full | strict | local | nolo | status")
            return
        if mode in ("full", "default"):
            self.full_access = True
            self.config["full_access"] = True
            self.config["allow_path_outside_project"] = False
            self.config["allow_local_network"] = True
            self.config["bash_confirm_destructive"] = False
            self.tools = build_tools(
                self.project_dir, full_access=True, allow_outside=False,
                allow_local=True, bash_confirm=False,
            )
            UI.ok("Full in-project permission (default). Files stay inside project folder.")
            return
        if mode in ("strict", "sandbox", "safe"):
            self.full_access = False
            self.config["full_access"] = False
            self.config["bash_confirm_destructive"] = True
            self.tools = build_tools(
                self.project_dir, full_access=False, allow_outside=False,
                allow_local=bool(self.config.get("allow_local_network", True)),
                bash_confirm=True,
            )
            UI.ok("Strict mode enabled. Files still project-only. (run tool blocks destructive git ops outright; no confirm step exists yet.)")
            return
        if mode == "local":
            self.config["allow_local_network"] = True
            if "browse_page" in self.tools:
                self.tools["browse_page"].allow_local_network = True
            UI.ok("Local network browse allowed.")
            return
        if mode in ("nolo", "nolocal"):
            self.config["allow_local_network"] = False
            if "browse_page" in self.tools:
                self.tools["browse_page"].allow_local_network = False
            UI.ok("Local network browse disabled.")
            return
        if mode == "outside":
            UI.warn(
                "Outside-project file access is disabled by policy. "
                "All file tools are limited to the project folder."
            )
            return
        UI.err(f"Unknown permission mode: {mode}. Use full | strict | local | nolo | status")

    def cmd_worktree(self, auto: bool = False) -> None:
        branch = f"neon/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        path = self.project_dir / ".neon_worktrees" / branch.replace("/", "_")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            chk = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(self.project_dir), capture_output=True, text=True, timeout=15,
            )
            if chk.returncode != 0:
                if auto:
                    UI.info("Not a git repo — skipping worktree.")
                    return
                UI.err("Not a git repository. Run git init first, or ignore worktree.")
                return
            r = subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(path)],
                cwd=str(self.project_dir), capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                UI.warn(f"worktree failed: {(r.stderr or r.stdout or '')[:300]}")
                return
            UI.ok(f"Git worktree ready: {path} (branch {branch})")
            UI.info("Point future work there manually or re-launch with -p to that path.")
            self._update_working_memory(f"worktree: {path} branch={branch}", section="Decisions")
            self.project_state["worktree"] = {"path": str(path), "branch": branch}
            self._sync_project_state()
        except Exception as e:
            UI.warn(f"worktree error: {e}")


def load_config() -> Dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    raw: Dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                raw = loaded
        except Exception:
            raw = {}

    migrated = False

    if "projects" not in raw or not isinstance(raw.get("projects"), dict):
        raw["projects"] = {}
        migrated = True

    legacy_key = (raw.get("api_key") or "").strip() if isinstance(raw.get("api_key"), str) else ""
    legacy_proj = raw.get("project_dir") or raw.get("active_project")
    if legacy_key and legacy_proj:
        pkey = str(Path(str(legacy_proj)).resolve())
        entry = dict(raw["projects"].get(pkey) or {})
        if not entry.get("api_key"):
            entry["api_key"] = legacy_key
            migrated = True
        for field in (
            "default_model", "default_persona", "last_goal",
            "last_sdlc_phase_idx", "last_sdlc_completed", "thinking_effort",
        ):
            if field in raw and field not in entry:
                entry[field] = raw[field]
                migrated = True
        raw["projects"][pkey] = entry
        raw["active_project"] = pkey
        if "api_key" in raw:
            del raw["api_key"]
            migrated = True

    try:
        for kp in SESSIONS_DIR.glob("project_*.key.json"):
            data = _safe_json_load(kp)
            if not data:
                continue
            proj = data.get("project")
            key = (data.get("api_key") or "").strip()
            if not proj or not key:
                continue
            pkey = str(Path(str(proj)).resolve())
            entry = dict(raw["projects"].get(pkey) or {})
            if entry.get("api_key") != key:
                entry["api_key"] = key
                raw["projects"][pkey] = entry
                migrated = True
    except Exception:
        pass

    for k, v in DEFAULT_CONFIG.items():
        if k == "api_key":
            continue
        if k not in raw:
            raw[k] = v
            migrated = True

    # Sanitize default_model at both the root level and inside every saved
    # project entry. Unlike phase_models (cleaned per-turn in
    # _apply_phase_model), a stale default_model was never validated here,
    # so an old model id left over from a prior model-stack swap would
    # silently survive every load, get merged into runtime by
    # apply_project_to_runtime, and break every request for that project
    # until someone noticed and ran /model manually.
    if raw.get("default_model") not in MODELS:
        bad = raw.get("default_model")
        if bad:
            UI.warn(
                f"default_model '{bad}' is not a known model; resetting to "
                f"'{DEFAULT_CONFIG['default_model']}'."
            )
        raw["default_model"] = DEFAULT_CONFIG["default_model"]
        migrated = True
    for _pkey, _entry in list(raw.get("projects", {}).items()):
        if not isinstance(_entry, dict):
            continue
        _dm = _entry.get("default_model")
        if _dm is not None and _dm not in MODELS:
            UI.warn(
                f"Project '{_pkey}': default_model '{_dm}' is not a known "
                f"model; removing stale override (falls back to the global "
                f"default_model)."
            )
            del _entry["default_model"]
            migrated = True

    raw["version"] = APP_VERSION
    if "projects" not in raw:
        raw["projects"] = {}

    _HARDWIRE = {
        "stream_timeout": 420.0,
        "request_timeout": 420.0,
        "stream_content_timeout": 200.0,
        "first_token_timeout": 60.0,
        "http_read_timeout": 180.0,
        "http_connect_timeout": 15.0,
        "retry_max": 8,
        "retry_base_delay": 2.5,
        "max_retries_per_provider": 3,
        "nim_shared_rpm": 38.0,
        "inter_turn_delay": 0.35,
        "compact_threshold": 5000,
        "startup_compact_tokens": 60000,
        "max_rounds": 0,
        "max_autopilot_rounds": 0,
        "max_phase_rounds": 150,
        "testing_max_phase_rounds": 150,
        "verification_max_phase_rounds": 150,
        "max_same_tool_successes": 3,
        "post_429_backoff": 25.0,
        "rpm_safety": 0.80,
        # Was hardwired to "off" — every config load silently reset this
        # back regardless of what was saved. Now hardwired to "low" instead,
        # per explicit request. allow_medium_high_thinking stays False, so
        # "low" is the ceiling; raise both together if you want more.
        "thinking_effort": "low",
        "allow_medium_high_thinking": False,
        "show_thinking": True,
        "autopilot_thinking": False,
    }
    for _k, _v in _HARDWIRE.items():
        if raw.get(_k) != _v:
            raw[_k] = _v
            migrated = True

    if migrated:
        try:
            save_config(raw)
        except Exception:
            pass
    return raw


def save_config(cfg: Dict[str, Any]):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    out = dict(cfg)
    for k in ("api_key", "_fresh", "_root"):
        out.pop(k, None)
    if not isinstance(out.get("projects"), dict) or not out["projects"]:
        out.setdefault("projects", {})
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


def get_project_entry(cfg: Dict[str, Any], project_dir: Path) -> Dict[str, Any]:
    pkey = str(Path(project_dir).resolve())
    projects = cfg.setdefault("projects", {})
    if pkey not in projects or not isinstance(projects[pkey], dict):
        projects[pkey] = {}
    return projects[pkey]


def apply_project_to_runtime(cfg: Dict[str, Any], project_dir: Path) -> Dict[str, Any]:
    pkey = str(Path(project_dir).resolve())
    entry = get_project_entry(cfg, project_dir)
    runtime = dict(DEFAULT_CONFIG)
    for k, v in cfg.items():
        if k in ("projects", "api_key", "active_project"):
            continue
        runtime[k] = v
    for k, v in entry.items():
        runtime[k] = v
    normalize_thinking_policy(runtime)
    runtime["http_read_timeout"] = max(180.0, float(runtime.get("http_read_timeout") or 180.0))
    runtime["stream_timeout"] = max(
        float(runtime.get("stream_timeout") or 420.0),
        float(runtime.get("first_token_timeout") or 60.0) + 60.0,
    )
    runtime["request_timeout"] = max(
        float(runtime.get("request_timeout") or 420.0),
        float(runtime.get("stream_timeout") or 420.0),
    )
    runtime["project_dir"] = pkey
    runtime["api_key"] = (entry.get("api_key") or "").strip()
    cfg["active_project"] = pkey
    return runtime


def save_project_settings(cfg: Dict[str, Any], project_dir: Path, **fields: Any) -> None:
    entry = get_project_entry(cfg, project_dir)
    for k, v in fields.items():
        if v is None and k in entry:
            continue
        entry[k] = v
    cfg["active_project"] = str(Path(project_dir).resolve())
    save_config(cfg)


def ensure_api_key(cfg: Dict[str, Any], project_dir: Optional[Path] = None) -> bool:
    project = Path(project_dir or cfg.get("project_dir") or ".").resolve()
    root = cfg.get("_root") if isinstance(cfg.get("_root"), dict) else None
    if not isinstance(root, dict) or "projects" not in root:
        try:
            root = load_config()
        except Exception:
            root = {"projects": {}}
        cfg["_root"] = root
    root.setdefault("projects", {})
    entry = get_project_entry(root, project)

    def _save(key: str) -> None:
        key = key.strip().strip('"').strip("'")
        entry["api_key"] = key
        cfg["api_key"] = key
        try:
            save_config(root)
        except Exception:
            pass
        try:
            save_project_api_key(project, key)
        except Exception:
            pass

    def _is_valid_key(k: str) -> bool:
        if not k or not isinstance(k, str):
            return False
        k = k.strip()
        if not (k.startswith("nvapi-") or k.startswith("Bearer nvapi-")):
            return False
        return all(ord(c) < 128 and not c.isspace() for c in k)

    raw_key = (entry.get("api_key") or "").strip()
    key = raw_key if _is_valid_key(raw_key) else ""
    if not key:
        raw_key = (load_project_api_key(project) or "").strip()
        if _is_valid_key(raw_key):
            key = raw_key
            _save(key)
            return True

    if not key:
        env_key = (
            os.environ.get("NVIDIA_API_KEY")
            or os.environ.get("NGC_API_KEY")
            or os.environ.get("NIM_API_KEY")
            or ""
        ).strip()
        if _is_valid_key(env_key):
            key = env_key
            _save(key)
            UI.info("API key taken from environment and saved for this project.")
            return True

    if not key:
        # Fall back to root or any existing configured project key in config.json
        root_key = (root.get("api_key") or "").strip()
        if _is_valid_key(root_key):
            key = root_key
        else:
            for p_entry in (root.get("projects") or {}).values():
                if isinstance(p_entry, dict):
                    cand = (p_entry.get("api_key") or "").strip()
                    if _is_valid_key(cand):
                        key = cand
                        break
        if key:
            _save(key)
            UI.info("API key taken from existing config and saved for this project.")
            return True

    if key:
        cfg["api_key"] = key
        return True

    print()
    print("=" * 56)
    print("  NEON ARCHITECT — first-time setup")
    print("=" * 56)
    print(f"  Project: {project}")
    print(f"  Config:  {CONFIG_FILE}")
    print()
    print("  No API key saved for this folder yet.")
    print("  Get a free/paid key: https://build.nvidia.com")
    print("  Paste your NVIDIA NIM key below (starts with nvapi-).")
    print("  It will be remembered for this project only.")
    print("=" * 56)
    try:
        key = input("  NVIDIA API Key: ").strip().strip('"').strip("'")
    except (EOFError, KeyboardInterrupt):
        print()
        key = ""
    if not key or not _is_valid_key(key):
        UI.err("Valid API key (starts with nvapi-) is required to start.")
        return False
    _save(key)
    UI.ok("API key saved. Starting agent…")
    return True


def save_session(agent: "NeonArchitect", *, also_config: bool = True) -> bool:
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        session = {
            "state_version": STATE_VERSION,
            "timestamp": datetime.now().isoformat(),
            "project": str(agent.project_dir),
            "model": agent.model_key,
            "persona": agent.persona_key,
            "persona_history": list(agent.persona_history),
            "goal": agent.goal,
            "autopilot": bool(agent.autopilot),
            "autopilot_resume": bool(
                agent.autopilot
                or (agent.goal and agent.sdlc_phase_idx < len(SDLC_PHASES))
            ),
            "sdlc_phase_idx": agent.sdlc_phase_idx,
            "sdlc_completed": sorted(agent.sdlc_completed),
            "autopilot_rounds": agent.autopilot_rounds,
            "round": agent.round,
            "messages": agent.ctx.export(),
            "compaction_summary": agent.ctx.compaction_summary,
            "meter": {"prompt": agent.meter.prompt, "completion": agent.meter.completion},
            "todos": dict(TodoTool._todos),
            "todo_counter": TodoTool._counter,
            "thinking_effort": agent.config.get("thinking_effort"),
        }
        sf = _project_session_path(agent.project_dir)
        tmp = sf.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
        tmp.replace(sf)

        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as hf:
                hf.write(json.dumps({
                    "ts": session["timestamp"],
                    "project": session["project"],
                    "model": session["model"],
                    "goal": session["goal"],
                    "phase": session["sdlc_phase_idx"],
                    "tokens": session["meter"]["prompt"] + session["meter"]["completion"],
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

        if also_config:
            root = agent.config.get("_root") or load_config()
            fields = {
                "default_model": agent.model_key,
                "default_persona": agent.persona_key,
                "last_sdlc_phase_idx": agent.sdlc_phase_idx,
                "last_sdlc_completed": sorted(agent.sdlc_completed),
            }
            if agent.goal is not None:
                fields["last_goal"] = agent.goal
            save_project_settings(root, agent.project_dir, **fields)
            agent.config["_root"] = root
        return True
    except Exception as e:
        try:
            UI.warn(f"Session save failed: {e}")
        except Exception:
            pass
        return False


def load_session(project_dir: Path) -> Optional[Dict[str, Any]]:
    data = _safe_json_load(_project_session_path(project_dir))
    if not data:
        return None
    if int(data.get("state_version", 0)) > STATE_VERSION + 5:
        return None
    return data


def apply_session(agent: "NeonArchitect", data: Dict[str, Any]) -> None:
    if not data or not isinstance(data, dict):
        return

    def _safe_get(d: Dict[str, Any], key: str, default: Any = None) -> Any:
        try:
            return d.get(key, default)
        except Exception:
            return default

    try:
        mk = _safe_get(data, "model") or agent.config.get("default_model")
        if mk and mk in MODELS:
            agent.model_key = mk
            agent.model_cfg = MODELS[mk]
            agent.config["default_model"] = mk
    except Exception as e:
        UI.warn(f"Session restore (model): {e}")

    try:
        pk = _safe_get(data, "persona") or agent.config.get("default_persona", "adaptive")
        if pk in PERSONAS:
            agent.persona_key = pk
            agent.config["default_persona"] = pk
        for p in _safe_get(data, "persona_history") or []:
            if p in PERSONAS:
                agent.persona_history.append(p)
    except Exception as e:
        UI.warn(f"Session restore (persona): {e}")

    try:
        agent.goal = _safe_get(data, "goal") or agent.goal
        loaded_idx = int(_safe_get(data, "sdlc_phase_idx") or 0)
        # MONOTONIC PHASE LOCK SAFEGUARD: Ensure loading session data never demotes a higher reached phase
        agent.sdlc_phase_idx = max(getattr(agent, "sdlc_phase_idx", 0), loaded_idx)
        loaded_completed = set(_safe_get(data, "sdlc_completed") or [])
        agent.sdlc_completed.update(loaded_completed)
        agent.autopilot_rounds = int(_safe_get(data, "autopilot_rounds") or 0)
        agent.autopilot = False 
        agent._session_wants_resume = bool(
            _safe_get(data, "autopilot") or _safe_get(data, "autopilot_resume")
        )
        agent.round = int(_safe_get(data, "round") or 0)
    except Exception as e:
        UI.warn(f"Session restore (goal/sdlc): {e}")

    agent._phase_rounds = 0
    agent._phase_tool_counts = {}
    agent._phase_tool_success_counts = {}
    agent._phase_chars_written = 0
    agent._phase_bash_ok = False

    try:
        meter = _safe_get(data, "meter") or {}
        agent.meter.prompt = int(_safe_get(meter, "prompt") or 0)
        agent.meter.completion = int(_safe_get(meter, "completion") or 0)
    except Exception as e:
        UI.warn(f"Session restore (meter): {e}")

    try:
        todos = _safe_get(data, "todos")
        if isinstance(todos, dict):
            cleaned: Dict[str, Dict[str, Any]] = {}
            for k, v in todos.items():
                if not isinstance(v, dict):
                    continue
                cleaned[str(k)] = {
                    "content": str(v.get("content") or v.get("text") or v.get("task") or ""),
                    "status": str(v.get("status") or v.get("state") or "pending"),
                }
            TodoTool._todos = cleaned
            TodoTool._counter = int(_safe_get(data, "todo_counter") or len(cleaned))
        elif isinstance(todos, list):
            cleaned = {}
            for i, item in enumerate(todos):
                if isinstance(item, dict):
                    tid = str(item.get("id") or f"T{i+1:03d}")
                    cleaned[tid] = {
                        "content": str(item.get("content") or item.get("text") or ""),
                        "status": str(item.get("status") or item.get("state") or "pending"),
                    }
            TodoTool._todos = cleaned
            TodoTool._counter = int(_safe_get(data, "todo_counter") or len(cleaned))
    except Exception as e:
        UI.warn(f"Session restore (todos): {e}")
        TodoTool._todos = {}
        TodoTool._counter = 0

    try:
        msgs = _safe_get(data, "messages") or []
        restored = []
        for m in msgs:
            if isinstance(m, dict):
                role = m.get("role")
                if role == "system":
                    continue
                restored.append(m)
        agent.ctx.messages = sanitize_messages_for_api(restored)
        agent.ctx.compaction_summary = _safe_get(data, "compaction_summary")
    except Exception as e:
        UI.warn(f"Session restore (messages): {e}")
        agent.ctx.messages = []
        agent.ctx.compaction_summary = None

    agent._rebuild_system()


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION: TEXTUAL ALTERNATE-SCREEN TUI  (Crush / Hermes style full-screen)
# ═══════════════════════════════════════════════════════════════════════════════

try:
    from textual.app import App, ComposeResult
    from textual.containers import Container, Horizontal, Vertical
    from textual.widgets import Header, Footer, Input, RichLog, Static
    from textual.binding import Binding
    from textual import work
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


class _TuiLogSink:
    """Redirect UI prints into the Textual RichLog (thread-safe via call_from_thread)."""

    def __init__(self, app: "NeonTUI"):
        self.app = app

    def write(self, msg: str) -> None:
        if not msg:
            return
        try:
            self.app.call_from_thread(self.app.write_log, msg.rstrip("\n"))
        except Exception:
            pass

    def flush(self) -> None:
        pass


if HAS_TEXTUAL:

    class NeonTUI(App):
        """Full-screen alternate-buffer TUI for Neon Architect."""

        CSS = """
        Screen {
            background: #0b1220;
        }
        #head {
            dock: top;
            height: 3;
            background: #0f172a;
            color: #22d3ee;
            padding: 0 2;
            border: solid #22d3ee;
        }
        #log {
            background: #0b1220;
            color: #e2e8f0;
            border: solid #1e293b;
            padding: 0 1;
        }
        #status {
            dock: bottom;
            height: 1;
            background: #0f172a;
            color: #94a3b8;
            padding: 0 2;
        }
        #input {
            dock: bottom;
            margin-bottom: 1;
            background: #0f172a;
            border: solid #22d3ee;
            color: #f8fafc;
        }
        """

        BINDINGS = [
            Binding("ctrl+c", "quit", "Quit", show=True),
            Binding("ctrl+q", "quit", "Quit", show=False),
            Binding("escape", "quit", "Quit", show=False),
        ]

        def __init__(self, agent: "NeonArchitect"):
            super().__init__()
            self.agent = agent
            self._busy = False

        def compose(self) -> ComposeResult:
            model = self.agent.model_cfg.get("name", "?")
            proj = str(self.agent.project_dir)
            yield Static(
                f" ◆  {APP_NAME}  v{APP_VERSION}   ·   {model}   ·   {proj}",
                id="head",
            )
            yield RichLog(id="log", highlight=True, markup=True, wrap=True, auto_scroll=True)
            yield Input(placeholder="Type a request · /help · /goal · /autopilot · Enter to continue…", id="input")
            yield Static(" ready  ·  ctrl+c to quit", id="status")

        def on_mount(self) -> None:
            log = self.query_one("#log", RichLog)
            log.write(f"[bold cyan]{APP_NAME}[/] full-screen TUI  ·  model [white]{self.agent.model_cfg.get('name')}[/]")
            log.write("[dim]Panels · streaming · tools render in this log. Type below and press Enter.[/]")
            if self.agent.goal:
                log.write(f"[magenta]Goal:[/] {self.agent.goal}")
            self.query_one("#input", Input).focus()

        def write_log(self, msg: str) -> None:
            try:
                self.query_one("#log", RichLog).write(msg)
            except Exception:
                pass

        def set_status(self, text: str) -> None:
            try:
                self.query_one("#status", Static).update(f" {text}")
            except Exception:
                pass

        def on_input_submitted(self, event: Input.Submitted) -> None:
            text = (event.value or "").strip()
            event.input.value = ""
            if not text:
                text = "Please proceed with the current task."
            if text in ("/exit", "/quit"):
                self.exit()
                return
            if self._busy:
                self.write_log("[yellow]busy — wait for the current turn to finish[/]")
                return
            # Set _busy here, synchronously, on the main/event thread —
            # not inside run_agent_turn. @work(thread=True) schedules the
            # worker and returns immediately without waiting for the
            # thread body to start, so if the flag were set on the worker
            # thread instead, two Enter presses close enough together
            # could both observe self._busy == False above and both spawn
            # a worker, racing on the global sys.stdout/stderr swap below
            # and on concurrent self.agent.run_turn() calls (which mutates
            # shared agent/context state and isn't safe to run twice at
            # once). Setting it here closes that window.
            self._busy = True
            self.run_agent_turn(text)

        @work(thread=True)
        def run_agent_turn(self, user_text: str) -> None:
            self.call_from_thread(self.set_status, "thinking…")
            self.call_from_thread(self.write_log, f"[bold cyan]you[/]  {user_text}")

            # Temporarily tee stdout into the log so stream chunks appear full-width
            sink = _TuiLogSink(self)
            old_out, old_err = sys.stdout, sys.stderr

            class _Tee:
                def __init__(self, primary, secondary):
                    self.primary = primary
                    self.secondary = secondary
                def write(self, data):
                    try:
                        self.primary.write(data)
                    except Exception:
                        pass
                    if data and data != "\n":
                        try:
                            self.secondary.write(data)
                        except Exception:
                            pass
                def flush(self):
                    try:
                        self.primary.flush()
                    except Exception:
                        pass

            # In alternate screen, don't write to real stdout — only log
            class _LogOnly:
                def __init__(self, s):
                    self.s = s
                def write(self, data):
                    if data:
                        self.s.write(data)
                def flush(self):
                    pass

            sys.stdout = _LogOnly(sink)  # type: ignore
            sys.stderr = _LogOnly(sink)  # type: ignore
            try:
                self.agent.run_turn(user_text)
            except Exception as e:
                self.call_from_thread(self.write_log, f"[red]error:[/] {e}")
            finally:
                sys.stdout, sys.stderr = old_out, old_err
                self._busy = False
                self.call_from_thread(self.set_status, "ready  ·  ctrl+c to quit")


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    parser.add_argument("--project", "-p", default=None,
                        help="Project directory (default: current working directory)")
    parser.add_argument("--model", "-m", default=None, help="Model key (glm-5.2, glm-5.2-fp8, minimax-m3, laguna-xs-2.1)")
    parser.add_argument("--fresh", action="store_true",
                        help="Start a clean session (do not restore prior goal/messages/todos)")
    parser.add_argument("--tui", action="store_true",
                        help="Launch full-screen alternate-buffer TUI (Textual / Crush-style)")
    parser.add_argument("--full-access", action="store_true",
                        help="Unrestricted mode: local network browse allowed (destructive git ops still blocked in run tool)")
    parser.add_argument("--allow-outside", action="store_true",
                        help="Allow file tools to touch paths outside the project directory")
    parser.add_argument("--allow-local-net", action="store_true",
                        help="Allow browse_page to localhost / .local hosts")
    args = parser.parse_args()

    root_cfg = load_config()
    if args.project:
        project = Path(args.project).resolve()
    else:
        project = Path.cwd().resolve()

    cfg = apply_project_to_runtime(root_cfg, project)
    cfg["_root"] = root_cfg
    if args.model:
        cfg["default_model"] = args.model
        save_project_settings(root_cfg, project, default_model=args.model)
    if args.fresh:
        cfg["_fresh"] = True
    if args.full_access:
        cfg["full_access"] = True
        cfg["allow_path_outside_project"] = False
        cfg["allow_local_network"] = True
        cfg["bash_confirm_destructive"] = False
    if args.allow_outside:
        UI.warn("--allow-outside ignored: file access is limited to the project folder only.")
        cfg["allow_path_outside_project"] = False
    if args.allow_local_net:
        cfg["allow_local_network"] = True

    if not ensure_api_key(cfg, project_dir=project):
        sys.exit(1)
    cfg["api_key"] = (get_project_entry(root_cfg, project).get("api_key") or cfg.get("api_key") or "").strip()
    if not cfg["api_key"]:
        UI.err("No API key after ensure_api_key — set NVIDIA_API_KEY or use /apikey after fixing config.")
        sys.exit(1)

    os.system("cls" if os.name == "nt" else "clear")
    _banner_model = MODELS.get(cfg.get("default_model"), MODELS["glm-5.2"])["name"]
    UI.neon_banner(_banner_model)
    if HAS_RICH:
        console.print(UI.header(
            MODELS.get(cfg["default_model"], MODELS["glm-5.2"])["name"],
            str(project),
        ))
    else:
        print(f"{APP_NAME} v{APP_VERSION}")
        print(f"Model: {cfg.get('default_model')} | Project: {project}")

    try:
        agent = NeonArchitect(cfg)
    except Exception as e:
        UI.err(f"Failed to initialize: {e}")
        sys.exit(1)

    atexit.register(lambda: save_session(agent, also_config=True))

    try:
        root = agent.config.get("_root") or load_config()
        save_project_settings(
            root,
            project,
            default_model=agent.model_key,
            default_persona=agent.persona_key,
        )
        agent.config["_root"] = root
        agent.config["project_dir"] = str(project)
        agent.config["version"] = APP_VERSION
    except Exception as e:
        UI.warn(f"Could not update config: {e}")

    # Full-screen alternate-buffer TUI (Textual)
    if getattr(args, "tui", False):
        if not HAS_TEXTUAL:
            UI.err("Textual is not installed. Run:  pip install textual")
            sys.exit(1)
        app = NeonTUI(agent)
        app.run()
        ok = save_session(agent, also_config=True)
        if ok:
            print(f"Session saved → {_project_session_path(agent.project_dir)}")
        print(f"Total usage: {agent.meter.report()}")
        print("Goodbye.")
        return

    if not cfg.get("_fresh"):
        try:
            if agent.offer_resume_autopilot():
                try:
                    agent.run_turn(agent._autopilot_prompt())
                except KeyboardInterrupt:
                    print()
                    UI.warn("Interrupted — autopilot still active; empty Enter or /stop.")
                except Exception as e:
                    UI.err(f"Resume turn error: {e}")
        except Exception as e:
            UI.warn(f"Resume prompt skipped: {e}")

    UI.separator("Ready")
    _k = (agent.config.get("api_key") or "")
    _nproj = len((agent.config.get("_root") or load_config()).get("projects") or {})
    _masked = ("***" + _k[-4:]) if len(_k) > 8 else ("***" if _k else None)
    if HAS_RICH:
        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="cc_muted", justify="right", min_width=10)
        summary.add_column(style="cc_body")
        summary.add_row("API key", (f"{_masked}  ({_nproj} project{'s' if _nproj != 1 else ''} configured)"
                                      if _masked else "[cc_warn]not set — use /apikey[/cc_warn]"))
        summary.add_row("Timeouts", f"first-token {agent.config.get('first_token_timeout', 60):.0f}s"
                                     f"  ·  retries {agent.config.get('retry_max', 2)}"
                                     f"  ·  thinking {agent.config.get('thinking_effort', 'off')}")
        summary.add_row("Personas", "architect · designer · engineer · tester · debugger · reviewer · devops")
        console.print(Padding(summary, (0, 2)))
        console.print()
        tip = Text()
        tip.append("  ", style="cc_muted")
        tip.append("Type a request", style="cc_body")
        tip.append("  ·  press ", style="cc_muted")
        tip.append("Enter", style="cc_fg")
        tip.append(" to continue  ·  ", style="cc_muted")
        tip.append("/help", style="cc_primary_bold")
        tip.append(" commands  ·  ", style="cc_muted")
        tip.append("/goal", style="cc_primary_bold")
        tip.append(" + ", style="cc_muted")
        tip.append("/autopilot", style="cc_primary_bold")
        tip.append(" for full SDLC", style="cc_muted")
        console.print(tip)
        console.print()
    else:
        if _masked:
            print(f"  API key: {_masked}  ({_nproj} project(s) configured)")
        else:
            print("  No API key for this project — set with /apikey")
        print(f"  Timeouts: first-token {agent.config.get('first_token_timeout', 60):.0f}s, "
              f"retries {agent.config.get('retry_max', 2)}, thinking {agent.config.get('thinking_effort', 'off')}")
        print("  Type a request, or press Enter to continue. /help for commands.")

    while True:
        try:
            if HAS_RICH:
                raw = console.input("[cc_prompt]  ❯  [/cc_prompt]").strip()
            else:
                raw = input("  > ").strip()
        except KeyboardInterrupt:
            print()
            break
        except EOFError:
            # stdin closed (e.g. launched detached with no TTY).
            # If autopilot is active, keep running — don't need user input.
            if getattr(agent, "autopilot", False):
                import time as _t
                _t.sleep(2)
                raw = ""
            else:
                print()
                break

        if not raw:
            raw = "Please proceed with the current task."

        if raw == "/exit":
            break
        if raw == "/clear":
            agent.ctx.clear()
            agent.round = 0
            agent.nudges = 0
            agent._rebuild_system()
            agent.persist(force=True)
            UI.ok("Context cleared (goal/model/todos kept). State saved.")
            continue
        if raw == "/help" or raw == "/?":
            if HAS_RICH:
                groups = [
                    ("Session", [
                        ("/new", "clean session (keeps API key & model)"),
                        ("/reset", "wipe goal, SDLC, todos, conversation"),
                        ("/clear", "wipe conversation only"),
                        ("/exit", "quit"),
                    ]),
                    ("Autopilot", [
                        ("/goal <desc>", "set the objective"),
                        ("/autopilot", "run full SDLC until goal reached"),
                        ("/generate [stack]", "scaffold + AI-generate a full-stack app"),
                        ("/status", "show autopilot progress"),
                        ("/stop", "halt autopilot"),
                        ("/todo", "show task tracker"),
                    ]),
                    ("Model & context", [
                        ("/model <name>", "switch model"),
                        ("/persona <name>", "switch persona"),
                        ("/compact", "force context compaction"),
                        ("/cost", "token usage stats"),
                    ]),
                    ("Project", [
                        ("/files", "show project file tree"),
                        ("/apikey <key>", "set API key for this project"),
                        ("/permission", "review file/bash permissions"),
                        ("/worktree", "git worktree status"),
                    ]),
                ]
                for title, rows in groups:
                    console.print(Text(f"  ◆  {title}", style="cc_primary_bold"))
                    t = Table.grid(padding=(0, 2))
                    t.add_column(style="cc_primary", min_width=18)
                    t.add_column(style="cc_body")
                    for cmd, desc in rows:
                        t.add_row(f"     {cmd}", desc)
                    console.print(t)
                    console.print()
                console.print(Text("  Empty Enter continues the last task.", style="cc_subtle"))
                console.print()
            else:
                print("Commands: /new /reset /clear /exit /goal /autopilot /status /stop /todo "
                      "/model /persona /compact /cost /files /apikey /permission /worktree")
            continue
        if raw == "/files":
            agent.cmd_files()
            continue
        if raw == "/cost":
            agent.cmd_cost()
            continue
        if raw == "/compact":
            agent.cmd_compact()
            continue
        if raw.startswith("/model "):
            agent.cmd_model(raw.split(None, 1)[1].strip())
            continue
        if raw == "/todo":
            agent.cmd_todo()
            continue
        if raw.startswith("/goal "):
            agent.cmd_goal(raw.split(None, 1)[1].strip())
            continue
        if raw == "/goal":
            agent.cmd_goal()
            continue
        if raw.startswith("/persona "):
            agent.cmd_persona(raw.split(None, 1)[1].strip())
            continue
        if raw == "/persona":
            agent.cmd_persona()
            continue
        if raw == "/autopilot" or raw.startswith("/autopilot "):
            agent.cmd_autopilot(restart="restart" in raw.lower())
            continue
        if raw == "/status":
            agent._autopilot_status()
            continue
        if raw == "/stop":
            agent._stop_autopilot()
            continue
        if raw == "/permission" or raw.startswith("/permission "):
            arg = raw.split(None, 1)[1] if " " in raw else "status"
            agent.cmd_permission(arg)
            continue
        if raw == "/worktree":
            agent.cmd_worktree(auto=False)
            continue
        if raw.startswith("/generate"):
            _cmd_generate(raw, agent)
            continue
        if raw == "/full-access" or raw == "/fullaccess":
            agent.cmd_permission("full")
            continue
        if raw.startswith("/apikey"):
            parts = raw.split(None, 1)
            root = agent.config.get("_root") or load_config()
            entry = get_project_entry(root, agent.project_dir)
            if len(parts) < 2 or not parts[1].strip():
                UI.info(f"Project: {agent.project_dir}")
                UI.info(f"Config:  {CONFIG_FILE}")
                k = (entry.get("api_key") or agent.config.get("api_key") or "")
                if k:
                    UI.info(f"Current key: {'***' + k[-4:] if len(k) > 8 else '***'}")
                else:
                    UI.warn("No API key set for this project.")
                UI.info(f"Projects in config: {len(root.get('projects') or {})}")
                for pp in sorted((root.get("projects") or {}).keys()):
                    has = "key=yes" if (root["projects"][pp] or {}).get("api_key") else "key=no"
                    mark = "→" if pp == str(agent.project_dir.resolve()) else " "
                    UI.info(f"  {mark} {pp} ({has})")
                UI.info("Usage: /apikey <your-nvidia-key>")
                continue
            new_key = parts[1].strip().strip('"').strip("'").replace("\n","").replace("\r","")
            save_project_settings(root, agent.project_dir, api_key=new_key)
            agent.config["api_key"] = new_key
            agent.config["_root"] = root
            try:
                save_project_api_key(agent.project_dir, new_key)
            except Exception:
                pass
            try:
                agent.pool = build_pool(agent.config)
                UI.ok(f"API key saved for this project → {CONFIG_FILE}")
                UI.info(f"Providers: {', '.join(p.name for p in agent.pool.providers)}")
            except Exception as e:
                UI.err(f"Key saved but pool rebuild failed: {e}")
            continue
        if raw == "/new":
            agent.ctx.clear()
            agent.goal = None
            agent.autopilot = False
            agent.sdlc_phase_idx = 0
            agent.sdlc_completed.clear()
            agent.autopilot_rounds = 0
            agent.round = 0
            agent.nudges = 0
            agent.meter.prompt = 0
            agent.meter.completion = 0
            agent._phase_rounds = 0
            agent._phase_tool_counts = {}
            agent._phase_tool_success_counts = {}
            agent._phase_chars_written = 0
            agent._phase_bash_ok = False
            agent.current_turn_tool_calls = False
            TodoTool._todos.clear()
            TodoTool._counter = 0
            agent.config.pop("last_goal", None)
            agent.config.pop("last_sdlc_phase_idx", None)
            agent.config.pop("last_sdlc_completed", None)
            agent._rebuild_system()
            agent.persist(force=True)
            UI.ok("New session for this project. Goal/todos/history cleared. API key & model kept.")
            try:
                agent.project_state["autopilot_active"] = False
                agent.project_state["autopilot_resume"] = False
                agent._session_wants_resume = False
                agent._sync_project_state()
            except Exception:
                pass
            UI.info(f"Project: {agent.project_dir}")
            continue
        if raw == "/reset":
            agent.ctx.clear()
            agent.goal = None
            agent.autopilot = False
            agent.sdlc_phase_idx = 0
            agent.sdlc_completed.clear()
            agent.autopilot_rounds = 0
            agent.round = 0
            agent.nudges = 0
            agent.meter.prompt = 0
            agent.meter.completion = 0
            agent._phase_rounds = 0
            agent._phase_tool_counts = {}
            agent._phase_tool_success_counts = {}
            agent._phase_chars_written = 0
            agent._phase_bash_ok = False
            TodoTool._todos.clear()
            TodoTool._counter = 0
            agent.config.pop("last_goal", None)
            agent.config.pop("last_sdlc_phase_idx", None)
            agent.config.pop("last_sdlc_completed", None)
            agent._rebuild_system()
            agent.persist(force=True)
            UI.ok("Full reset: goal, SDLC, todos, conversation wiped. Model/persona kept.")
            continue
        if raw.startswith("/"):
            UI.warn(f"Unknown command: {raw}. Type /help to see available commands.")
            continue

        try:
            agent.run_turn(raw)
        except KeyboardInterrupt:
            print("\n")
            UI.warn("Interrupted by user.")
            continue
        except Exception as e:
            UI.err(f"Error: {e}")
            continue

    ok = save_session(agent, also_config=True)
    UI.separator()
    if ok:
        UI.ok(f"Session saved → {_project_session_path(agent.project_dir)}")
    UI.ok(f"Total usage: {agent.meter.report()}")
    print("Goodbye.")


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 10: GENERATION LAYER
#  Transforms the runtime engine into a true Emergent.sh clone.
#  Adds: STACK_TEMPLATES · SpecializedAgent · GenerationOrchestrator
#        LivePreviewManager · generate_app Tool · /generate command
# ═══════════════════════════════════════════════════════════════════════════════

import socket as _socket
import textwrap as _textwrap

# ── Utility: find a free TCP port ────────────────────────────────────────────

def _find_free_port(start: int = 3100, end: int = 3999) -> int:
    """Return the lowest free TCP port in [start, end]."""
    for port in range(start, end):
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port in range {start}–{end}")


# ── Section A: Stack Templates ────────────────────────────────────────────────
# Inline project scaffolds keyed by stack name.
# Rendered with str.format_map(ctx) where ctx has: project_name, year, description

_T = {  # template store
    "fastapi-react": {
        # ── Backend ──────────────────────────────────────────────────────────
        "backend/requirements.txt": """\
fastapi>=0.111.0
uvicorn[standard]>=0.30.0
sqlalchemy>=2.0.0
alembic>=1.13.0
python-jose[cryptography]>=3.3.0
bcrypt>=4.1.0
python-multipart>=0.0.9
pydantic-settings>=2.2.0
httpx>=0.27.0
""",
        "backend/main.py": """\
\"\"\"
{project_name} — FastAPI backend
Generated by Neon Architect / NVIDIA NIM
\"\"\"
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.routers import auth, projects, health

app = FastAPI(title="{project_name}", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3100"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(projects.router, prefix="/api/projects", tags=["projects"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
""",
        "backend/__init__.py": "",
        "backend/routers/__init__.py": "",
        "backend/routers/health.py": """\
from fastapi import APIRouter
router = APIRouter()

@router.get("/health")
def health():
    return {{"status": "ok", "service": "{project_name}"}}
""",
        "backend/routers/auth.py": """\
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import bcrypt
from jose import jwt, JWTError
from datetime import datetime, timedelta
import os

router = APIRouter()
SECRET = os.getenv("JWT_SECRET", "change-me-in-production")
ALGORITHM = "HS256"
security = HTTPBearer(auto_error=False)
# bcrypt has a hard 72-byte input limit; truncate defensively.
_BCRYPT_MAX_BYTES = 72

def _hash_password(password: str) -> str:
    truncated = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(truncated, bcrypt.gensalt()).decode("utf-8")

def _verify_password(password: str, hashed: str) -> bool:
    truncated = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.checkpw(truncated, hashed.encode("utf-8"))

# In-memory user store (replace with DB in production)
_users: dict = {{}}

class RegisterIn(BaseModel):
    email: str
    password: str

class LoginIn(BaseModel):
    email: str
    password: str

def _make_token(email: str) -> str:
    exp = datetime.utcnow() + timedelta(hours=24)
    return jwt.encode({{"sub": email, "exp": exp}}, SECRET, algorithm=ALGORITHM)

def current_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, SECRET, algorithms=[ALGORITHM])
        return payload["sub"]
    except JWTError:
        raise HTTPException(401, "Invalid token")

@router.post("/register")
def register(body: RegisterIn):
    if body.email in _users:
        raise HTTPException(400, "Email already registered")
    _users[body.email] = _hash_password(body.password)
    return {{"token": _make_token(body.email)}}

@router.post("/login")
def login(body: LoginIn):
    hashed = _users.get(body.email)
    if not hashed or not _verify_password(body.password, hashed):
        raise HTTPException(401, "Invalid credentials")
    return {{"token": _make_token(body.email)}}

@router.get("/me")
def me(user: str = Depends(current_user)):
    return {{"email": user}}
""",
        "backend/routers/projects.py": """\
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from backend.routers.auth import current_user
from typing import List, Optional
import uuid, time

router = APIRouter()
_store: dict = {{}}  # project_id -> project dict

class ProjectIn(BaseModel):
    name: str
    description: Optional[str] = ""
    stack: Optional[str] = "fastapi-react"

@router.get("/", response_model=List[dict])
def list_projects(user: str = Depends(current_user)):
    return [p for p in _store.values() if p["owner"] == user]

@router.post("/", status_code=201)
def create_project(body: ProjectIn, user: str = Depends(current_user)):
    pid = str(uuid.uuid4())
    proj = {{"id": pid, "owner": user, "name": body.name,
             "description": body.description, "stack": body.stack,
             "status": "created", "created_at": time.time()}}
    _store[pid] = proj
    return proj

@router.get("/{{project_id}}")
def get_project(project_id: str, user: str = Depends(current_user)):
    p = _store.get(project_id)
    if not p or p["owner"] != user:
        raise HTTPException(404, "Not found")
    return p

@router.delete("/{{project_id}}", status_code=204)
def delete_project(project_id: str, user: str = Depends(current_user)):
    p = _store.get(project_id)
    if not p or p["owner"] != user:
        raise HTTPException(404, "Not found")
    del _store[project_id]
""",
        # ── Frontend ─────────────────────────────────────────────────────────
        "frontend/package.json": """\
{{
  "name": "{project_name_slug}",
  "version": "0.1.0",
  "private": true,
  "scripts": {{
    "dev": "vite --port 5173",
    "build": "tsc && vite build",
    "preview": "vite preview"
  }},
  "dependencies": {{
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-router-dom": "^6.23.0",
    "axios": "^1.7.2",
    "zustand": "^4.5.2",
    "@tanstack/react-query": "^5.40.0"
  }},
  "devDependencies": {{
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.0",
    "typescript": "^5.4.5",
    "vite": "^5.3.1"
  }}
}}
""",
        "frontend/index.html": """\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{project_name}</title>
    <style>
      *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
      body {{ font-family: 'Inter', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }}
    </style>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
""",
        "frontend/vite.config.ts": """\
import {{ defineConfig }} from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({{
  plugins: [react()],
  server: {{
    port: 5173,
    proxy: {{ '/api': 'http://localhost:8000' }}
  }}
}})
""",
        "frontend/tsconfig.json": """\
{{
  "compilerOptions": {{
    "target": "ES2020",
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true
  }},
  "include": ["src"]
}}
""",
        "frontend/src/main.tsx": """\
import React from 'react'
import ReactDOM from 'react-dom/client'
import {{ BrowserRouter }} from 'react-router-dom'
import {{ QueryClient, QueryClientProvider }} from '@tanstack/react-query'
import App from './App'

const qc = new QueryClient()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={{qc}}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
)
""",
        "frontend/src/App.tsx": """\
import {{ Routes, Route, Navigate }} from 'react-router-dom'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import {{ useAuthStore }} from './stores/authStore'

export default function App() {{
  const token = useAuthStore(s => s.token)
  return (
    <Routes>
      <Route path="/login" element={{<LoginPage />}} />
      <Route path="/dashboard" element={{token ? <DashboardPage /> : <Navigate to="/login" />}} />
      <Route path="*" element={{<Navigate to={{token ? '/dashboard' : '/login'}} />}} />
    </Routes>
  )
}}
""",
        "frontend/src/stores/authStore.ts": """\
import {{ create }} from 'zustand'
import {{ persist }} from 'zustand/middleware'
import axios from 'axios'

interface AuthState {{
  token: string | null
  email: string | null
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  logout: () => void
}}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({{
      token: null,
      email: null,
      login: async (email, password) => {{
        const {{ data }} = await axios.post('/api/auth/login', {{ email, password }})
        set({{ token: data.token, email }})
        axios.defaults.headers.common['Authorization'] = `Bearer ${{data.token}}`
      }},
      register: async (email, password) => {{
        const {{ data }} = await axios.post('/api/auth/register', {{ email, password }})
        set({{ token: data.token, email }})
        axios.defaults.headers.common['Authorization'] = `Bearer ${{data.token}}`
      }},
      logout: () => {{
        set({{ token: null, email: null }})
        delete axios.defaults.headers.common['Authorization']
      }}
    }}),
    {{ name: '{project_name_slug}-auth' }}
  )
)
""",
        "frontend/src/pages/LoginPage.tsx": """\
import {{ useState }} from 'react'
import {{ useNavigate }} from 'react-router-dom'
import {{ useAuthStore }} from '../stores/authStore'

export default function LoginPage() {{
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [isRegister, setIsRegister] = useState(false)
  const [error, setError] = useState('')
  const {{ login, register }} = useAuthStore()
  const nav = useNavigate()

  const submit = async (e: React.FormEvent) => {{
    e.preventDefault()
    setError('')
    try {{
      if (isRegister) await register(email, password)
      else await login(email, password)
      nav('/dashboard')
    }} catch (err: any) {{
      setError(err?.response?.data?.detail || 'Authentication failed')
    }}
  }}

  return (
    <div style={{{{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#0f172a' }}}}>
      <div style={{{{ width: 380, background: '#1e293b', borderRadius: 16, padding: 40, boxShadow: '0 25px 50px #0008' }}}}>
        <h1 style={{{{ fontSize: 28, fontWeight: 700, color: '#38bdf8', marginBottom: 8 }}}}>{project_name}</h1>
        <p style={{{{ color: '#94a3b8', marginBottom: 32 }}}}>{{isRegister ? 'Create your account' : 'Sign in to continue'}}</p>
        <form onSubmit={{submit}}>
          <input type="email" placeholder="Email" value={{email}} onChange={{e => setEmail(e.target.value)}}
            style={{{{ width: '100%', padding: '12px 16px', borderRadius: 8, border: '1px solid #334155', background: '#0f172a', color: '#e2e8f0', marginBottom: 12, fontSize: 14 }}}} />
          <input type="password" placeholder="Password" value={{password}} onChange={{e => setPassword(e.target.value)}}
            style={{{{ width: '100%', padding: '12px 16px', borderRadius: 8, border: '1px solid #334155', background: '#0f172a', color: '#e2e8f0', marginBottom: 16, fontSize: 14 }}}} />
          {{error && <p style={{{{ color: '#fb7185', marginBottom: 12, fontSize: 13 }}}}>{{error}}</p>}}
          <button type="submit" style={{{{ width: '100%', padding: '12px', borderRadius: 8, border: 'none', background: '#38bdf8', color: '#0f172a', fontWeight: 700, fontSize: 15, cursor: 'pointer' }}}}>
            {{isRegister ? 'Create Account' : 'Sign In'}}
          </button>
        </form>
        <p style={{{{ textAlign: 'center', marginTop: 20, color: '#64748b', fontSize: 13 }}}}>
          {{isRegister ? 'Already have an account? ' : "Don't have an account? "}}
          <span onClick={{() => setIsRegister(!isRegister)}} style={{{{ color: '#38bdf8', cursor: 'pointer' }}}}>
            {{isRegister ? 'Sign in' : 'Register'}}
          </span>
        </p>
      </div>
    </div>
  )
}}
""",
        "frontend/src/pages/DashboardPage.tsx": """\
import {{ useState, useEffect }} from 'react'
import axios from 'axios'
import {{ useAuthStore }} from '../stores/authStore'

interface Project {{ id: string; name: string; description: string; status: string; created_at: number }}

export default function DashboardPage() {{
  const [projects, setProjects] = useState<Project[]>([])
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const {{ email, logout }} = useAuthStore()

  const fetchProjects = async () => {{
    try {{ const {{ data }} = await axios.get('/api/projects/'); setProjects(data) }}
    catch {{ setProjects([]) }}
  }}

  useEffect(() => {{ fetchProjects() }}, [])

  const create = async (e: React.FormEvent) => {{
    e.preventDefault()
    if (!newName.trim()) return
    await axios.post('/api/projects/', {{ name: newName, description: newDesc }})
    setNewName(''); setNewDesc('')
    fetchProjects()
  }}

  return (
    <div style={{{{ minHeight: '100vh', background: '#0f172a', padding: '32px 24px' }}}}>
      <div style={{{{ maxWidth: 900, margin: '0 auto' }}}}>
        <div style={{{{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 40 }}}}>
          <h1 style={{{{ color: '#38bdf8', fontSize: 28, fontWeight: 700 }}}}>My Projects</h1>
          <div style={{{{ display: 'flex', alignItems: 'center', gap: 16 }}}}>
            <span style={{{{ color: '#94a3b8', fontSize: 14 }}}}>{{email}}</span>
            <button onClick={{logout}} style={{{{ padding: '8px 16px', borderRadius: 8, border: '1px solid #334155', background: 'transparent', color: '#94a3b8', cursor: 'pointer', fontSize: 13 }}}}>Sign out</button>
          </div>
        </div>

        <form onSubmit={{create}} style={{{{ display: 'flex', gap: 12, marginBottom: 40 }}}}>
          <input placeholder="Project name" value={{newName}} onChange={{e => setNewName(e.target.value)}}
            style={{{{ flex: 1, padding: '12px 16px', borderRadius: 8, border: '1px solid #334155', background: '#1e293b', color: '#e2e8f0', fontSize: 14 }}}} />
          <input placeholder="Description (optional)" value={{newDesc}} onChange={{e => setNewDesc(e.target.value)}}
            style={{{{ flex: 2, padding: '12px 16px', borderRadius: 8, border: '1px solid #334155', background: '#1e293b', color: '#e2e8f0', fontSize: 14 }}}} />
          <button type="submit" style={{{{ padding: '12px 24px', borderRadius: 8, border: 'none', background: '#38bdf8', color: '#0f172a', fontWeight: 700, cursor: 'pointer', fontSize: 14 }}}}>+ New</button>
        </form>

        <div style={{{{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 20 }}}}>
          {{projects.map(p => (
            <div key={{p.id}} style={{{{ background: '#1e293b', borderRadius: 12, padding: 24, border: '1px solid #334155' }}}}>
              <h3 style={{{{ color: '#e2e8f0', marginBottom: 8, fontWeight: 600 }}}}>{{p.name}}</h3>
              <p style={{{{ color: '#64748b', fontSize: 13, marginBottom: 16 }}}}>{{p.description || 'No description'}}</p>
              <span style={{{{ fontSize: 12, padding: '4px 10px', borderRadius: 99, background: '#0f172a', color: '#38bdf8' }}}}>{{p.status}}</span>
            </div>
          ))}}
          {{projects.length === 0 && (
            <div style={{{{ gridColumn: '1 / -1', textAlign: 'center', color: '#475569', padding: 60 }}}}>
              No projects yet. Create your first one above.
            </div>
          )}}
        </div>
      </div>
    </div>
  )
}}
""",
        # ── DevOps ────────────────────────────────────────────────────────────
        "docker-compose.yml": """\
version: "3.9"
services:
  backend:
    build:
      context: .
      dockerfile: Dockerfile.backend
    ports:
      - "8000:8000"
    environment:
      - JWT_SECRET=${{JWT_SECRET:-change-me}}
      - NIM_API_KEY=${{NIM_API_KEY}}
    volumes:
      - ./backend:/app/backend
    command: uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

  frontend:
    build:
      context: .
      dockerfile: Dockerfile.frontend
    ports:
      - "5173:5173"
    volumes:
      - ./frontend:/app
      - /app/node_modules
    command: npm run dev -- --host
    depends_on:
      - backend
""",
        "Dockerfile.backend": """\
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
""",
        "Dockerfile.frontend": """\
FROM node:20-alpine
WORKDIR /app
COPY frontend/package.json .
RUN npm install
COPY frontend/ .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host"]
""",
        ".env.example": """\
# Copy to .env and fill in values
JWT_SECRET=change-me-in-production
NIM_API_KEY=your-nvidia-api-key-here
DATABASE_URL=sqlite:///./app.db
""",
        "README.md": """\
# {project_name}

> {description}

Generated by **Neon Architect** using NVIDIA NIM.

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

- Frontend: http://localhost:5173
- Backend API: http://localhost:8000
- API docs: http://localhost:8000/docs

## Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18 + Vite + TypeScript |
| Backend | FastAPI + Uvicorn |
| Auth | JWT (jose + bcrypt) |
| State | Zustand |
| Data fetching | TanStack Query |

## Development (without Docker)

```bash
# Backend
cd backend && pip install -r requirements.txt
uvicorn backend.main:app --reload

# Frontend
cd frontend && npm install && npm run dev
```
""",
    },

    # ── Next.js + Postgres ─────────────────────────────────────────────────────
    "nextjs-postgres": {
        "package.json": """\
{{
  "name": "{project_name_slug}",
  "version": "0.1.0",
  "private": true,
  "scripts": {{
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "lint": "next lint"
  }},
  "dependencies": {{
    "next": "14.2.4",
    "react": "^18",
    "react-dom": "^18",
    "@prisma/client": "^5.14.0",
    "next-auth": "^4.24.7",
    "bcryptjs": "^2.4.3",
    "zod": "^3.23.8"
  }},
  "devDependencies": {{
    "@types/node": "^20",
    "@types/react": "^18",
    "@types/react-dom": "^18",
    "@types/bcryptjs": "^2.4.6",
    "prisma": "^5.14.0",
    "typescript": "^5"
  }}
}}
""",
        "prisma/schema.prisma": """\
generator client {{
  provider = "prisma-client-js"
}}

datasource db {{
  provider = "postgresql"
  url      = env("DATABASE_URL")
}}

model User {{
  id        String    @id @default(cuid())
  email     String    @unique
  password  String
  projects  Project[]
  createdAt DateTime  @default(now())
}}

model Project {{
  id          String   @id @default(cuid())
  name        String
  description String?
  status      String   @default("created")
  owner       User     @relation(fields: [ownerId], references: [id])
  ownerId     String
  createdAt   DateTime @default(now())
}}
""",
        "app/layout.tsx": """\
import type {{ Metadata }} from 'next'
export const metadata: Metadata = {{ title: '{project_name}', description: '{description}' }}
export default function RootLayout({{ children }}: {{ children: React.ReactNode }}) {{
  return <html lang="en"><body style={{{{fontFamily:'system-ui',background:'#0f172a',color:'#e2e8f0',minHeight:'100vh'}}}}>{{children}}</body></html>
}}
""",
        "app/page.tsx": """\
import Link from 'next/link'
export default function Home() {{
  return (
    <main style={{{{textAlign:'center',paddingTop:120}}}}>
      <h1 style={{{{fontSize:48,fontWeight:700,color:'#38bdf8',marginBottom:16}}}}>{project_name}</h1>
      <p style={{{{color:'#94a3b8',marginBottom:40}}}}>{description}</p>
      <Link href="/dashboard" style={{{{padding:'12px 32px',borderRadius:8,background:'#38bdf8',color:'#0f172a',fontWeight:700,textDecoration:'none'}}}}>
        Get Started
      </Link>
    </main>
  )
}}
""",
        "docker-compose.yml": """\
version: "3.9"
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: {project_name_slug}
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  web:
    build: .
    ports:
      - "3000:3000"
    environment:
      DATABASE_URL: postgresql://postgres:postgres@db:5432/{project_name_slug}
      NEXTAUTH_SECRET: ${{NEXTAUTH_SECRET:-change-me}}
      NEXTAUTH_URL: http://localhost:3000
    depends_on:
      - db
    command: sh -c "npx prisma migrate deploy && npm start"

volumes:
  pgdata:
""",
        "Dockerfile": """\
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
RUN npm run build

FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/.next .next
COPY --from=builder /app/node_modules node_modules
COPY --from=builder /app/package.json .
COPY --from=builder /app/prisma prisma
EXPOSE 3000
CMD ["npm", "start"]
""",
        ".env.example": """\
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/{project_name_slug}
NEXTAUTH_SECRET=change-me-in-production
NEXTAUTH_URL=http://localhost:3000
NIM_API_KEY=your-nvidia-api-key-here
""",
        "README.md": """\
# {project_name}

> {description}

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

- App: http://localhost:3000

## Stack

| Layer | Technology |
|-------|-----------|
| Framework | Next.js 14 (App Router) |
| Database | PostgreSQL via Prisma |
| Auth | NextAuth.js |
""",
    },

    # ── Flutter + FastAPI ──────────────────────────────────────────────────────
    "flutter-fastapi": {
        "pubspec.yaml": """\
name: {project_name_slug}
description: {description}
version: 1.0.0+1
environment:
  sdk: '>=3.3.0 <4.0.0'
dependencies:
  flutter:
    sdk: flutter
  http: ^1.2.1
  shared_preferences: ^2.2.3
  provider: ^6.1.2
flutter:
  uses-material-design: true
""",
        "lib/main.dart": """\
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'screens/login_screen.dart';
import 'screens/dashboard_screen.dart';
import 'stores/auth_store.dart';

void main() {{
  runApp(
    ChangeNotifierProvider(create: (_) => AuthStore(), child: const MyApp()),
  );
}}

class MyApp extends StatelessWidget {{
  const MyApp({{super.key}});
  @override
  Widget build(BuildContext context) {{
    final auth = context.watch<AuthStore>();
    return MaterialApp(
      title: '{project_name}',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorScheme: ColorScheme.fromSeed(seedColor: Colors.blue, brightness: Brightness.dark), useMaterial3: true),
      home: auth.token != null ? const DashboardScreen() : const LoginScreen(),
    );
  }}
}}
""",
        "lib/stores/auth_store.dart": """\
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'dart:convert';

class AuthStore extends ChangeNotifier {{
  String? token;
  String? email;
  static const _base = 'http://localhost:8000/api';

  AuthStore() {{ _load(); }}

  Future<void> _load() async {{
    final prefs = await SharedPreferences.getInstance();
    token = prefs.getString('token');
    email = prefs.getString('email');
    notifyListeners();
  }}

  Future<void> login(String em, String pw) async {{
    final res = await http.post(Uri.parse('$_base/auth/login'),
        headers: {{'Content-Type': 'application/json'}},
        body: jsonEncode({{'email': em, 'password': pw}}));
    if (res.statusCode != 200) throw Exception('Login failed');
    final data = jsonDecode(res.body);
    token = data['token']; email = em;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('token', token!); await prefs.setString('email', email!);
    notifyListeners();
  }}

  Future<void> register(String em, String pw) async {{
    final res = await http.post(Uri.parse('$_base/auth/register'),
        headers: {{'Content-Type': 'application/json'}},
        body: jsonEncode({{'email': em, 'password': pw}}));
    if (res.statusCode != 200) throw Exception('Registration failed');
    final data = jsonDecode(res.body);
    token = data['token']; email = em;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('token', token!); await prefs.setString('email', email!);
    notifyListeners();
  }}

  Future<void> logout() async {{
    token = null; email = null;
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('token'); await prefs.remove('email');
    notifyListeners();
  }}
}}
""",
        "lib/screens/login_screen.dart": """\
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../stores/auth_store.dart';

class LoginScreen extends StatefulWidget {{
  const LoginScreen({{super.key}});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}}

class _LoginScreenState extends State<LoginScreen> {{
  final _email = TextEditingController();
  final _pw = TextEditingController();
  String _err = '';
  bool _isRegister = false;

  Future<void> _submit() async {{
    setState(() => _err = '');
    try {{
      if (_isRegister) {{
        await context.read<AuthStore>().register(_email.text, _pw.text);
      }} else {{
        await context.read<AuthStore>().login(_email.text, _pw.text);
      }}
    }} catch (e) {{
      setState(() => _err = e.toString());
    }}
  }}

  @override
  Widget build(BuildContext ctx) => Scaffold(
    body: Center(child: Container(width: 360, padding: const EdgeInsets.all(32),
      decoration: BoxDecoration(color: const Color(0xFF1e293b), borderRadius: BorderRadius.circular(16)),
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        Text('{project_name}', style: const TextStyle(fontSize: 28, fontWeight: FontWeight.bold, color: Color(0xFF38bdf8))),
        const SizedBox(height: 8),
        Text(_isRegister ? 'Create your account' : 'Sign in to continue', style: const TextStyle(color: Color(0xFF94a3b8))),
        const SizedBox(height: 24),
        TextField(controller: _email, decoration: const InputDecoration(labelText: 'Email', filled: true)),
        const SizedBox(height: 12),
        TextField(controller: _pw, obscureText: true, decoration: const InputDecoration(labelText: 'Password', filled: true)),
        if (_err.isNotEmpty) ...[const SizedBox(height: 8), Text(_err, style: const TextStyle(color: Colors.red))],
        const SizedBox(height: 24),
        SizedBox(width: double.infinity, child: ElevatedButton(onPressed: _submit, child: Text(_isRegister ? 'Create Account' : 'Sign In'))),
        const SizedBox(height: 16),
        TextButton(
          onPressed: () => setState(() => _isRegister = !_isRegister),
          child: Text(_isRegister ? 'Already have an account? Sign in' : "Don't have an account? Register"),
        ),
      ]),
    )),
  );
}}
""",
        "lib/screens/dashboard_screen.dart": """\
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../stores/auth_store.dart';

class DashboardScreen extends StatelessWidget {{
  const DashboardScreen({{super.key}});
  @override
  Widget build(BuildContext ctx) {{
    final auth = ctx.watch<AuthStore>();
    return Scaffold(
      appBar: AppBar(title: const Text('{project_name}'), actions: [
        IconButton(icon: const Icon(Icons.logout), onPressed: auth.logout)
      ]),
      body: const Center(child: Text('Dashboard — your projects appear here')),
    );
  }}
}}
""",
        "backend/requirements.txt": """\
fastapi>=0.111.0
uvicorn[standard]>=0.30.0
python-jose[cryptography]>=3.3.0
bcrypt>=4.1.0
python-multipart>=0.0.9
""",
        "backend/main.py": """\
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.routers import auth, health

app = FastAPI(title="{project_name}")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
""",
        "backend/__init__.py": "",
        "backend/routers/__init__.py": "",
        "backend/routers/health.py": """\
from fastapi import APIRouter
router = APIRouter()
@router.get("/health")
def health():
    return {{"status": "ok"}}
""",
        "backend/routers/auth.py": """\
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import bcrypt
from jose import jwt
from datetime import datetime, timedelta
import os

router = APIRouter()
SECRET = os.getenv("JWT_SECRET", "change-me")
ALGORITHM = "HS256"
_users: dict = {{}}
_BCRYPT_MAX_BYTES = 72

def _hash_password(password: str) -> str:
    truncated = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(truncated, bcrypt.gensalt()).decode("utf-8")

def _verify_password(password: str, hashed: str) -> bool:
    truncated = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.checkpw(truncated, hashed.encode("utf-8"))

class LoginIn(BaseModel):
    email: str
    password: str

def _token(email: str) -> str:
    return jwt.encode({{"sub": email, "exp": datetime.utcnow() + timedelta(hours=24)}}, SECRET, ALGORITHM)

@router.post("/register")
def register(body: LoginIn):
    if body.email in _users: raise HTTPException(400, "Already registered")
    _users[body.email] = _hash_password(body.password)
    return {{"token": _token(body.email)}}

@router.post("/login")
def login(body: LoginIn):
    h = _users.get(body.email)
    if not h or not _verify_password(body.password, h): raise HTTPException(401, "Invalid credentials")
    return {{"token": _token(body.email)}}
""",
        "docker-compose.yml": """\
version: "3.9"
services:
  backend:
    build:
      context: .
      dockerfile: Dockerfile.backend
    ports:
      - "8000:8000"
    environment:
      - JWT_SECRET=${{JWT_SECRET:-change-me}}
""",
        "Dockerfile.backend": """\
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
""",
        ".env.example": """\
JWT_SECRET=change-me-in-production
NIM_API_KEY=your-nvidia-api-key-here
""",
        "README.md": """\
# {project_name}

> {description}

## Stack
| Layer | Technology |
|-------|-----------|
| Mobile | Flutter |
| Backend | FastAPI |
| Auth | JWT |

## Quick Start
```bash
# Backend
cd backend && pip install -r requirements.txt && uvicorn backend.main:app --reload

# Flutter
flutter pub get && flutter run
```
""",
    },

    # ── Expo + Node ────────────────────────────────────────────────────────────
    "expo-node": {
        "package.json": """\
{{
  "name": "{project_name_slug}",
  "version": "1.0.0",
  "main": "expo-router/entry",
  "scripts": {{
    "start": "expo start",
    "android": "expo run:android",
    "ios": "expo run:ios",
    "web": "expo start --web"
  }},
  "dependencies": {{
    "expo": "~51.0.0",
    "expo-router": "~3.5.0",
    "react": "18.2.0",
    "react-native": "0.74.0",
    "expo-secure-store": "~13.0.0",
    "axios": "^1.7.2",
    "zustand": "^4.5.2"
  }},
  "devDependencies": {{
    "@babel/core": "^7.24.0",
    "@types/react": "~18.2.45",
    "typescript": "^5.3.3"
  }}
}}
""",
        "app/(tabs)/index.tsx": """\
import {{ Text, View, StyleSheet }} from 'react-native'
export default function HomeScreen() {{
  return (
    <View style={{styles.container}}>
      <Text style={{styles.title}}>{project_name}</Text>
      <Text style={{styles.sub}}>{description}</Text>
    </View>
  )
}}
const styles = StyleSheet.create({{
  container: {{ flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: '#0f172a' }},
  title: {{ fontSize: 32, fontWeight: '700', color: '#38bdf8', marginBottom: 8 }},
  sub: {{ fontSize: 16, color: '#94a3b8' }},
}})
""",
        "server/package.json": """\
{{
  "name": "{project_name_slug}-server",
  "version": "1.0.0",
  "scripts": {{ "start": "node index.js", "dev": "nodemon index.js" }},
  "dependencies": {{
    "express": "^4.19.2",
    "cors": "^2.8.5",
    "jsonwebtoken": "^9.0.2",
    "bcryptjs": "^2.4.3",
    "better-sqlite3": "^9.6.0"
  }}
}}
""",
        "server/index.js": """\
const express = require('express')
const cors = require('cors')
const jwt = require('jsonwebtoken')
const bcrypt = require('bcryptjs')
const Database = require('better-sqlite3')

const app = express()
const db = new Database('app.db')
const SECRET = process.env.JWT_SECRET || 'change-me'

db.exec(`CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, password TEXT)`)
db.exec(`CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, name TEXT, description TEXT, status TEXT DEFAULT 'created', created_at INTEGER DEFAULT (unixepoch()))`)

app.use(cors()); app.use(express.json())

const auth = (req, res, next) => {{
  const h = req.headers.authorization
  if (!h) return res.status(401).json({{error: 'Unauthorized'}})
  try {{ req.user = jwt.verify(h.replace('Bearer ', ''), SECRET); next() }}
  catch {{ res.status(401).json({{error: 'Invalid token'}}) }}
}}

app.get('/api/health', (_, res) => res.json({{status: 'ok'}}))

app.post('/api/auth/register', async (req, res) => {{
  const {{ email, password }} = req.body
  try {{
    const hash = await bcrypt.hash(password, 12)
    const {{ lastInsertRowid: id }} = db.prepare('INSERT INTO users (email, password) VALUES (?,?)').run(email, hash)
    res.json({{ token: jwt.sign({{sub: id, email}}, SECRET, {{expiresIn: '24h'}}) }})
  }} catch {{ res.status(400).json({{error: 'Email taken'}}) }}
}})

app.post('/api/auth/login', async (req, res) => {{
  const {{ email, password }} = req.body
  const user = db.prepare('SELECT * FROM users WHERE email=?').get(email)
  if (!user || !await bcrypt.compare(password, user.password)) return res.status(401).json({{error:'Invalid credentials'}})
  res.json({{ token: jwt.sign({{sub: user.id, email}}, SECRET, {{expiresIn: '24h'}}) }})
}})

app.get('/api/projects', auth, (req, res) => {{
  res.json(db.prepare('SELECT * FROM projects WHERE owner_id=?').all(req.user.sub))
}})

app.post('/api/projects', auth, (req, res) => {{
  const {{ name, description }} = req.body
  const {{ lastInsertRowid: id }} = db.prepare('INSERT INTO projects (owner_id, name, description) VALUES (?,?,?)').run(req.user.sub, name, description || '')
  res.status(201).json(db.prepare('SELECT * FROM projects WHERE id=?').get(id))
}})

const PORT = process.env.PORT || 8000
app.listen(PORT, () => console.log(`Server running on port ${{PORT}}`))
""",
        "docker-compose.yml": """\
version: "3.9"
services:
  server:
    build: ./server
    ports:
      - "8000:8000"
    environment:
      - JWT_SECRET=${{JWT_SECRET:-change-me}}
    volumes:
      - ./server:/app
      - /app/node_modules
""",
        "server/Dockerfile": """\
FROM node:20-alpine
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
EXPOSE 8000
CMD ["node", "index.js"]
""",
        ".env.example": """\
JWT_SECRET=change-me-in-production
NIM_API_KEY=your-nvidia-api-key-here
""",
        "README.md": """\
# {project_name}

> {description}

## Stack
| Layer | Technology |
|-------|-----------|
| Mobile | Expo / React Native |
| Backend | Node.js + Express |
| Database | SQLite (better-sqlite3) |
| Auth | JWT |

## Quick Start
```bash
# Server
cd server && npm install && npm start

# Expo
npm install && npx expo start
```
""",
    },
}


def _detect_stack(prompt: str) -> str:
    """Auto-detect best stack from prompt keywords."""
    p = prompt.lower()
    # Expo / React Native takes priority over generic 'mobile' keyword
    if any(w in p for w in ("expo", "react native", "rn ")):
        return "expo-node"
    if any(w in p for w in ("flutter", "dart", "ios", "android mobile", "mobile app", "mobile")):
        return "flutter-fastapi"
    if any(w in p for w in ("next.js", "nextjs", "next js", "postgres", "prisma", "vercel")):
        return "nextjs-postgres"
    return "fastapi-react"



def _scaffold_project(template_key: str, project_dir: Path, ctx: dict) -> List[str]:
    """Write all template files to project_dir, return list of created paths."""
    tmpl = _T.get(template_key, _T["fastapi-react"])
    created: List[str] = []
    for rel_path, content_tpl in tmpl.items():
        try:
            content = content_tpl.format_map(ctx)
        except KeyError:
            content = content_tpl  # leave unresolved slots as-is
        dest = project_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():  # never overwrite existing agent-written files
            dest.write_text(content, encoding="utf-8")
            created.append(rel_path)
    return created


# ── Section B+C: Specialized Agents ──────────────────────────────────────────

class SpecializedAgent:
    """Base class for all generation-layer agents.

    Each agent holds a tightly-scoped system prompt and calls the
    existing NIM provider pool for inference — no new HTTP clients.
    """

    role: str = "agent"
    _system_prompt: str = "You are a senior software engineer."

    def __init__(self, pool: "ProviderPool", config: Dict[str, Any]) -> None:
        self.pool = pool
        self.config = config

    def _call_nim(self, messages: List[Dict[str, str]], max_tokens: int = 8192) -> str:
        """Synchronous NIM call through the existing provider pool."""
        if not HAS_OPENAI:
            return f"# {self.role} output\n# openai package not available\n"

        for attempt in range(8):
            provider = None
            # Wait for an available provider (up to 60 s)
            for _ in range(30):
                provider = self.pool.next_available()
                if provider:
                    break
                time.sleep(2.0)
            if not provider:
                raise RuntimeError(f"[{self.role}] No NIM provider available after 60 s")

            model_cfg = provider.model_cfg
            payload: Dict[str, Any] = {
                "model": model_cfg["id"],
                "messages": messages,
                "max_tokens": max_tokens,
                "stream": False,
            }
            if model_cfg.get("temperature") is not None:
                payload["temperature"] = model_cfg["temperature"]
            if model_cfg.get("top_p") is not None:
                payload["top_p"] = model_cfg["top_p"]

            try:
                resp = provider.client.chat.completions.create(**payload)
                # A malformed/empty response — e.g. resp.choices == [] under
                # a content-filter block or certain backend error conditions
                # that don't raise — throws IndexError/AttributeError here,
                # neither of which is in OPENAI_ERRORS below. Without this
                # check that exception escapes the except clause entirely,
                # skipping every retry/rotation/backoff this loop provides
                # for every other failure mode and crashing straight out of
                # _call_nim on the very first bad response from any
                # provider, permanent or not. Treat it as just another
                # provider-side failure so it gets the same retry treatment.
                if not getattr(resp, "choices", None):
                    raise RuntimeError(f"Empty choices in response from {provider.name}")
                provider.record_success()
                return (resp.choices[0].message.content or "").strip()
            except OPENAI_ERRORS as e:
                err_str = str(e).lower()
                if "429" in err_str or "rate" in err_str:
                    provider.record_failure(cooldown=self.config.get("post_429_backoff", 25.0))
                    self.pool.propagate_shared_cooldown(provider, self.config.get("post_429_backoff", 25.0))
                    time.sleep(5.0 * (attempt + 1))
                elif "404" in err_str or "410" in err_str:
                    provider.record_failure(permanent=True)
                else:
                    provider.record_failure()
                    time.sleep(2.0)
            except (IndexError, AttributeError, KeyError, RuntimeError) as e:
                # Malformed-response class of failure (empty choices, missing
                # message/content on the returned object, etc.) — same
                # retry/backoff treatment as the generic OPENAI_ERRORS branch
                # above, just not wrapped in one of those SDK exception types.
                provider.record_failure()
                time.sleep(2.0)
        raise RuntimeError(f"[{self.role}] All NIM attempts exhausted")

    def _messages(self, user_content: str) -> List[Dict[str, str]]:
        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_content},
        ]

    def run(self, ctx: Dict[str, Any]) -> Dict[str, str]:
        """Run the agent. Returns {rel_path: content} of files to write."""
        raise NotImplementedError


class PlannerAgent(SpecializedAgent):
    role = "planner"
    _system_prompt = (
        "You are a senior software architect writing a project plan.\n"
        "Output ONLY the content of PLAN.md — a markdown document with exactly these sections:\n"
        "## Requirements\n## Risks\n## Acceptance Criteria\n\n"
        "Be specific, actionable, and technical. No preamble, no code blocks wrapping the markdown. "
        "Never wrap your output in ``` fences of any kind — output raw markdown text only, starting directly with the first heading."
    )

    def run(self, ctx: Dict[str, Any]) -> Dict[str, str]:
        prompt = (
            f"Project: {ctx['project_name']}\n"
            f"Stack: {ctx['stack']}\n"
            f"Description: {ctx['description']}\n\n"
            "Write PLAN.md with sections: Requirements, Risks, Acceptance Criteria."
        )
        content = self._call_nim(self._messages(prompt), max_tokens=4096)
        return {"PLAN.md": content}


class ArchitectAgent(SpecializedAgent):
    role = "architect"
    # Backend package root is stack-dependent and MUST match what BackendAgent,
    # DatabaseAgent, and TesterAgent hardcode downstream — otherwise generated
    # tests fail with ModuleNotFoundError (e.g. Tester writes
    # "from backend.main import app" but the architecture says app/main.py).
    _BACKEND_ROOT_BY_STACK = {
        "fastapi-react": "backend/ (module path: backend.main, backend.routers, backend.models)",
        "flutter-fastapi": "backend/ (module path: backend.main, backend.routers, backend.models)",
        "nextjs-postgres": "app/ (Next.js 14 App Router — app/api/.../route.ts)",
        "expo-node": "server/ (Express — server/routes/*.js)",
    }

    _system_prompt = (
        "You are a senior software architect writing ARCHITECTURE.md.\n"
        "Output ONLY the markdown document with these sections:\n"
        "## Components\n## Data Flow\n## Tech Stack\n## API Surface\n\n"
        "Be specific about module names, file paths, and data schemas. "
        "CRITICAL: the backend package root and its exact path/module convention will be given to you "
        "in the prompt — you MUST use that exact root for every backend file path and import example "
        "you write. Do not invent or default to any other root (never use app/main.py unless told to). "
        "Every other agent in this pipeline (backend, database, tests, deployment) hardcodes this same "
        "root, so any deviation here breaks the entire generated project. "
        "Never wrap your output in ``` fences of any kind — output raw markdown text only, starting directly with the first heading."
    )

    def run(self, ctx: Dict[str, Any]) -> Dict[str, str]:
        plan = ctx.get("PLAN.md", "No plan yet.")
        stack = ctx["stack"]
        backend_root = self._BACKEND_ROOT_BY_STACK.get(stack, "backend/ (module path: backend.main)")
        prompt = (
            f"Project: {ctx['project_name']} ({stack})\n"
            f"Description: {ctx['description']}\n\n"
            f"PLAN.md:\n{plan[:3000]}\n\n"
            f"MANDATORY backend package root for this stack: {backend_root}\n"
            "Use this exact root for every backend file path, import statement, and module reference "
            "in the document. Do not use any other backend root.\n\n"
            "Write ARCHITECTURE.md with sections: Components, Data Flow, Tech Stack, API Surface."
        )
        content = self._call_nim(self._messages(prompt), max_tokens=4096)
        return {"ARCHITECTURE.md": content}


class DesignerAgent(SpecializedAgent):
    role = "designer"
    _system_prompt = (
        "You are a senior UI/UX designer and frontend engineer.\n"
        "Output ONLY valid TypeScript/JavaScript/Dart code — no markdown fences, no explanations.\n"
        "Design a clean, modern dark-mode color theme and component system for the given project."
    )

    def run(self, ctx: Dict[str, Any]) -> Dict[str, str]:
        stack = ctx["stack"]
        if stack == "fastapi-react":
            prompt = (
                f"Project: {ctx['project_name']}\nDescription: {ctx['description']}\n\n"
                "Write frontend/src/theme.ts — a TypeScript module exporting a COLORS object and a TYPOGRAPHY object. "
                "Use a professional dark-mode palette with primary=#38bdf8, background=#0f172a, surface=#1e293b, "
                "text=#e2e8f0, muted=#94a3b8, danger=#fb7185, success=#4ade80. "
                "No imports needed. Export as: export const COLORS = {{...}}; export const TYPOGRAPHY = {{...}};"
            )
            out = self._call_nim(self._messages(prompt), max_tokens=2048)
            return {"frontend/src/theme.ts": out}
        elif stack == "nextjs-postgres":
            # Same root as FrontendAgent/BackendAgent/ArchitectAgent for this
            # stack — no frontend/ tree in a Next.js App Router project.
            prompt = (
                f"Project: {ctx['project_name']}\nDescription: {ctx['description']}\n\n"
                "Write lib/theme.ts — a TypeScript module exporting a COLORS object and a TYPOGRAPHY object. "
                "Use a professional dark-mode palette with primary=#38bdf8, background=#0f172a, surface=#1e293b, "
                "text=#e2e8f0, muted=#94a3b8, danger=#fb7185, success=#4ade80. "
                "No imports needed. Export as: export const COLORS = {{...}}; export const TYPOGRAPHY = {{...}};"
            )
            out = self._call_nim(self._messages(prompt), max_tokens=2048)
            return {"lib/theme.ts": out}
        elif stack == "flutter-fastapi":
            prompt = (
                f"Project: {ctx['project_name']}\n\n"
                "Write lib/theme.dart — a Flutter file that defines AppTheme with a dark ThemeData. "
                "Primary color #38bdf8, background #0f172a. Export: class AppTheme {{ static ThemeData dark() {{...}} }}"
            )
            out = self._call_nim(self._messages(prompt), max_tokens=2048)
            return {"lib/theme.dart": out}
        elif stack == "expo-node":
            # Explicit branch — do not fall through to silent {}. Expo apps
            # still benefit from a shared color/typography module under app/.
            prompt = (
                f"Project: {ctx['project_name']}\nDescription: {ctx['description']}\n\n"
                "Write app/theme.ts — a TypeScript module exporting COLORS and TYPOGRAPHY "
                "for a React Native / Expo dark-mode UI. Primary #38bdf8, background #0f172a, "
                "surface #1e293b, text #e2e8f0. Export: export const COLORS = {{...}}; export const TYPOGRAPHY = {{...}};"
            )
            out = self._call_nim(self._messages(prompt), max_tokens=2048)
            return {"app/theme.ts": out}
        else:
            raise ValueError(
                f"DesignerAgent: unsupported stack {stack!r}. "
                f"Expected one of: fastapi-react, nextjs-postgres, flutter-fastapi, expo-node."
            )


class FrontendAgent(SpecializedAgent):
    role = "frontend"
    _system_prompt = (
        "You are a senior frontend engineer. Output ONLY valid source code — no markdown fences.\n"
        "Write complete, working components. Never write TODO stubs or placeholder implementations.\n"
        "Use the dark color system: primary=#38bdf8, bg=#0f172a, surface=#1e293b."
    )

    def run(self, ctx: Dict[str, Any]) -> Dict[str, str]:
        stack = ctx["stack"]
        arch = ctx.get("ARCHITECTURE.md", "")[:2000]
        results: Dict[str, str] = {}

        if stack == "fastapi-react":
            # Generate a real feature component
            prompt = (
                f"Project: {ctx['project_name']}\nDescription: {ctx['description']}\n"
                f"Architecture:\n{arch}\n\n"
                "Write frontend/src/components/ProjectCard.tsx — a React component that accepts "
                "a project object {{id, name, description, status, created_at}} as props "
                "and renders a polished dark-mode card. Include hover effects via inline style state. "
                "Export as: export default function ProjectCard({{ project }}: {{ project: any }}) {{...}}"
            )
            results["frontend/src/components/ProjectCard.tsx"] = self._call_nim(self._messages(prompt), max_tokens=3000)

            # Generate a chat/builder UI component
            prompt2 = (
                f"Project: {ctx['project_name']}\n\n"
                "Write frontend/src/components/ChatPanel.tsx — a React component for an AI chat interface. "
                "It should have a messages list area (scrollable), a text input at the bottom, and a Send button. "
                "State: messages array, input string. Each message has {{role: 'user'|'assistant', content: string}}. "
                "Assistant messages render in #1e293b bubbles, user messages in #38bdf8 bubbles. "
                "Export as: export default function ChatPanel() {{...}}"
            )
            results["frontend/src/components/ChatPanel.tsx"] = self._call_nim(self._messages(prompt2), max_tokens=3000)

        elif stack == "nextjs-postgres":
            # Next.js 14 App Router has no separate frontend/ tree — components
            # live under app/ or components/ at the project root, matching the
            # same root ArchitectAgent and BackendAgent already use for this stack.
            prompt = (
                f"Project: {ctx['project_name']}\nDescription: {ctx['description']}\n"
                f"Architecture:\n{arch}\n\n"
                "Write components/ProjectCard.tsx — a React Server/Client component that accepts "
                "a project object {{id, name, description, status, created_at}} as props "
                "and renders a polished dark-mode card. Include hover effects via inline style state. "
                "Add 'use client' at the top if it uses hooks/state. "
                "Export as: export default function ProjectCard({{ project }}: {{ project: any }}) {{...}}"
            )
            results["components/ProjectCard.tsx"] = self._call_nim(self._messages(prompt), max_tokens=3000)

            prompt2 = (
                f"Project: {ctx['project_name']}\n\n"
                "Write components/ChatPanel.tsx — a React Client Component for an AI chat interface. "
                "Start with 'use client'. It should have a messages list area (scrollable), a text input "
                "at the bottom, and a Send button. State: messages array, input string. "
                "Each message has {{role: 'user'|'assistant', content: string}}. "
                "Assistant messages render in #1e293b bubbles, user messages in #38bdf8 bubbles. "
                "Export as: export default function ChatPanel() {{...}}"
            )
            results["components/ChatPanel.tsx"] = self._call_nim(self._messages(prompt2), max_tokens=3000)

        elif stack == "flutter-fastapi":
            prompt = (
                f"Project: {ctx['project_name']}\n\n"
                "Write lib/widgets/project_card.dart — a Flutter StatelessWidget that renders "
                "a dark-mode project card given name, description, and status strings. "
                "Use Card, ListTile, and Chip widgets. Export: class ProjectCard extends StatelessWidget {{...}}"
            )
            results["lib/widgets/project_card.dart"] = self._call_nim(self._messages(prompt), max_tokens=2000)

        elif stack == "expo-node":
            prompt = (
                f"Project: {ctx['project_name']}\n\n"
                "Write app/components/ProjectCard.tsx — a React Native component that renders "
                "a project card (name, description, status) using View, Text, and StyleSheet. "
                "Dark background #1e293b, primary #38bdf8. "
                "Export as: export default function ProjectCard({{ project }}: {{ project: any }}) {{...}}"
            )
            results["app/components/ProjectCard.tsx"] = self._call_nim(self._messages(prompt), max_tokens=2000)

        else:
            raise ValueError(
                f"FrontendAgent: unsupported stack {stack!r}. "
                f"Expected one of: fastapi-react, nextjs-postgres, flutter-fastapi, expo-node."
            )

        return results


class BackendAgent(SpecializedAgent):
    role = "backend"
    _system_prompt = (
        "You are a senior backend engineer. Output ONLY valid Python or JavaScript source code — no markdown fences.\n"
        "Write complete, working implementations — never placeholder functions or TODO stubs.\n"
        "Follow REST conventions. Include proper error handling and input validation."
    )

    def run(self, ctx: Dict[str, Any]) -> Dict[str, str]:
        stack = ctx["stack"]
        arch = ctx.get("ARCHITECTURE.md", "")[:2000]
        results: Dict[str, str] = {}

        if stack in ("fastapi-react", "flutter-fastapi"):
            prompt = (
                f"Project: {ctx['project_name']}\nDescription: {ctx['description']}\n"
                f"Architecture:\n{arch}\n\n"
                "Write backend/nim_service.py — a Python module that wraps NVIDIA NIM API calls. "
                "It should have a function: async def generate_code(prompt: str, api_key: str, model: str = 'z-ai/glm-5.2') -> str "
                "that calls https://integrate.api.nvidia.com/v1/chat/completions using the openai Python package "
                "with OpenAI(base_url='https://integrate.api.nvidia.com/v1', api_key=api_key). "
                "Return the assistant message content as a string. Include proper error handling."
            )
            results["backend/nim_service.py"] = self._call_nim(self._messages(prompt), max_tokens=3000)

            prompt2 = (
                f"Project: {ctx['project_name']}\n\n"
                "Write backend/routers/generate.py — a FastAPI router at prefix /api/generate. "
                "POST /api/generate/code accepts {{prompt: str, stack: str}} JSON body and auth header. "
                "CRITICAL call signature: generate_code is defined as "
                "async def generate_code(prompt: str, api_key: str, model: str = 'z-ai/glm-5.2') -> str "
                "in backend/nim_service.py. Call it ONLY with those keyword args — never pass stack= or user=. "
                "Read the API key from os.environ.get('NVIDIA_API_KEY') or os.environ.get('NIM_API_KEY'); "
                "if missing, raise HTTPException(503, 'NIM API key not configured'). "
                "Return {{code: str, files: list}} where code is the returned string and files is []. "
                "Include a non-streaming path only (skip StreamingResponse unless you implement a real async generator). "
                "Import: from backend.routers.auth import current_user, from backend.nim_service import generate_code, import os"
            )
            results["backend/routers/generate.py"] = self._call_nim(self._messages(prompt2), max_tokens=3000)

        elif stack == "nextjs-postgres":
            prompt = (
                f"Project: {ctx['project_name']}\n\n"
                "Write app/api/generate/route.ts — a Next.js 14 App Router API route (POST). "
                "It accepts {{prompt: string}} in the body and calls NVIDIA NIM API "
                "(https://integrate.api.nvidia.com/v1/chat/completions) using fetch with streaming. "
                "Return a ReadableStream of text chunks. Use process.env.NIM_API_KEY."
            )
            results["app/api/generate/route.ts"] = self._call_nim(self._messages(prompt), max_tokens=3000)

        elif stack == "expo-node":
            prompt = (
                f"Project: {ctx['project_name']}\n\n"
                "Write server/routes/generate.js — an Express router at /api/generate. "
                "POST /api/generate/code accepts {{prompt, stack}} and calls NVIDIA NIM using node-fetch. "
                "Auth middleware: require Bearer JWT. Return {{code: string}}."
            )
            results["server/routes/generate.js"] = self._call_nim(self._messages(prompt), max_tokens=3000)

        else:
            raise ValueError(
                f"BackendAgent: unsupported stack {stack!r}. "
                f"Expected one of: fastapi-react, nextjs-postgres, flutter-fastapi, expo-node."
            )

        return results


class DatabaseAgent(SpecializedAgent):
    role = "database"
    _system_prompt = (
        "You are a senior database engineer. Output ONLY valid Python or SQL source code — no markdown fences.\n"
        "Write complete SQLAlchemy models or Prisma schemas. No placeholder fields."
    )

    def run(self, ctx: Dict[str, Any]) -> Dict[str, str]:
        stack = ctx["stack"]
        arch = ctx.get("ARCHITECTURE.md", "")[:2000]
        results: Dict[str, str] = {}

        if stack in ("fastapi-react", "flutter-fastapi"):
            prompt = (
                f"Project: {ctx['project_name']}\nDescription: {ctx['description']}\n"
                f"Architecture:\n{arch}\n\n"
                "Write backend/models.py — SQLAlchemy 2.x ORM models for this project. "
                "Include: Base = declarative_base(), User model (id UUID pk, email unique, password_hash, created_at), "
                "Project model (id UUID pk, owner_id FK→users, name, description, status, stack, created_at, updated_at). "
                "Use proper types: String, DateTime, ForeignKey. Add __repr__ methods."
            )
            results["backend/models.py"] = self._call_nim(self._messages(prompt), max_tokens=3000)

            prompt2 = (
                "Write backend/database.py — SQLAlchemy 2.x database setup. "
                "Use create_engine with DATABASE_URL from env (default: sqlite:///./app.db). "
                "Include: engine, SessionLocal = sessionmaker(...), get_db() FastAPI dependency, init_db() that calls Base.metadata.create_all. "
                "Import Base from backend.models."
            )
            results["backend/database.py"] = self._call_nim(self._messages(prompt2), max_tokens=2000)

        elif stack == "nextjs-postgres":
            prompt = (
                f"Project: {ctx['project_name']}\nDescription: {ctx['description']}\n"
                f"Architecture:\n{arch}\n\n"
                "Write prisma/schema.prisma — a Prisma schema for PostgreSQL. "
                "Include: generator client { provider = \"prisma-client-js\" }, "
                "datasource db { provider = \"postgresql\", url = env(\"DATABASE_URL\") }, "
                "model User (id String @id @default(uuid()), email String @unique, passwordHash String, "
                "createdAt DateTime @default(now())), "
                "model Project (id String @id @default(uuid()), ownerId String, owner User @relation(fields: [ownerId], references: [id]), "
                "name String, description String, status String, stack String, "
                "createdAt DateTime @default(now()), updatedAt DateTime @updatedAt). "
                "No placeholder fields."
            )
            results["prisma/schema.prisma"] = self._call_nim(self._messages(prompt), max_tokens=2000)

        elif stack == "expo-node":
            prompt = (
                f"Project: {ctx['project_name']}\nDescription: {ctx['description']}\n\n"
                "Write server/db.js — a Node.js module setting up a local SQLite database via better-sqlite3. "
                "Export: const db = new Database(process.env.DB_PATH || './app.db'); "
                "and a function initDb() that runs CREATE TABLE IF NOT EXISTS for a users table "
                "(id TEXT PRIMARY KEY, email TEXT UNIQUE, password_hash TEXT, created_at TEXT) "
                "and a projects table (id TEXT PRIMARY KEY, owner_id TEXT, name TEXT, description TEXT, "
                "status TEXT, stack TEXT, created_at TEXT, updated_at TEXT). "
                "Export: module.exports = { db, initDb };"
            )
            results["server/db.js"] = self._call_nim(self._messages(prompt), max_tokens=2000)

        else:
            raise ValueError(
                f"DatabaseAgent: unsupported stack {stack!r}. "
                f"Expected one of: fastapi-react, nextjs-postgres, flutter-fastapi, expo-node."
            )

        return results


class TesterAgent(SpecializedAgent):
    role = "tester"
    _system_prompt = (
        "You are a QA engineer. Output ONLY valid Python pytest test code — no markdown fences.\n"
        "Write real tests with meaningful assertions — never trivial assert True.\n"
        "Use httpx.TestClient or fastapi.testclient.TestClient for API tests."
    )

    def run(self, ctx: Dict[str, Any]) -> Dict[str, str]:
        stack = ctx["stack"]
        results: Dict[str, str] = {}

        if stack in ("fastapi-react", "flutter-fastapi"):
            prompt = (
                f"Project: {ctx['project_name']}\n\n"
                "Write tests/test_api.py — pytest integration tests for the FastAPI backend.\n"
                "Include:\n"
                "1. test_health() — GET /api/health returns 200 with status=ok\n"
                "2. test_register() — POST /api/auth/register with email/password returns token\n"
                "3. test_login() — POST /api/auth/login with correct credentials returns token\n"
                "4. test_login_wrong_password() — returns 401\n"
                "5. test_create_project() — POST /api/projects/ with auth header creates project\n"
                "6. test_list_projects() — GET /api/projects/ returns list\n\n"
                "Use: from fastapi.testclient import TestClient; from backend.main import app\n"
                "client = TestClient(app)"
            )
            results["tests/test_api.py"] = self._call_nim(self._messages(prompt), max_tokens=4000)

            results["tests/__init__.py"] = ""
            results["tests/conftest.py"] = (
                "import pytest\n"
                "from fastapi.testclient import TestClient\n"
                "from backend.main import app\n\n"
                "@pytest.fixture\n"
                "def client():\n"
                "    with TestClient(app) as c:\n"
                "        yield c\n"
            )

        elif stack == "nextjs-postgres":
            prompt = (
                f"Project: {ctx['project_name']}\n\n"
                "Write tests/api.test.ts — Jest tests for Next.js API routes using node-fetch.\n"
                "Test: health endpoint, register, login, create project endpoints.\n"
                "Use: const BASE = 'http://localhost:3000'"
            )
            results["tests/api.test.ts"] = self._call_nim(self._messages(prompt), max_tokens=3000)

        elif stack == "expo-node":
            # No agent in this pipeline writes a standalone server/app.js
            # entrypoint for expo-node (only server/routes/*.js routers) — so
            # unlike the other stacks, the test file must build its own
            # minimal Express app inline and mount the generated router,
            # rather than importing an entrypoint module that doesn't exist.
            prompt = (
                f"Project: {ctx['project_name']}\n\n"
                "Write server/tests/api.test.js — Jest + supertest integration tests for the Express backend.\n"
                "IMPORTANT: there is no server/app.js entrypoint in this project. Build the test app inline:\n"
                "  const express = require('express');\n"
                "  const app = express();\n"
                "  app.use(express.json());\n"
                "  app.get('/api/health', (req, res) => res.json({ status: 'ok' }));\n"
                "  // mount any other generated routers under server/routes/ here if present\n"
                "Include:\n"
                "1. test for GET /api/health returns 200 with status='ok'\n"
                "2. a second test exercising one route from server/routes/generate.js if it can be "
                "required without a live NIM API key, otherwise skip it with test.skip and a comment why\n\n"
                "Use: const request = require('supertest');"
            )
            results["server/tests/api.test.js"] = self._call_nim(self._messages(prompt), max_tokens=3000)

        else:
            raise ValueError(
                f"TesterAgent: unsupported stack {stack!r}. "
                f"Expected one of: fastapi-react, nextjs-postgres, flutter-fastapi, expo-node."
            )

        return results


class DevOpsAgent(SpecializedAgent):
    role = "devops"
    _system_prompt = (
        "You are a DevOps engineer. Output ONLY valid YAML, shell scripts, or Dockerfile content.\n"
        "Never output markdown fences or explanations in your files."
    )

    def run(self, ctx: Dict[str, Any]) -> Dict[str, str]:
        results: Dict[str, str] = {}

        # GitHub Actions CI
        prompt = (
            f"Project: {ctx['project_name']}\nStack: {ctx['stack']}\n\n"
            "Write .github/workflows/ci.yml — a GitHub Actions workflow that:\n"
            "1. Triggers on push to main and pull_request\n"
            "2. Sets up Python 3.12 (or Node 20 for nextjs/expo stacks)\n"
            "3. Installs dependencies\n"
            "4. Runs tests (pytest for Python, npm test for Node)\n"
            "Use ubuntu-latest runner."
        )
        results[".github/workflows/ci.yml"] = self._call_nim(self._messages(prompt), max_tokens=2000)

        # VERIFICATION.md — grounded in actual test output, not asserted.
        # DevOpsAgent runs before tests exist on the very first pass (no
        # test_output in ctx yet), so this has to degrade honestly instead
        # of assuming success either way.
        test_output = ctx.get("test_output")  # TestRunResult | None, set by orchestrator after repair pass
        if test_output is None:
            test_section = "Tests have not been executed yet for this generation."
        elif not test_output.ran:
            test_section = f"Test runner did not run (no test files or unsupported stack).\n{test_output.output[:1500]}"
        else:
            test_section = (
                f"Test run {'PASSED' if test_output.passed else 'FAILED'}.\n"
                f"Raw output:\n{test_output.output[:3000]}"
            )
        prompt2 = (
            f"Project: {ctx['project_name']}\nStack: {ctx['stack']}\n"
            f"Description: {ctx['description']}\n\n"
            f"Actual test results:\n{test_section}\n\n"
            "Write VERIFICATION.md — a checklist verifying acceptance criteria.\n"
            "Format each item as: | Criterion | Test | Status |\n"
            "Include checks for: auth works, projects CRUD works, Docker builds, CI passes, preview loads.\n"
            "Mark a criterion ✅ Pass ONLY if the actual test results above confirm it. "
            "If tests did not run, or failed, or don't cover a criterion, mark it "
            "⚠️ Unverified or ❌ Fail as appropriate — do not mark anything Pass on the "
            "assumption that it probably works."
        )
        results["VERIFICATION.md"] = self._call_nim(self._messages(prompt2), max_tokens=2000)

        return results


# ── Section D: GenerationOrchestrator ────────────────────────────────────────

@dataclass
class TestRunResult:
    """Outcome of actually invoking the test runner as a subprocess.

    `ran` distinguishes "we invoked pytest/npm test and it told us
    something" from "there was nothing to run" (no test files, no
    runner for this stack, timeout) — the two must not collapse into
    the same falsy state or a missing test suite silently reads as a
    passing one.
    """
    ran: bool
    passed: bool
    output: str = ""


@dataclass
class GenerationResult:
    success: bool
    project_dir: Path
    preview_url: Optional[str] = None
    files_created: List[str] = field(default_factory=list)
    files_generated: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    stack: str = "fastapi-react"
    test_output: Optional[TestRunResult] = None
    repair_rounds: int = 0


class GenerationOrchestrator:
    """Coordinates the full prompt → scaffold → agent-generate → preview pipeline."""

    AGENTS = [
        PlannerAgent,
        ArchitectAgent,
        DesignerAgent,
        FrontendAgent,
        BackendAgent,
        DatabaseAgent,
        TesterAgent,
        DevOpsAgent,
    ]

    def __init__(self, pool: "ProviderPool", config: Dict[str, Any]) -> None:
        self.pool = pool
        self.config = config

    def _run_tests(self, project_dir: Path, stack: str) -> TestRunResult:
        """Actually invoke the test runner. This is the piece that was
        missing entirely — TesterAgent previously only wrote a file to
        disk; nothing executed it. `ran=False` means we have no verdict
        (no runner for this stack, no test files, or the process itself
        failed to start/timed out) — callers must not treat that as pass.
        """
        if stack in ("fastapi-react", "flutter-fastapi"):
            if not (project_dir / "tests").exists():
                return TestRunResult(ran=False, passed=False, output="No tests/ directory found.")
            runner = shutil.which("pytest") or (shutil.which("python") and "python")
            cmd = (
                [runner, "-m", "pytest", "tests/", "-x", "--tb=short", "-q"]
                if runner == "python"
                else [runner, "tests/", "-x", "--tb=short", "-q"]
            )
        elif stack in ("nextjs-postgres", "expo-node"):
            if not (project_dir / "package.json").exists():
                return TestRunResult(ran=False, passed=False, output="No package.json found.")
            npm = shutil.which("npm")
            if not npm:
                return TestRunResult(ran=False, passed=False, output="npm not found on PATH.")
            cmd = [npm, "test", "--", "--ci"]
        else:
            return TestRunResult(ran=False, passed=False, output=f"No test runner defined for stack={stack!r}.")

        try:
            wrapped = None
            if RunTool._docker_available():
                wrapped = RunTool._wrap_argv_for_docker(cmd, project_dir, project_dir)
            proc = subprocess.run(
                wrapped if wrapped is not None else cmd,
                cwd=str(project_dir) if wrapped is None else str(project_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            return TestRunResult(ran=True, passed=proc.returncode == 0, output=output[-6000:])
        except FileNotFoundError as e:
            return TestRunResult(ran=False, passed=False, output=f"Test runner not found: {e}")
        except subprocess.TimeoutExpired:
            return TestRunResult(ran=False, passed=False, output="Test run timed out after 120s.")
        except Exception as e:
            return TestRunResult(ran=False, passed=False, output=f"Test run failed to execute: {e}")

    def _repair_pass(
        self,
        ctx: Dict[str, Any],
        project_dir: Path,
        result: GenerationResult,
        on_progress: Optional[Any],
        max_rounds: int = 3,
        seed_failure_output: Optional[str] = None,
    ) -> TestRunResult:
        """Run tests; on failure, ask the model for targeted file fixes and
        retry, up to max_rounds. Returns the final TestRunResult (which may
        still be failing or unrun after exhausting rounds — that's a real
        outcome, not swallowed into success).

        seed_failure_output, when given, is treated as an opening-round
        failure even before any test executes — used to fold static
        cross-file consistency problems into the same repair mechanism
        instead of needing a separate one.
        """
        def _prog(phase: str, msg: str) -> None:
            if on_progress:
                on_progress(phase, msg)
            else:
                UI.info(f"[{phase}] {msg}")

        repair_agent = SpecializedAgent(self.pool, self.config)
        repair_agent.role = "repair"
        repair_agent._system_prompt = (
            "You are a senior engineer fixing failing tests. Given failing test "
            "output and the current content of relevant files, return ONLY a JSON "
            "object mapping relative file paths to their full corrected content. "
            "No markdown fences, no explanation, no partial diffs — full file "
            "content for every file you change. Example: "
            '{"backend/main.py": "<full corrected file>"}'
        )

        if seed_failure_output:
            test_result = TestRunResult(ran=True, passed=False, output=seed_failure_output)
        else:
            test_result = self._run_tests(project_dir, ctx["stack"])
            if not test_result.ran:
                _prog("test", f"⚠ tests did not run: {test_result.output[:200]}")
                return test_result
            if test_result.passed:
                _prog("test", "✓ tests passed on first run")
                return test_result

        for round_n in range(1, max_rounds + 1):
            _prog("repair", f"round {round_n}/{max_rounds}: tests failing, requesting fix…")

            # Only the files this orchestrator actually generated are fair
            # game to patch, and we send real current content — not the
            # truncated docs the generation agents got — since a repair
            # needs the actual broken code, not a summary of it.
            candidate_paths = [
                p for p in result.files_generated
                if p.endswith((".py", ".ts", ".tsx", ".js", ".dart"))
            ]
            file_blob = ""
            for rel_path in candidate_paths[:12]:
                fp = project_dir / rel_path
                if fp.exists():
                    file_blob += f"\n--- {rel_path} ---\n{fp.read_text(encoding='utf-8', errors='replace')[:3000]}\n"

            prompt = (
                f"Stack: {ctx['stack']}\n\n"
                f"Failing test output:\n{test_result.output[:4000]}\n\n"
                f"Current file contents:\n{file_blob[:12000]}\n\n"
                "Return corrected file contents as a JSON object: "
                '{"path/to/file": "full corrected content", ...}. '
                "Only include files that actually need to change."
            )

            try:
                raw = repair_agent._call_nim(repair_agent._messages(prompt), max_tokens=8000)
                fixes = json.loads(raw)
            except (json.JSONDecodeError, RuntimeError) as e:
                result.errors.append(f"repair round {round_n}: could not parse/obtain fix — {e}")
                _prog("repair", f"✗ round {round_n}: {e}")
                continue

            if not isinstance(fixes, dict) or not fixes:
                result.errors.append(f"repair round {round_n}: model returned no usable fixes")
                continue

            for rel_path, content in fixes.items():
                if not isinstance(content, str) or not content.strip():
                    continue
                # Same containment discipline as the rest of the pipeline —
                # a repair fix is still model-controlled path input.
                safe_rel = rel_path.strip().lstrip("/\\")
                dest = (project_dir / safe_rel).resolve()
                try:
                    dest.relative_to(project_dir.resolve())
                except ValueError:
                    result.errors.append(f"repair round {round_n}: rejected out-of-tree path {rel_path!r}")
                    continue
                # Repair writes bypass WriteTool, so apply the same fence-strip
                # and (for .py) AST gate the normal write path enforces —
                # otherwise a fenced or syntactically invalid "fix" lands on
                # disk and the next pytest round fails for a reason the
                # repair loop itself just introduced.
                content = WriteTool._strip_balanced_code_fence(content, safe_rel)
                if safe_rel.endswith((".py", ".pyi")):
                    try:
                        ast.parse(content)
                    except SyntaxError as e:
                        result.errors.append(
                            f"repair round {round_n}: refused invalid Python for "
                            f"{safe_rel!r} — {e.msg} (line {e.lineno})"
                        )
                        continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
                ctx[safe_rel] = content
                if safe_rel not in result.files_generated:
                    result.files_generated.append(safe_rel)

            test_result = self._run_tests(project_dir, ctx["stack"])
            if not test_result.ran:
                # No test runner/files for this stack after the repair
                # write — nothing more the repair loop can validate
                # against. Stop here rather than burning remaining rounds
                # re-requesting fixes with no way to confirm they helped.
                _prog("repair", f"⚠ round {round_n}: tests still don't run — {test_result.output[:150]}")
                return test_result
            if test_result.passed:
                _prog("repair", f"✓ tests passed after round {round_n}")
                result.repair_rounds = round_n
                return test_result

        result.repair_rounds = max_rounds
        _prog("repair", f"✗ tests still failing after {max_rounds} repair round(s)")
        return test_result

    def _check_python_import_consistency(
        self, project_dir: Path, generated_paths: List[str]
    ) -> List[str]:
        """Static cross-file check for the specific failure mode described
        in the review: BackendAgent and DatabaseAgent (and others) each
        write Python files in separate, independent LLM calls, seeing only
        truncated ARCHITECTURE.md text — not each other's actual code — so
        an import like `from backend.database import Base` can reference a
        module DatabaseAgent never actually created, or named differently.

        This does NOT execute anything — it's ast.parse plus a name-exists
        check against the project's own generated Python files, so it also
        catches problems a test suite might never exercise (an import at
        module load time that isn't covered by any generated test). Returns
        a list of human-readable problem descriptions; empty means clean.
        """
        py_files = [p for p in generated_paths if p.endswith(".py")]
        if not py_files:
            return []

        # Map "backend.database" -> "backend/database.py" (and package
        # __init__ forms) so we can check `from backend.database import X`
        # style imports against what was actually written to disk.
        module_map: Dict[str, str] = {}
        for rel_path in py_files:
            mod = rel_path[:-3].replace("/", ".").replace("\\", ".")
            module_map[mod] = rel_path
            if mod.endswith(".__init__"):
                module_map[mod[: -len(".__init__")]] = rel_path

        problems: List[str] = []
        for rel_path in py_files:
            fp = project_dir / rel_path
            if not fp.exists():
                continue
            try:
                source = fp.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=rel_path)
            except SyntaxError as e:
                problems.append(f"{rel_path}: syntax error — {e.msg} (line {e.lineno})")
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.level and node.level > 0:
                        continue  # relative import — out of scope for this pass
                    if not node.module or not node.module.startswith(
                        tuple(m.split(".")[0] for m in module_map) or ("__nomatch__",)
                    ):
                        continue  # third-party import (fastapi, sqlalchemy, ...) — not ours to check
                    if node.module in module_map:
                        continue
                    # No escape clause here on purpose. An earlier version
                    # tried to excuse an import when its *ancestor* package
                    # was generated (e.g. "backend.database" excused by the
                    # presence of "backend" itself from backend/__init__.py)
                    # — but a package's own __init__.py existing says
                    # nothing about whether a specific submodule under it
                    # was ever written. Verified via harness: that escape
                    # let `from backend.database import Base` pass clean
                    # even though backend/database.py was never generated —
                    # exactly the failure this check exists to catch. The
                    # only real escape is an exact match in module_map
                    # (handled by the `if node.module in module_map` check
                    # above); anything else sharing our top-level package
                    # name but without its own generated file is reported.
                    problems.append(
                        f"{rel_path}: imports '{node.module}' but no generated file "
                        f"provides that module (generated: {sorted(module_map)})"
                    )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if any(m.split(".")[0] == top for m in module_map) and alias.name not in module_map:
                            # Same fix as the ImportFrom branch above: no
                            # ancestor-package escape — module_map already
                            # contains an exact-match check (`alias.name
                            # not in module_map`); a shared top-level
                            # package name without its own generated file
                            # is reported, not excused.
                            problems.append(
                                f"{rel_path}: imports '{alias.name}' but no generated file "
                                f"provides that module"
                            )
        return problems

    def generate(
        self,
        description: str,
        project_dir: Path,
        stack: Optional[str] = None,
        on_progress: Optional[Any] = None,  # Callable[[str, str], None]
    ) -> GenerationResult:
        """Run the full generation pipeline.

        1. Auto-detect stack from description if not provided
        2. Scaffold template files (instant)
        3. Run each SpecializedAgent in order, writing generated files
        4. Return GenerationResult with file lists and preview URL
        """
        def _prog(phase: str, msg: str) -> None:
            if on_progress:
                on_progress(phase, msg)
            else:
                UI.info(f"[{phase}] {msg}")

        stack = stack or _detect_stack(description)
        project_name = project_dir.name
        project_name_slug = re.sub(r"[^a-z0-9-]", "-", project_name.lower()).strip("-") or "my-app"

        ctx: Dict[str, Any] = {
            "project_name": project_name,
            "project_name_slug": project_name_slug,
            "description": description,
            "stack": stack,
            "year": datetime.now().year,
        }

        result = GenerationResult(success=False, project_dir=project_dir, stack=stack)

        # Phase 1: Scaffold template
        _prog("scaffold", f"Writing {stack} template files…")
        try:
            created = _scaffold_project(stack, project_dir, ctx)
            result.files_created = created
            _prog("scaffold", f"✓ {len(created)} template files written")
        except Exception as e:
            result.errors.append(f"Scaffold failed: {e}")
            _prog("scaffold", f"✗ Scaffold error: {e}")

        # Phase 2: Run agents (Planner..Tester), then repair, then DevOps —
        # DevOpsAgent is pulled out of the fixed sequence so its
        # VERIFICATION.md prompt can see the real test_output in ctx
        # instead of running blind before any test has been executed.
        pre_devops = [cls for cls in self.AGENTS if cls is not DevOpsAgent]
        agents = [cls(self.pool, self.config) for cls in pre_devops]
        for agent in agents:
            _prog(agent.role, f"Running {agent.role} agent…")
            try:
                files = agent.run(ctx)
                written = 0
                for rel_path, content in files.items():
                    if not content.strip():
                        continue
                    # Agent outputs bypass WriteTool, so apply the same
                    # fence-strip + Python AST gate the interactive write
                    # path enforces. Without this, a fenced or invalid
                    # model payload lands on disk and only surfaces later
                    # as a ModuleNotFoundError / SyntaxError in tests.
                    content = WriteTool._strip_balanced_code_fence(content, rel_path)
                    if rel_path.endswith((".py", ".pyi")):
                        try:
                            ast.parse(content)
                        except SyntaxError as e:
                            result.errors.append(
                                f"{agent.role}: refused invalid Python for "
                                f"{rel_path!r} — {e.msg} (line {e.lineno})"
                            )
                            _prog(agent.role, f"✗ refused {rel_path}: SyntaxError line {e.lineno}")
                            continue
                    dest = project_dir / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content, encoding="utf-8")
                    result.files_generated.append(rel_path)
                    # Feed artifact content back into ctx for downstream agents
                    ctx[rel_path] = content
                    written += 1
                _prog(agent.role, f"✓ {agent.role}: {written} file(s) generated")
            except Exception as e:
                err = f"{agent.role} failed: {e}"
                result.errors.append(err)
                _prog(agent.role, f"✗ {err}")

        # Phase 2.5: static cross-file consistency check (ast-based) —
        # catches import mismatches between independently-generated
        # Python files (e.g. BackendAgent importing a module DatabaseAgent
        # never wrote) before spending a test-run round-trip to discover
        # the same thing, and catches ones a generated test might not
        # exercise at all.
        import_problems = self._check_python_import_consistency(project_dir, result.files_generated)
        if import_problems:
            _prog("consistency", f"✗ {len(import_problems)} cross-file import issue(s) found — folding into repair")
            for p in import_problems:
                result.errors.append(f"consistency: {p}")
        else:
            _prog("consistency", "✓ no cross-file import mismatches detected")

        # Phase 3: run tests for real, repair on failure, cap at 3 rounds.
        # This is the loop that was previously absent entirely — TesterAgent
        # wrote tests/test_api.py and nothing ever executed it. If the
        # static check above found import problems, seed them as the
        # opening failure so round 1 fixes those even if the generated
        # test suite wouldn't itself have caught them.
        test_result = self._repair_pass(
            ctx, project_dir, result, on_progress,
            seed_failure_output=(
                "Static import-consistency check found the following issues "
                "(fix these first — a module referenced by import does not "
                "exist among the generated files):\n" + "\n".join(import_problems)
            ) if import_problems else None,
        )
        result.test_output = test_result
        ctx["test_output"] = test_result

        # Phase 4: DevOps now runs with real test_output available, so
        # VERIFICATION.md reflects what actually happened.
        devops = DevOpsAgent(self.pool, self.config)
        _prog(devops.role, f"Running {devops.role} agent…")
        try:
            files = devops.run(ctx)
            written = 0
            for rel_path, content in files.items():
                if not content.strip():
                    continue
                content = WriteTool._strip_balanced_code_fence(content, rel_path)
                if rel_path.endswith((".py", ".pyi")):
                    try:
                        ast.parse(content)
                    except SyntaxError as e:
                        result.errors.append(
                            f"{devops.role}: refused invalid Python for "
                            f"{rel_path!r} — {e.msg} (line {e.lineno})"
                        )
                        _prog(devops.role, f"✗ refused {rel_path}: SyntaxError line {e.lineno}")
                        continue
                dest = project_dir / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
                result.files_generated.append(rel_path)
                ctx[rel_path] = content
                written += 1
            _prog(devops.role, f"✓ {devops.role}: {written} file(s) generated")
        except Exception as e:
            err = f"{devops.role} failed: {e}"
            result.errors.append(err)
            _prog(devops.role, f"✗ {err}")

        # success now requires: no hard agent failures AND tests either
        # passed or genuinely couldn't be run (missing runner is a
        # separate, visible failure mode, not silently "successful").
        # A test run that executed and failed is never success, regardless
        # of how many files got written — this replaces the previous
        # `len(errors) == 0 or len(files_generated) > 0`, which reported
        # success on a single stray file with seven agents having thrown.
        if not test_result.ran:
            result.success = len(result.errors) == 0
            if test_result.ran is False:
                _prog("verdict", f"⚠ success={result.success} but tests never ran — verify manually")
        else:
            result.success = len(result.errors) == 0 and test_result.passed

        return result


# ── Section E: LivePreviewManager ─────────────────────────────────────────────

class LivePreviewManager:
    """Manages ephemeral per-project dev servers.

    Assigns a unique port in 3100–3999 per project directory.
    Spawns the appropriate dev server for the given stack.
    Registers atexit cleanup so servers die with the parent process.
    """

    _servers: Dict[str, subprocess.Popen] = {}  # project_id → process
    _ports: Dict[str, int] = {}                 # project_id → port
    _lock = threading.Lock()

    @classmethod
    def _project_key(cls, project_dir: Path) -> str:
        return _project_id(project_dir)

    @classmethod
    def _server_cmd(cls, stack: str, project_dir: Path, port: int) -> Optional[List[str]]:
        """Return the command to start a dev server for this stack."""
        if stack in ("fastapi-react", "flutter-fastapi"):
            # Backend via uvicorn
            py = shutil.which("uvicorn") or "uvicorn"
            return [py, "backend.main:app", "--host", "0.0.0.0", "--port", str(port), "--reload"]
        elif stack == "nextjs-postgres":
            npm = shutil.which("npm") or "npm"
            return [npm, "run", "dev", "--", "--port", str(port)]
        elif stack == "expo-node":
            node = shutil.which("node") or "node"
            return [node, str(project_dir / "server" / "index.js")]
        return None

    @classmethod
    def start(cls, project_dir: Path, stack: str) -> Optional[str]:
        """Start a dev server and return its URL, or None on failure."""
        key = cls._project_key(project_dir)
        with cls._lock:
            # Return existing URL if server is running
            if key in cls._servers:
                proc = cls._servers[key]
                if proc.poll() is None:
                    port = cls._ports.get(key, 8000)
                    return f"http://localhost:{port}"
                else:
                    del cls._servers[key]

            try:
                port = _find_free_port(3100, 3999)
            except RuntimeError:
                return None

            # _find_free_port proves a port is free by binding then
            # immediately releasing it (it has to — the port needs to be
            # free for the *subprocess* to bind, not held by us). That
            # leaves a real gap between "port confirmed free" and "the
            # child process has actually called bind()" — Popen() returning
            # only means the OS has forked/exec'd the child, not that
            # uvicorn/npm/node has finished importing and reached its own
            # listen() call, which is easily hundreds of ms to a couple of
            # seconds away. cls._lock serializes assignment across start()
            # calls, but it's released right after Popen() returns — before
            # that gap closes — so a second start() call for a *different*
            # project, arriving in that window, can call _find_free_port
            # again, see the same port as free, and be handed the same
            # port. Retry with a fresh port if the child looks like it
            # died immediately (the classic signature of "address already
            # in use" for a CLI dev server that exits on bind failure
            # rather than retrying itself).
            port_attempts_left = 3
            proc = None
            while port_attempts_left > 0:
                port_attempts_left -= 1
                cmd = cls._server_cmd(stack, project_dir, port)
                if not cmd:
                    return None
                try:
                    env = {**os.environ, "PORT": str(port)}
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(project_dir),
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                except Exception as e:
                    UI.warn(f"Preview server failed to start: {e}")
                    return None
                time.sleep(0.3)
                if proc.poll() is None:
                    # Still alive after a brief settle — treat as bound.
                    cls._servers[key] = proc
                    cls._ports[key] = port
                    return f"http://localhost:{port}"
                # Died immediately — most likely lost the port race.
                # Pick a new port (skipping the one that just failed) and
                # retry, rather than silently returning a URL nothing is
                # listening on.
                try:
                    port = _find_free_port(port + 1, 3999)
                except RuntimeError:
                    return None
            UI.warn("Preview server exited immediately on every port attempt.")
            return None

    @classmethod
    def stop(cls, project_dir: Path) -> None:
        key = cls._project_key(project_dir)
        with cls._lock:
            proc = cls._servers.pop(key, None)
            cls._ports.pop(key, None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    @classmethod
    def get_url(cls, project_dir: Path) -> Optional[str]:
        key = cls._project_key(project_dir)
        with cls._lock:
            if key in cls._servers and cls._servers[key].poll() is None:
                return f"http://localhost:{cls._ports.get(key, 8000)}"
        return None

    @classmethod
    def stop_all(cls) -> None:
        with cls._lock:
            keys = list(cls._servers.keys())
        for key in keys:
            proc = cls._servers.pop(key, None)
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

atexit.register(LivePreviewManager.stop_all)


# ── Section G-pre: DeployTool ─────────────────────────────────────────────────

class DeployTool(Tool):
    """Closes the "LivePreviewManager isn't deployment" gap: LivePreviewManager
    only ever gives you localhost. This tool does the two cheapest REAL
    deploy actions rather than another local process:

    1. git_push_github — creates a GitHub repo via the REST API (api.github.com
       is already an allowed egress domain in this environment) and pushes the
       project's current commit to it. This is the "own your code" story
       Emergent/Lovable market, and it's genuinely a few dozen lines, not a
       platform integration.
    2. cli_deploy — shells out to whichever of vercel/netlify/flyctl/railway
       CLI is actually installed and authenticated on this machine, against
       the real project directory. This does NOT vendor API clients for each
       platform — it uses argv-only subprocess the same way RunTool does
       (no shell) and requires the person to have already run that CLI's own
       login flow; this tool never touches or stores a deploy credential.

    Neither of these is optional-feeling scaffolding — both produce a real,
    externally reachable artifact (a GitHub repo URL, or a live deployment
    URL) rather than another localhost process.
    """

    name = "deploy"
    description = (
        "Deploy the project to a real, externally reachable target — a GitHub "
        "repository (git_push_github) or a hosting provider via its official "
        "CLI (cli_deploy: vercel/netlify/flyctl/railway, whichever is installed "
        "and already authenticated on this machine). This is NOT the local "
        "preview server — use generate_app's preview for local dev, use this "
        "when the person wants something they can actually share a URL to."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["git_push_github", "cli_deploy"],
            },
            "repo_name": {
                "type": "string",
                "description": "For git_push_github: repository name to create under the authenticated account.",
            },
            "private": {
                "type": "boolean",
                "description": "For git_push_github: create as a private repo.",
                "default": True,
            },
            "provider": {
                "type": "string",
                "enum": ["vercel", "netlify", "flyctl", "railway"],
                "description": "For cli_deploy: which CLI to invoke. Must already be installed and logged in.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, cwd: Path, config: Dict[str, Any]) -> None:
        super().__init__(cwd)
        self._config = config

    def _github_token(self) -> Optional[str]:
        # Never accept a token as a tool-call argument — that would put a
        # credential in model-visible conversation history/logs. Only read
        # it from the environment, same trust boundary as any other secret
        # this process already has access to.
        return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    def _git_push_github(self, repo_name: Optional[str], private: bool) -> "ToolResult":
        if not HAS_HTTPX:
            return ToolResult("", error="httpx not available — cannot call GitHub API.", is_error=True)
        token = self._github_token()
        if not token:
            return ToolResult(
                "",
                error=(
                    "No GITHUB_TOKEN (or GH_TOKEN) found in the environment. Set one "
                    "with 'repo' scope and re-run — this tool will not accept a token "
                    "as a tool-call argument, only from the environment."
                ),
                is_error=True,
            )
        if not (self.cwd / ".git").exists():
            init = subprocess.run(["git", "init"], cwd=str(self.cwd), capture_output=True, text=True)
            if init.returncode != 0:
                return ToolResult("", error=f"git init failed: {init.stderr}", is_error=True)

        name = repo_name or self.cwd.name
        name = re.sub(r"[^a-zA-Z0-9._-]", "-", name).strip("-") or "generated-app"

        try:
            resp = httpx.post(
                "https://api.github.com/user/repos",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                json={"name": name, "private": bool(private)},
                timeout=20,
            )
        except Exception as e:
            return ToolResult("", error=f"GitHub API request failed: {e}", is_error=True)

        if resp.status_code == 422:
            # Repo likely already exists under this account — fetch it
            # rather than treating "already exists" as a hard failure.
            try:
                who = httpx.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {token}"}, timeout=20,
                ).json()
                login = who.get("login")
                get_resp = httpx.get(
                    f"https://api.github.com/repos/{login}/{name}",
                    headers={"Authorization": f"Bearer {token}"}, timeout=20,
                )
                if get_resp.status_code != 200:
                    return ToolResult("", error=f"Repo create returned 422 and lookup failed: {get_resp.text[:300]}", is_error=True)
                repo_data = get_resp.json()
            except Exception as e:
                return ToolResult("", error=f"Repo may already exist but lookup failed: {e}", is_error=True)
        elif resp.status_code not in (200, 201):
            return ToolResult("", error=f"GitHub repo creation failed ({resp.status_code}): {resp.text[:300]}", is_error=True)
        else:
            repo_data = resp.json()

        clone_url = repo_data.get("clone_url", "")
        html_url = repo_data.get("html_url", "")
        # Embed the token in the push URL for this one push only — never
        # written to .git/config, so it doesn't persist on disk after
        # this call returns.
        auth_url = clone_url.replace("https://", f"https://x-access-token:{token}@") if clone_url else None
        if not auth_url:
            return ToolResult("", error="GitHub API response missing clone_url.", is_error=True)

        steps = [
            ["git", "add", "-A"],
            ["git", "commit", "-m", "Generated by NEON ARCHITECT", "--allow-empty"],
            ["git", "branch", "-M", "main"],
            ["git", "push", auth_url, "main", "--force"],
        ]
        for step in steps:
            r = subprocess.run(step, cwd=str(self.cwd), capture_output=True, text=True, timeout=60)
            if r.returncode != 0 and step[1] != "commit":
                # commit can legitimately fail with "nothing to commit" —
                # not fatal. Any other step failing is.
                return ToolResult(
                    "", error=f"'{' '.join(step[:2])}' failed: {r.stderr[:500]}", is_error=True
                )

        return ToolResult(f"Pushed to GitHub: {html_url}\nClone URL: {clone_url}")

    def _cli_deploy(self, provider: Optional[str]) -> "ToolResult":
        if not provider:
            return ToolResult("", error="provider is required for cli_deploy.", is_error=True)
        binaries = {
            "vercel": (["vercel"], ["--prod", "--yes"]),
            "netlify": (["netlify"], ["deploy", "--prod"]),
            "flyctl": (["flyctl"], ["deploy"]),
            "railway": (["railway"], ["up"]),
        }
        if provider not in binaries:
            return ToolResult("", error=f"Unknown provider {provider!r}.", is_error=True)
        names, argv_tail = binaries[provider]
        resolved = None
        for name in names:
            resolved = shutil.which(name)
            if resolved:
                break
        if not resolved:
            return ToolResult(
                "",
                error=(
                    f"'{provider}' CLI not found on PATH. Install and log in with "
                    f"the provider's own CLI first — this tool deliberately does "
                    f"not vendor an API client or store a deploy credential itself."
                ),
                is_error=True,
            )
        try:
            r = subprocess.run(
                [resolved, *argv_tail], cwd=str(self.cwd), shell=False,
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            return ToolResult("", error=f"{provider} deploy timed out after 300s.", is_error=True)
        output = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            return ToolResult(output[-4000:], error=f"{provider} deploy exited {r.returncode}.", is_error=True)
        return ToolResult(output[-4000:])

    def execute(
        self,
        action: str = "",
        repo_name: Optional[str] = None,
        private: bool = True,
        provider: Optional[str] = None,
    ) -> "ToolResult":
        if action == "git_push_github":
            return self._git_push_github(repo_name, private)
        elif action == "cli_deploy":
            return self._cli_deploy(provider)
        return ToolResult("", error=f"Unknown action {action!r}.", is_error=True)


# ── Section G: generate_app Tool ─────────────────────────────────────────────

class GenerateAppTool(Tool):
    """Tool the autonomous agent can call during the implementation phase.

    Scaffolds a complete full-stack project from a natural-language spec,
    writes all files to project_dir, and starts a live preview server.
    The result is reported back to the agent as a structured tool result.
    """
    name = "generate_app"
    description = (
        "Scaffold and generate a complete full-stack application from a natural-language spec. "
        "Writes all project files (frontend, backend, docker-compose, tests, CI) and starts a live preview. "
        "Use during the implementation phase to bootstrap a working app in one step."
    )
    parameters = {
        "type": "object",
        "properties": {
            "spec": {
                "type": "string",
                "description": "Natural-language description of the application to build.",
            },
            "stack": {
                "type": "string",
                "enum": ["fastapi-react", "nextjs-postgres", "flutter-fastapi", "expo-node", "auto"],
                "description": "Tech stack to use. 'auto' detects from spec keywords.",
                "default": "auto",
            },
            "project_name": {
                "type": "string",
                "description": "Project name (slug). Defaults to the project directory name.",
            },
            "start_preview": {
                "type": "boolean",
                "description": "Whether to start the live preview server after generation.",
                "default": True,
            },
        },
        "required": ["spec"],
    }

    def __init__(self, cwd: Path, pool: "ProviderPool", config: Dict[str, Any]) -> None:
        super().__init__(cwd)
        self._pool = pool
        self._config = config

    def execute(
        self,
        spec: str,
        stack: str = "auto",
        project_name: Optional[str] = None,
        start_preview: bool = True,
    ) -> "ToolResult":
        _KNOWN_STACKS = ("fastapi-react", "nextjs-postgres", "flutter-fastapi", "expo-node")
        if stack == "auto":
            chosen_stack = _detect_stack(spec)
        elif stack in _KNOWN_STACKS:
            chosen_stack = stack
        else:
            return ToolResult(
                "",
                error=(
                    f"Unsupported stack {stack!r}. "
                    f"Use one of: {', '.join(_KNOWN_STACKS)} or 'auto'."
                ),
                is_error=True,
            )
        project_dir = self.cwd
        if project_name:
            # project_name is model-controlled tool-call input, same as any
            # path argument to read/write/edit — but unlike those tools,
            # this one built the path with a plain self.cwd / project_name
            # join and no resolution check, so a value like
            # "../../../../tmp/evil" resolved straight through to a
            # directory outside the project sandbox, and everything this
            # tool goes on to write (scaffold files, every agent's
            # generated files) would land there instead of under cwd.
            # Route it through the same _sanitize_rel_path +
            # relative_to(cwd) check every other tool in this file already
            # trusts, instead of trusting it as a bare path segment.
            safe_name = self._sanitize_rel_path(project_name)
            if not safe_name:
                return ToolResult(
                    f"Rejected project_name {project_name!r}: must be a plain "
                    "folder name, not empty or a reserved word.",
                    is_error=True,
                )
            candidate = (self.cwd / safe_name).resolve()
            try:
                candidate.relative_to(self.cwd.resolve())
            except ValueError:
                return ToolResult(
                    f"Rejected project_name {project_name!r}: resolves outside "
                    f"the project folder ({self.cwd}). Use a plain subfolder "
                    "name, not a path with '..' segments.",
                    is_error=True,
                )
            project_dir = candidate
            project_dir.mkdir(parents=True, exist_ok=True)

        progress_lines: List[str] = []

        def on_progress(phase: str, msg: str) -> None:
            progress_lines.append(f"[{phase}] {msg}")
            UI.info(f"  gen/{phase}: {msg}")

        orch = GenerationOrchestrator(self._pool, self._config)
        result = orch.generate(
            description=spec,
            project_dir=project_dir,
            stack=chosen_stack,
            on_progress=on_progress,
        )

        preview_url: Optional[str] = None
        if start_preview and result.success:
            preview_url = LivePreviewManager.start(project_dir, chosen_stack)
            if preview_url:
                result.preview_url = preview_url

        summary = (
            f"Generation {'succeeded' if result.success else 'completed with errors'}.\n"
            f"Stack: {chosen_stack}\n"
            f"Template files: {len(result.files_created)}\n"
            f"Agent-generated files: {len(result.files_generated)}\n"
            f"Preview: {preview_url or 'not started'}\n"
            f"Errors: {len(result.errors)}\n"
        )
        if result.errors:
            summary += "Error log:\n" + "\n".join(f"  - {e}" for e in result.errors[:5])
        if result.files_generated:
            summary += "\nGenerated files:\n" + "\n".join(f"  {f}" for f in result.files_generated[:20])

        return ToolResult(summary, is_error=not result.success)


# ── Section F: /generate command handler ──────────────────────────────────────

def _cmd_generate(raw: str, agent: "NeonArchitect") -> None:
    """Handle the /generate [stack] [description] slash command from the REPL loop."""
    parts = raw.split(None, 2)
    explicit_stack: Optional[str] = None
    inline_desc: str = ""

    if len(parts) >= 2:
        if parts[1] in _T:
            explicit_stack = parts[1]
            if len(parts) >= 3:
                inline_desc = parts[2].strip()
        else:
            inline_desc = raw[len("/generate"):].strip()

    desc = inline_desc
    if not desc:
        if HAS_RICH:
            _console = Console(theme=THEME)
            _console.print(
                "\n[cc_primary_bold]  ◆  App Generator[/cc_primary_bold]  "
                "[cc_muted]— describe what you want to build[/cc_muted]\n"
            )
            desc = _console.input("[cc_prompt]  Description ❯  [/cc_prompt]").strip()
        else:
            print("\n  ◆  App Generator — describe what you want to build")
            desc = input("  Description > ").strip()

    if not desc:
        UI.warn("No description provided — /generate cancelled.")
        return

    stack = explicit_stack or _detect_stack(desc)
    UI.info(f"Stack: {stack}  |  Project dir: {agent.project_dir}")
    UI.info(f"Scaffolding {stack} template then running NIM agents…\n")

    def on_progress(phase: str, msg: str) -> None:
        if "✓" in msg or "✗" in msg:
            (UI.ok if "✓" in msg else UI.err)(f"  {phase}: {msg}")
        else:
            UI.info(f"  {phase}: {msg}")

    orch = GenerationOrchestrator(agent.pool, agent.config)
    result = orch.generate(
        description=desc,
        project_dir=agent.project_dir,
        stack=stack,
        on_progress=on_progress,
    )

    UI.separator()
    if result.success:
        UI.ok(f"Generation complete!  {len(result.files_created)} template + {len(result.files_generated)} AI-generated files.")
    else:
        UI.warn(f"Generation completed with {len(result.errors)} error(s).")

    # Start preview
    UI.info("Starting live preview server…")
    preview_url = LivePreviewManager.start(agent.project_dir, stack)
    if preview_url:
        UI.ok(f"Preview: {preview_url}")
        UI.info("  (The server is starting in the background. Allow 5–15 s for first load.)")
    else:
        UI.warn("Preview server could not be started — run manually: see README.md")

    # Auto-set goal if not already set
    if not agent.goal:
        agent.cmd_goal(
            f"Build '{agent.project_dir.name}': {desc[:200]}. "
            f"The {stack} scaffold is already in place — augment it, fix errors, "
            "add real NIM-powered features, and verify the full stack works."
        )
        UI.info("Goal set automatically from your description. Run /autopilot to continue building.")

    UI.separator()


# ── Patch /generate into the existing Tool registry ───────────────────────────
# We register GenerateAppTool so the autonomous agent can call it during
# the implementation phase via its normal tool-call flow.

_GENERATION_LAYER_READY = True  # sentinel for tests / introspection


if __name__ == "__main__":
    main()