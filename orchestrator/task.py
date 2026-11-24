"""长任务：提交即返回 + 进度快照 + 按子任务续跑。

## 为什么不是「一次调用做完」

同步接口有硬超时（生产里是 900 秒），而多日查询、批量重建这类任务可能跑几十分钟。
硬扛的结果是请求被掐断、前端拿不到结果、用户重试又从零开始。所以拆成三步：

1. **提交即返回**：先把子任务列表落成进度快照，立刻返回任务 id；
2. **逐个推进**：每完成一个子任务就更新快照（而不是全部做完才写），
   进程被杀时也能知道做到哪了；
3. **按快照续跑**：重启后只跑没完成的，已完成的绝不重跑（重跑会重复计费，
   有些写操作还会重复生效）。

## 部分失败是常态，不是异常

多日查询里某一天的表可能刚好在维护。正确做法是**保留成功结果、列出失败日期**，
而不是整批失败让用户全量重来。所以终止状态有三个：全成功、部分成功、全失败。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TaskStatus(StrEnum):
    """任务状态。`PARTIAL` 是「有成功也有失败」，不能和 `FAILED` 混为一谈。"""

    SUBMITTED = "submitted"
    RUNNING = "running"
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class Progress:
    """进度快照：可以直接序列化落盘。"""

    task_id: str
    subtasks: list[str]
    succeeded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    status: TaskStatus = TaskStatus.SUBMITTED
    attempts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.subtasks)

    @property
    def finished(self) -> int:
        return len(self.succeeded) + len(self.failed)

    def remaining(self) -> list[str]:
        """还没跑过的子任务（成功与已判失败的都算跑过）。"""
        done = set(self.succeeded) | {name for name, _reason in self.failed}
        return [name for name in self.subtasks if name not in done]


def submit(task_id: str, subtasks: list[str]) -> Progress:
    """提交任务：只落快照，不执行。"""
    return Progress(task_id=task_id, subtasks=list(subtasks), status=TaskStatus.SUBMITTED)


def start(progress: Progress) -> Progress:
    """标记开始推进（第一次 record 前调用，便于日志区分"排队"与"在跑"）。"""
    if progress.status is not TaskStatus.SUBMITTED:
        return progress
    progress.status = TaskStatus.RUNNING
    return progress


def record(progress: Progress, subtask: str, *, ok: bool, reason: str = "") -> Progress:
    """记录一个子任务的结果。**每完成一个就写一次**，崩溃时最多丢一个。"""
    progress.attempts[subtask] = progress.attempts.get(subtask, 0) + 1
    if ok:
        if subtask not in progress.succeeded:
            progress.succeeded.append(subtask)
    else:
        progress.failed.append((subtask, reason or "unknown"))
    if progress.status is TaskStatus.SUBMITTED:
        progress.status = TaskStatus.RUNNING
    return progress


def finish(progress: Progress) -> Progress:
    """收尾：按成功/失败比例定终态。"""
    if progress.remaining():
        # 还有没跑的子任务，不能算完成
        progress.status = TaskStatus.RUNNING
        return progress
    if progress.succeeded and progress.failed:
        progress.status = TaskStatus.PARTIAL
    elif progress.succeeded:
        progress.status = TaskStatus.DONE
    else:
        progress.status = TaskStatus.FAILED
    return progress


def resume(progress: Progress) -> list[str]:
    """重启后续跑：只返回没跑过的子任务。

    已判失败的**不自动重试**——失败原因可能是表在维护，重试只会再失败一次；
    要不要重试由调用方决定（`retry_failed` 显式要求）。
    """
    return progress.remaining()


def retry_failed(progress: Progress) -> list[str]:
    """显式重试失败项：把它们从 failed 里摘出来，重新算作待跑。"""
    names = [name for name, _reason in progress.failed]
    progress.failed = []
    if progress.status in (TaskStatus.PARTIAL, TaskStatus.FAILED):
        progress.status = TaskStatus.RUNNING
    return names


def summary(progress: Progress) -> dict:
    """给用户看的摘要：行数与失败清单，不给中间态细节。"""
    return {
        "task_id": progress.task_id,
        "status": progress.status.value,
        "total": progress.total,
        "succeeded": len(progress.succeeded),
        "failed": len(progress.failed),
        "failed_subtasks": [name for name, _reason in progress.failed],
        "remaining": len(progress.remaining()),
    }


__all__ = [
    "Progress",
    "TaskStatus",
    "finish",
    "record",
    "resume",
    "retry_failed",
    "start",
    "submit",
    "summary",
]
