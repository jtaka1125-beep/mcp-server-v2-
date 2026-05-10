"""Layer 1 boundary verification helpers per entry 5ecbed0b.

Crossing a process boundary (claude CLI, recorder_v2 subprocess, gemini_router
helper, etc) means exit_code=0 alone is **not** proof of success. This module
provides reusable verification helpers that catch:

  1. Silent failures (exit=0 with empty stdout)
  2. Node loader crashes (Error: / node:internal/ in stderr)
  3. Workspace-trust prompt leaks (defense-in-depth for claude CLI)
  4. Python tracebacks (Traceback (most recent call last) in stderr)

The output is a `(ok: bool, anomaly_reason: str | None)` tuple. Callers map
`ok=False` to whatever failure envelope is appropriate for their context
(e.g. dispatcher.Result.error, JSON status response, etc).

Source of truth: memory entry 5ecbed0b (mirage-design, "境界確認原則 7 枚目").
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Default anomaly patterns (Claude CLI focus, also catches Python tracebacks)
# ---------------------------------------------------------------------------

# Patterns to scan in stderr (most reliable signal for crashes)
DEFAULT_STDERR_PATTERNS: Tuple[str, ...] = (
    "node:internal/",       # Node.js loader crash, very strong signal
    "Error: Cannot find",   # Module resolution failure
    "MODULE_NOT_FOUND",     # Node module miss
    "Traceback (most ",     # Python traceback header
    "TypeError:",           # Common Python/JS runtime error
    "SyntaxError:",         # Parse error
    "ReferenceError:",      # JS-specific
)

# Patterns to scan in stdout+stderr combined (defense-in-depth, lower confidence)
# These are typed-out Claude CLI prompts that should never reach a -p / --print
# completion. If they do, the workspace-trust skip / dangerously-skip flags
# regressed.
DEFAULT_PROMPT_LEAK_PATTERNS: Tuple[str, ...] = (
    "Do you trust the files",       # workspace-trust prompt (en)
    "このフォルダ内のファイル",      # workspace-trust prompt (ja)
    "Bypass permissions on",        # permission warning prompt
)


# ---------------------------------------------------------------------------
# Core verifier
# ---------------------------------------------------------------------------

def verify_claude_output(
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    require_nonempty_stdout: bool = True,
    stderr_patterns: Optional[Sequence[str]] = None,
    prompt_leak_patterns: Optional[Sequence[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Layer 1 verification for a claude CLI subprocess result.

    Returns (ok, anomaly_reason).

      ok=True, reason=None       output looks structurally healthy
      ok=False, reason=<key>     anomaly detected; <key> is a short tag
                                 like 'silent_failure_empty_stdout' or
                                 'node_error_detected:node:internal/'

    Args:
      returncode: process exit code
      stdout:     captured stdout (text)
      stderr:     captured stderr (text)
      require_nonempty_stdout:
                  if True (default), exit=0 with empty stdout is treated as
                  a silent failure. Set False for tools that legitimately
                  produce no stdout on success.
      stderr_patterns:
                  override DEFAULT_STDERR_PATTERNS. Pass () to disable
                  stderr scanning entirely.
      prompt_leak_patterns:
                  override DEFAULT_PROMPT_LEAK_PATTERNS. Pass () to disable.

    Note: this helper does NOT inspect returncode != 0; the caller is expected
    to check that separately and combine the two signals (anomaly OR nonzero
    exit -> failure).
    """
    stdout = stdout or ""
    stderr = stderr or ""

    # 1. silent failure: exit=0 but no stdout
    if require_nonempty_stdout and returncode == 0 and not stdout.strip():
        return False, "silent_failure_empty_stdout"

    # 2. stderr crash patterns (node loader / python traceback / etc)
    patterns = stderr_patterns if stderr_patterns is not None else DEFAULT_STDERR_PATTERNS
    for needle in patterns:
        if needle and needle in stderr:
            return False, f"stderr_crash_pattern:{needle}"

    # 3. workspace-trust / permission-prompt leakage (defense-in-depth)
    leak_patterns = (
        prompt_leak_patterns
        if prompt_leak_patterns is not None
        else DEFAULT_PROMPT_LEAK_PATTERNS
    )
    if leak_patterns:
        combined = stdout + "\n" + stderr
        for needle in leak_patterns:
            if needle and needle in combined:
                return False, f"prompt_leak:{needle}"

    return True, None


# ---------------------------------------------------------------------------
# Envelope helper
# ---------------------------------------------------------------------------

def to_anomaly_envelope(
    reason: str,
    *,
    returncode: int = 0,
    stdout_preview: str = "",
    stderr_preview: str = "",
    stage: str = "subprocess",
    preview_chars: int = 200,
) -> dict:
    """Build a JSON-friendly anomaly record for downstream consumers.

    Use this when verify_claude_output returns ok=False and the caller wants
    to surface the failure as a structured payload (e.g. step6 escalation
    error envelope, run_task Result.output JSON, etc).
    """
    return {
        "status": "anomaly",
        "stage": stage,
        "reason": reason,
        "returncode": returncode,
        "stdout_preview": (stdout_preview or "")[:preview_chars],
        "stderr_preview": (stderr_preview or "")[:preview_chars],
    }


# ---------------------------------------------------------------------------
# Self-test (run as `py boundary.py`)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Quick smoke check
    cases = [
        # (returncode, stdout, stderr, expect_ok)
        (0, "hello world\n",  "",                                                True),
        (0, "",                "",                                                False),  # silent
        (0, "ok",              "node:internal/modules/cjs/loader",                False),  # node crash
        (0, "ok",              "Traceback (most recent call last):\n  File ...",  False),  # py traceback
        (0, "Do you trust the files in this workspace?", "",                      False),  # trust leak
        (1, "partial output",  "some warning",                                    True),   # nonzero exit not our job
    ]
    for rc, so, se, expect in cases:
        ok, reason = verify_claude_output(rc, so, se)
        marker = "OK" if ok == expect else "FAIL"
        print(f"[{marker}] rc={rc} expect_ok={expect} got_ok={ok} reason={reason!r}")
