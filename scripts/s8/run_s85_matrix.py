from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from check_baseline import REPO, isolated_env

Classification = Literal["automatic_recovery", "safe_pause", "terminal_failure"]


@dataclass(frozen=True)
class MatrixCase:
    case_id: str
    category: str
    injection: str
    nodeid: str
    expected: Classification
    observable: str


CASES = [
    MatrixCase(
        "intent-commit-before", "pre_dispatch", "commit_response before commit",
        "tests/unit/test_s82_execution.py::test_intent_commit_failure_prevents_tool_execution",
        "terminal_failure", "storage error; tool calls=0; marker absent",
    ),
    MatrixCase(
        "dispatch-commit-before", "pre_dispatch", "start_attempt before commit",
        "tests/unit/test_s82_execution.py::test_dispatch_commit_failure_prevents_side_effect",
        "terminal_failure", "storage error; attempts=0; marker absent",
    ),
    MatrixCase(
        "result-commit-before", "unknown_result", "commit_result before commit",
        "tests/unit/test_s82_execution.py::test_result_commit_failure_stops_after_one_side_effect",
        "safe_pause", "one side effect; dispatching attempt; no continuation",
    ),
    MatrixCase(
        "committed-result-resume", "committed_result", "kill at second model entry",
        "tests/integration/test_s83_recovery.py::test_recovery_reuses_committed_result_and_request_identity",
        "automatic_recovery", "explicit resume; one claim; counter=1; run succeeded",
    ),
    MatrixCase(
        "unknown-result-restart", "unknown_result", "kill before result commit",
        "tests/integration/test_s83_recovery.py::test_unknown_result_stays_reviewable_without_replay",
        "safe_pause", "needs_review; call unknown; counter remains 1",
    ),
    MatrixCase(
        "final-commit-before", "final_boundary", "final commit before commit",
        "tests/unit/test_s82_execution.py::test_final_response_commit_failure_does_not_report_success",
        "terminal_failure", "no succeeded state and no success event",
    ),
    MatrixCase(
        "concurrent-resume", "concurrency", "two clients resume one run",
        "tests/integration/test_s83_recovery.py::test_recovery_reuses_committed_result_and_request_identity",
        "automatic_recovery", "exactly one started claim and one tool attempt",
    ),
    MatrixCase(
        "approval-restart", "approval", "kill after one-time approval before dispatch",
        "tests/integration/test_s83_recovery.py::test_restart_invalidates_one_time_approval",
        "safe_pause", "old token rejected; new approval required",
    ),
    MatrixCase(
        "workspace-mismatch", "workspace", "resume from a different workspace",
        "tests/integration/test_s83_recovery.py::test_workspace_mismatch_blocks_unstarted_write",
        "safe_pause", "no attempt and no write",
    ),
    MatrixCase(
        "workspace-file-change", "workspace", "related file changed before resume",
        "tests/integration/test_s83_recovery.py::test_workspace_file_change_blocks_write",
        "safe_pause", "needs_review; original external edit preserved",
    ),
    MatrixCase(
        "child-interrupted", "child_run", "restart with active child",
        "tests/integration/test_s83_recovery.py::test_background_child_relationship_survives_restart",
        "safe_pause", "parent-child relation retained; child not auto-run",
    ),
    MatrixCase(
        "child-result-query", "child_run", "restart after child completion",
        "tests/integration/test_s83_recovery.py::test_completed_child_result_is_queryable_across_runner",
        "automatic_recovery", "new runner reuses persisted child result",
    ),
    MatrixCase(
        "summary-insert-fault", "compaction", "after summary insert before commit",
        "tests/unit/test_s84_compaction.py::test_summary_insert_fault_rolls_back_pointer",
        "safe_pause", "summary and active pointer both roll back",
    ),
    MatrixCase(
        "summary-pointer-fault", "compaction", "after pointer update before commit",
        "tests/unit/test_s84_compaction.py::test_checkpoint_switch_fault_rolls_back_summary",
        "safe_pause", "old summary remains effective",
    ),
    MatrixCase(
        "summary-generation-fault", "compaction", "fake provider raises",
        "tests/unit/test_s84_compaction.py::test_generation_failure_preserves_effective_state",
        "safe_pause", "raw history and previous context remain authoritative",
    ),
    MatrixCase(
        "summary-concurrent-tail", "compaction", "append behind provider barrier",
        "tests/unit/test_s84_compaction.py::test_concurrent_messages_remain_after_snapshot",
        "automatic_recovery", "new tail remains outside committed summary range",
    ),
    MatrixCase(
        "second-daemon", "daemon_ownership", "same profile on a second port",
        "tests/integration/test_s83_recovery.py::test_second_daemon_same_profile_is_rejected",
        "terminal_failure", "second daemon exits; first retains ownership",
    ),
    MatrixCase(
        "tui-success", "ui", "TUI Continue after committed-result restart",
        "tests/integration/test_s83_tui_live.py::test_live_tui_resumes_committed_run_after_restart",
        "automatic_recovery", "explicit Continue reaches succeeded without replay",
    ),
    MatrixCase(
        "tui-unknown-pause", "ui", "TUI Pause after unknown-result restart",
        "tests/integration/test_s83_tui_live.py::test_live_tui_pauses_unknown_result_with_human_note",
        "safe_pause", "needs_review retained with operator note",
    ),
    MatrixCase(
        "summary-daemon-restart", "compaction", "stop and restart after manual compact",
        "tests/integration/test_s84_compaction.py::test_daemon_restart_reuses_persisted_manual_summary",
        "automatic_recovery", "next run receives summary plus uncovered tail",
    ),
]


