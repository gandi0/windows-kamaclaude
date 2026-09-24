from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
BASELINE_COMMIT = "cc352fe653c26ab763838899785b6bdd6575f0c0"


# 返回目录下主文件、WAL 和其他工作负载产物的字节统计
def _sizes(root: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for path in root.rglob("*"):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.stat().st_size
    result["__total__"] = sum(result.values())
    return result


# 按项目现有字符除以四口径统计上下文，不冒充供应商 tokenizer
def _context_metrics(messages: list[dict[str, Any]]) -> dict[str, int]:
    encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    characters = sum(len(str(message.get("content", ""))) for message in messages)
    return {
        "messages": len(messages),
        "content_characters": characters,
        "canonical_json_bytes": len(encoded),
        "estimated_tokens_char_div_4": characters // 4,
    }


# 构造不包含真实密钥且不加载 dotenv 的 benchmark 子进程环境
def _worker_env(source_root: Path, scratch: Path) -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    profile = scratch / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    env.update({
        "USERPROFILE": str(profile),
        "APPDATA": str(profile / "AppData/Roaming"),
        "LOCALAPPDATA": str(profile / "AppData/Local"),
        "PYTHONPATH": str(source_root / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_DOTENV_DISABLED": "1",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_AUTH_TOKEN": "",
        "KAMA_CONFIG": str(scratch / "isolated-config.toml"),
    })
    return env


# 创建各版本都能接受的固定 session 元数据
def _session(sid: str) -> Any:
    from kama_claude.core.session.model import Session

    return Session(sid, "chat", "waiting_for_input", "benchmark", "t", "t", [])


# 运行两版本共享的消息持久化、读取和压缩提交 workload
def _storage_worker(
    variant: str, samples: int, warmup: int, message_count: int,
) -> dict[str, Any]:
    from kama_claude.core.session.store import SessionStore

    payload = "fixed-message-" + ("x" * 240)
    persistence: list[dict[str, Any]] = []
    compaction: list[dict[str, Any]] = []
    contexts: dict[str, Any] | None = None
    for index in range(samples + warmup):
        with tempfile.TemporaryDirectory(prefix=f"kama-s85-{variant}-") as folder:
            root = Path(folder)
            workspace = root / "workspace"
            workspace.mkdir()
            store = SessionStore(root / "sessions")
            sid = "benchmark-session"
            if hasattr(store, "execution"):
                store.write_meta(_session(sid), workspace=str(workspace))
            else:
                store.write_meta(_session(sid))
            started = time.perf_counter_ns()
            for number in range(message_count):
                store.append_message(
                    sid, "user" if number % 2 == 0 else "assistant",
                    f"{payload}-{number:04d}", "benchmark-run",
                )
            append_ns = time.perf_counter_ns() - started
            started = time.perf_counter_ns()
            original = store.read_messages(sid)
            read_ns = time.perf_counter_ns() - started
            assert len(original) == message_count
            size_before_compaction = _sizes(root / "sessions")

            summary_messages = [
                {"role": "user", "content": "fixed deterministic summary"},
                {"role": "assistant", "content": "Understood, I'll continue."},
            ]
            if hasattr(store, "execution"):
                store.close()
                store = SessionStore(root / "compact-sessions")
                store.write_meta(_session(sid), workspace=str(workspace))
                run_id = store.execution.begin_run(
                    sid, "compact-run", workspace=str(workspace),
                    user_content=f"{payload}-0000",
                )
                for number in range(1, message_count):
                    store.execution.append_message(
                        sid, "user" if number % 2 == 0 else "assistant",
                        f"{payload}-{number:04d}", run_id=run_id,
                    )
                snapshot = store.capture_compaction(sid, run_id)
                started = time.perf_counter_ns()
                store.commit_compaction(
                    sid, run_id, snapshot, "fixed deterministic summary",
                    generation_config_version="s8.5-benchmark-v1",
                    generation_config={"provider": "fixed", "temperature": 0},
                )
                compact_ns = time.perf_counter_ns() - started
                if index == warmup:
                    contexts = {
                        "raw": _context_metrics(store.read_raw_messages(sid)),
                        "effective": _context_metrics(store.read_messages(sid)),
                    }
            else:
                started = time.perf_counter_ns()
                store.write_compacted(sid, summary_messages)
                compact_ns = time.perf_counter_ns() - started
            size_after_compaction = _sizes(root)
            if hasattr(store, "close"):
                store.close()
            if index >= warmup:
                persistence.append({
                    "sample": index - warmup + 1,
                    "append_ns": append_ns,
                    "read_ns": read_ns,
                    "bytes_before_compaction": size_before_compaction["__total__"],
                })
                compaction.append({
                    "sample": index - warmup + 1,
                    "commit_ns": compact_ns,
                    "bytes_after_compaction": size_after_compaction["__total__"],
                })
    return {
        "variant": variant,
        "workload": {"messages": message_count, "payload_characters": len(payload)},
        "persistence_samples": persistence,
        "compaction_commit_samples": compaction,
        "context": contexts,
    }


# 测量固定假 provider 的摘要生成路径，不包含数据库提交
async def _generation_samples(samples: int, warmup: int, message_count: int) -> list[dict[str, int]]:
    from kama_claude.core.compact.compactor import Compactor
    from kama_claude.core.events.bus import EventBus
    from kama_claude.core.llm.types import LlmResponse

    class FixedProvider:
        # 固定返回同一摘要，避免网络和模型随机性进入测量
        async def chat(self, **kwargs: Any) -> LlmResponse:
            return LlmResponse("end_turn", text="fixed deterministic summary")

    messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 256}
        for index in range(message_count)
    ]
    rows: list[dict[str, int]] = []
    with tempfile.TemporaryDirectory(prefix="kama-s85-generation-") as folder:
        compactor = Compactor(EventBus(), Path(folder), "benchmark-session")
        for index in range(samples + warmup):
            started = time.perf_counter_ns()
            result = await compactor.compact_messages(messages, FixedProvider())
            elapsed = time.perf_counter_ns() - started
            assert result is not None and result.summary_text == "fixed deterministic summary"
            if index >= warmup:
                rows.append({"sample": index - warmup + 1, "provider_generation_ns": elapsed})
    return rows


