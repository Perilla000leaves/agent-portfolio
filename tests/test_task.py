"""长任务用例：提交即返回、逐条落进度、断点续跑、部分失败保留成功结果。"""
from __future__ import annotations

from orchestrator.task import (
    TaskStatus,
    finish,
    record,
    resume,
    retry_failed,
    start,
    submit,
    summary,
)


def _submitted() -> object:
    return submit("t-1", ["20261117", "20261118", "20261119"])


def test_submit_returns_immediately_without_running():
    progress = _submitted()
    assert progress.status is TaskStatus.SUBMITTED
    assert progress.remaining() == ["20261117", "20261118", "20261119"]


def test_record_advances_remaining():
    progress = _submitted()
    start(progress)
    record(progress, "20261117", ok=True)

    assert progress.status is TaskStatus.RUNNING
    assert progress.succeeded == ["20261117"]
    assert progress.remaining() == ["20261118", "20261119"]


def test_all_succeeded_is_done():
    progress = _submitted()
    for day in progress.subtasks:
        record(progress, day, ok=True)
    finish(progress)

    assert progress.status is TaskStatus.DONE
    assert summary(progress)["failed"] == 0


def test_partial_failure_keeps_successful_results():
    """某一天表在维护不该让整批重来：保留成功结果，列出失败日期。"""
    progress = _submitted()
    record(progress, "20261117", ok=True)
    record(progress, "20261118", ok=False, reason="table offline")
    record(progress, "20261119", ok=True)
    finish(progress)

    assert progress.status is TaskStatus.PARTIAL
    result = summary(progress)
    assert result["succeeded"] == 2
    assert result["failed_subtasks"] == ["20261118"]


def test_all_failed_is_failed():
    progress = _submitted()
    for day in progress.subtasks:
        record(progress, day, ok=False, reason="boom")
    finish(progress)
    assert progress.status is TaskStatus.FAILED


def test_finish_with_pending_subtasks_stays_running():
    """还有没跑的子任务时不能标完成——否则续跑会漏掉它们。"""
    progress = _submitted()
    record(progress, "20261117", ok=True)
    finish(progress)

    assert progress.status is TaskStatus.RUNNING


def test_resume_returns_only_unfinished():
    progress = _submitted()
    record(progress, "20261117", ok=True)
    record(progress, "20261118", ok=False, reason="table offline")

    # 崩溃重启：只跑没跑过的，已判失败的也不自动重试
    assert resume(progress) == ["20261119"]


def test_retry_failed_moves_them_back_to_pending():
    progress = _submitted()
    record(progress, "20261117", ok=True)
    record(progress, "20261118", ok=False, reason="table offline")
    record(progress, "20261119", ok=True)
    finish(progress)

    assert retry_failed(progress) == ["20261118"]
    assert resume(progress) == ["20261118"]


def test_attempts_are_counted_per_subtask():
    progress = _submitted()
    record(progress, "20261117", ok=False, reason="x")
    record(progress, "20261117", ok=True)
    assert progress.attempts["20261117"] == 2