# 返回 XML 中每个 pytest 用例名对应的执行状态和耗时
def _junit_results(path: Path) -> dict[str, tuple[str, float]]:
    results: dict[str, tuple[str, float]] = {}
    root = ET.parse(path).getroot()
    for node in root.iter("testcase"):
        name = str(node.attrib.get("name", ""))
        status = "passed"
        if node.find("failure") is not None:
            status = "failed"
        elif node.find("error") is not None:
            status = "error"
        elif node.find("skipped") is not None:
            status = "skipped"
        results[name] = (status, float(node.attrib.get("time", "0")))
    return results


# 核对生产源码没有可由配置或环境直接启用的 S8 故障入口
def _audit_fault_hooks() -> dict[str, object]:
    forbidden = ("KAMA_S8_FAULT", "KAMA_S83_TEST_SCENARIO", "S8_FAULT_INJECTION")
    hits: list[str] = []
    for path in (REPO / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            if marker in text:
                hits.append(f"{path.relative_to(REPO)}:{marker}")
    execution = (REPO / "src/kama_claude/core/session/execution.py").read_text(
        encoding="utf-8"
    )
    private_defaults = all(
        marker in execution
        for marker in (
            "self._before_commit: Callable[[], None] = lambda: None",
            "self._after_summary_insert: Callable[[], None] = lambda: None",
        )
    )
    return {
        "passed": not hits and private_defaults,
        "environment_or_config_hits": hits,
        "private_callbacks_default_to_noop": private_defaults,
        "scope": "static audit of src/**/*.py; crash/barrier activation remains in tests/helpers and scripts/s8",
    }


# 运行统一 pytest 集并将每个场景映射为可审阅的三分类结果
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=360)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    unique_nodeids = list(dict.fromkeys(case.nodeid for case in CASES))
    with tempfile.TemporaryDirectory(prefix="kama-s85-matrix-") as folder:
        scratch = Path(folder)
        junit = scratch / "matrix.xml"
        env = isolated_env(scratch)
        command = [
            sys.executable, "-m", "pytest", *unique_nodeids, "-v", "--tb=short",
            "--junitxml", str(junit), "--basetemp", str(scratch / "pytest"),
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command, cwd=REPO, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=args.timeout,
            )
            return_code = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            return_code = 124
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            stderr += "\nS8.5 matrix timeout.\n"
        elapsed = time.monotonic() - started
        (output / "pytest.txt").write_text(stdout + stderr, encoding="utf-8")
        parsed = _junit_results(junit) if junit.exists() else {}

    rows: list[dict[str, object]] = []
    for case in CASES:
        test_name = case.nodeid.rsplit("::", 1)[-1]
        status, seconds = parsed.get(test_name, ("missing", 0.0))
        row = asdict(case)
        row.update({
            "test_status": status,
            "test_seconds": seconds,
            "actual": case.expected if status == "passed" else "test_failure",
            "evidence": "pytest.txt",
        })
        rows.append(row)
    passed_rows = [row for row in rows if row["test_status"] == "passed"]
    counts = Counter(str(row["actual"]) for row in passed_rows)
    hook_audit = _audit_fault_hooks()
    result = {
        "schema_version": 1,
        "source": {
            "head": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
            ).stdout.strip(),
            "worktree": str(REPO),
        },
        "isolation": {
            "api_keys_inherited": False,
            "dotenv_disabled": True,
            "real_api_test_excluded": True,
            "temporary_profile": True,
        },
        "semantics": {
            "automatic_recovery": "after an explicit resume request, no operator fact judgment is needed",
            "safe_pause": "execution does not replay and remains blocked or preserves the old authoritative state",
            "terminal_failure": "the attempted operation or competing daemon terminates without reporting success",
            "unattended_restart_resume": "not implemented; 0 scenarios claim unattended continuation",
        },
        "command": command,
        "pytest_exit_code": return_code,
        "seconds": round(elapsed, 3),
        "attempted": len(rows),
        "passed": len(passed_rows),
        "test_failures": len(rows) - len(passed_rows),
        "classification_denominators": dict(sorted(Counter(case.expected for case in CASES).items())),
        "classification_observed": dict(sorted(counts.items())),
        "fault_hook_audit": hook_audit,
        "cases": rows,
    }
    (output / "matrix.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "attempted": result["attempted"], "passed": result["passed"],
        "classifications": result["classification_observed"],
        "fault_hook_audit": hook_audit["passed"], "output": str(output),
    }, ensure_ascii=False))
    return int(return_code != 0 or len(passed_rows) != len(rows) or not hook_audit["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