# 测量 S8 重启扫描、显式认领、继续执行和状态可查询边界
async def _recovery_samples(samples: int, warmup: int) -> list[dict[str, int]]:
    from kama_claude.core.config import KamaConfig
    from kama_claude.core.events.bus import EventBus
    from kama_claude.core.llm.types import LlmResponse
    from kama_claude.core.runner import AgentRunner
    from kama_claude.core.session.manager import SessionManager
    from kama_claude.core.session.store import SessionStore

    class FixedProvider:
        # 恢复后直接给出固定最终回复，不调用工具或外部服务
        async def chat(self, **kwargs: Any) -> LlmResponse:
            return LlmResponse("end_turn", text="recovered")

    rows: list[dict[str, int]] = []
    for index in range(samples + warmup):
        with tempfile.TemporaryDirectory(prefix="kama-s85-recovery-") as folder:
            root = Path(folder)
            workspace = root / "workspace"
            workspace.mkdir()
            session_root = root / "sessions"
            old = SessionStore(session_root)
            old.execution.start_daemon("old-epoch")
            old.write_meta(_session("s1"), workspace=str(workspace))
            old.execution.begin_run(
                "s1", "r1", workspace=str(workspace), user_content="fixed request",
                owner_epoch="old-epoch", execution_config={
                    "goal": "fixed request", "system_prompt_override": None,
                    "tool_whitelist": None,
                },
            )
            old.close()

            started = time.perf_counter_ns()
            current = SessionStore(session_root)
            opened = time.perf_counter_ns()
            current.execution.start_daemon("new-epoch")
            scanned = time.perf_counter_ns()
            bus = EventBus()
            manager = SessionManager(
                current,
                lambda: AgentRunner(KamaConfig(), bus=bus, provider=FixedProvider()),
                bus,
                epoch="new-epoch",
            )
            indexed = time.perf_counter_ns()
            result = await manager.resume("s1", "r1", str(workspace))
            claimed = time.perf_counter_ns()
            assert result["started"] is True
            task = manager._tasks["r1"]
            await asyncio.shield(task)
            completed = time.perf_counter_ns()
            status = manager.status("s1")
            query_ready = time.perf_counter_ns()
            assert status["session"]["run_status"] == "succeeded"
            await manager.shutdown()
            current.close()
            if index >= warmup:
                rows.append({
                    "sample": index - warmup + 1,
                    "open_ns": opened - started,
                    "scan_ns": scanned - opened,
                    "index_ns": indexed - scanned,
                    "claim_ns": claimed - indexed,
                    "continue_to_terminal_ns": completed - claimed,
                    "status_query_ns": query_ready - completed,
                    "restart_to_query_ready_ns": query_ready - started,
                })
    return rows


# 记录包含调用、attempt、消息和一个摘要的 SQLite 分阶段增长
def _database_growth(message_count: int) -> dict[str, Any]:
    from kama_claude.core.session.store import SessionStore
    from kama_claude.core.tools.base import ToolAttempt, ToolResult

    with tempfile.TemporaryDirectory(prefix="kama-s85-growth-") as folder:
        root = Path(folder)
        workspace = root / "workspace"
        workspace.mkdir()
        store = SessionStore(root / "sessions")
        initial_wal_live = _sizes(root / "sessions")
        initial_checkpoint = store.execution._conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        initial_checkpointed = _sizes(root / "sessions")
        store.write_meta(_session("s1"), workspace=str(workspace))
        run_id = store.execution.begin_run(
            "s1", "r1", workspace=str(workspace), user_content="fixed request"
        )
        after_run = _sizes(root / "sessions")
        tool_count = max(1, message_count // 10)
        for number in range(tool_count):
            step = number + 1
            tool_use_id = f"tool-{number}"
            call_id = f"call-{number}"
            attempt_id = f"attempt-{number}"
            block = {"type": "tool_use", "id": tool_use_id, "name": "list_dir", "input": {}}
            store.execution.commit_response(
                run_id, step, [block], [{
                    "call_id": call_id, "tool_use_id": tool_use_id,
                    "name": "list_dir", "input": {}, "effect": "read_only",
                    "retry_safe": True,
                }],
            )
            store.execution.start_attempt(call_id, attempt_id, 1)
            store.execution.commit_result(
                call_id,
                ToolResult(
                    content=f"fixed-result-{number}", call_id=call_id,
                    attempts=[ToolAttempt(attempt_id, 1, "known", None)],
                ),
            )
        store.execution.commit_response(
            run_id, tool_count + 1, [{"type": "text", "text": "fixed final"}], [], final=True
        )
        after_workload = _sizes(root / "sessions")
        snapshot = store.capture_compaction("s1", run_id)
        store.commit_compaction(
            "s1", run_id, snapshot, "fixed deterministic summary",
            generation_config_version="s8.5-benchmark-v1",
            generation_config={"provider": "fixed", "temperature": 0},
        )
        before_checkpoint = _sizes(root / "sessions")
        checkpoint = store.execution._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        after_checkpoint = _sizes(root / "sessions")
        counts = {
            table: int(store.execution._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("messages", "tool_calls", "attempts", "summaries")
        }
        store.close()
        return {
            "configured_tool_calls": tool_count,
            "row_counts": counts,
            "initial_schema_wal_live": initial_wal_live,
            "initial_wal_checkpoint_result": (
                list(initial_checkpoint) if initial_checkpoint is not None else None
            ),
            "initial_schema_checkpointed": initial_checkpointed,
            "after_run": after_run,
            "after_workload_wal_live": after_workload,
            "after_summary_wal_live": before_checkpoint,
            "wal_checkpoint_result": list(checkpoint) if checkpoint is not None else None,
            "after_wal_checkpoint_truncate": after_checkpoint,
        }


# 执行单一源码版本的原始 workload 并向父进程输出 JSON
def _worker_main(args: argparse.Namespace) -> int:
    result = _storage_worker(args.variant, args.samples, args.warmup, args.messages)
    result["provider_generation_samples"] = asyncio.run(
        _generation_samples(args.samples, args.warmup, args.messages)
    )
    if args.variant == "s8":
        result["recovery_samples"] = asyncio.run(_recovery_samples(args.samples, args.warmup))
        result["database_growth"] = _database_growth(args.messages)
    print(json.dumps(result, ensure_ascii=False))
    return 0


# 汇总一组纳秒样本，保留中位数、p95、极值和样本数
def _stats(values: list[int]) -> dict[str, float | int]:
    ordered = sorted(values)
    p95_index = max(0, min(len(ordered) - 1, (95 * len(ordered) + 99) // 100 - 1))
    return {
        "n": len(values),
        "median_ns": int(statistics.median(values)),
        "p95_ns": ordered[p95_index],
        "min_ns": ordered[0],
        "max_ns": ordered[-1],
    }


# 从原始结果生成不带性能价值判断的统计摘要
def _summarize(raw: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"sample_count": raw["method"]["samples"]}
    for variant in ("baseline", "s8"):
        data = raw[variant]
        summary[variant] = {
            "append": _stats([row["append_ns"] for row in data["persistence_samples"]]),
            "read": _stats([row["read_ns"] for row in data["persistence_samples"]]),
            "compaction_commit": _stats([
                row["commit_ns"] for row in data["compaction_commit_samples"]
            ]),
            "fake_provider_generation": _stats([
                row["provider_generation_ns"] for row in data["provider_generation_samples"]
            ]),
            "persistence_bytes": {
                "n": len(data["persistence_samples"]),
                "median": int(statistics.median(
                    row["bytes_before_compaction"] for row in data["persistence_samples"]
                )),
                "min": min(row["bytes_before_compaction"] for row in data["persistence_samples"]),
                "max": max(row["bytes_before_compaction"] for row in data["persistence_samples"]),
            },
        }
    baseline_median = summary["baseline"]["append"]["median_ns"]
    s8_median = summary["s8"]["append"]["median_ns"]
    summary["persistence_comparison"] = {
        "s8_minus_baseline_median_ns": s8_median - baseline_median,
        "s8_over_baseline_median_ratio": s8_median / baseline_median,
        "warning": "The stores provide different durability and recovery semantics; this ratio is not an end-to-end throughput claim.",
    }
    summary["s8"]["recovery"] = {
        key: _stats([row[key] for row in raw["s8"]["recovery_samples"]])
        for key in raw["s8"]["recovery_samples"][0]
        if key != "sample"
    }
    summary["context"] = raw["s8"]["context"]
    summary["database_growth"] = raw["s8"]["database_growth"]
    return summary


# 驱动隔离的原版与 S8 子进程并保存所有原始样本
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=15)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--messages", type=int, default=200)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--variant", choices=["baseline", "s8"])
    args = parser.parse_args()
    if args.worker:
        if args.variant is None:
            parser.error("--worker requires --variant")
        return _worker_main(args)
    if args.baseline_root is None or args.output is None:
        parser.error("--baseline-root and --output are required")
    baseline_root = args.baseline_root.resolve()
    baseline_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=baseline_root, capture_output=True, text=True,
        check=True,
    ).stdout.strip()
    if baseline_head != BASELINE_COMMIT:
        raise SystemExit(f"baseline must be {BASELINE_COMMIT}, got {baseline_head}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    variants: dict[str, Any] = {}
    for variant, source_root in (("baseline", baseline_root), ("s8", REPO)):
        with tempfile.TemporaryDirectory(prefix=f"kama-s85-{variant}-env-") as folder:
            scratch = Path(folder)
            command = [
                sys.executable, str(Path(__file__).resolve()), "--worker", "--variant", variant,
                "--samples", str(args.samples), "--warmup", str(args.warmup),
                "--messages", str(args.messages),
            ]
            completed = subprocess.run(
                command, cwd=source_root, env=_worker_env(source_root, scratch),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=900,
            )
            (output / f"{variant}-worker.txt").write_text(
                completed.stderr, encoding="utf-8"
            )
            if completed.returncode != 0:
                raise SystemExit(f"{variant} benchmark failed: {completed.stderr}")
            variants[variant] = json.loads(completed.stdout)
    raw = {
        "schema_version": 1,
        "method": {
            "baseline_commit": baseline_head,
            "s8_head": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
                check=True,
            ).stdout.strip(),
            "s8_worktree_dirty": bool(subprocess.run(
                ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True,
                check=True,
            ).stdout.strip()),
            "python": sys.version,
            "platform": platform.platform(),
            "clock": "time.perf_counter_ns",
            "warmup": args.warmup,
            "samples": args.samples,
            "messages": args.messages,
            "provider": "fixed in-process fake; no network",
            "token_estimate": "sum(len(str(message.content))) // 4",
            "recovery_trigger": "explicit resume; unattended restart continuation is not implemented",
        },
        **variants,
    }
    summary = _summarize(raw)
    (output / "raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
