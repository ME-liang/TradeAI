"""异步任务管理（进程内，线程安全）。

M1 阶段用内存字典即可；若后续多进程/多实例部署，把 _tasks 换成 Redis 即可，
对外接口不变。状态机：PENDING → RUNNING → SUCCESS / FAILED。
"""
from __future__ import annotations

import threading
import uuid
from typing import Optional

from .schemas import TaskStatus


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskStatus] = {}
        self._lock = threading.Lock()

    def create(self, code: str = "", bk: str = "", report_type: str = "") -> TaskStatus:
        """创建任务。仅当 **同一 code+bk+report_type** 已有 PENDING/RUNNING 任务时复用。

        去重键必须包含 report_type（2026-09-11 修复状态串扰根因）：此前只按 code+bk
        去重，概况任务尚在 RUNNING 时点「基本面」会命中该任务并原样返回，而调用方仍以
        report_type=fundamental 启动第二个 worker —— 两个 worker 共用同一 task_id 交替
        写 update()，stage/progress 与最终 html_path 被互相覆盖，前端两个面板轮询同一
        任务，出现「概况报告被基本面报告覆盖」。
        bk 为行业对比报告的对比基准板块：切换板块后再点生成必须新建任务而非复用旧任务。
        """
        with self._lock:
            for t in self._tasks.values():
                if (t.code == code and t.bk == bk
                        and getattr(t, "report_type", "") == report_type
                        and t.status in ("PENDING", "RUNNING")):
                    return t
            task_id = uuid.uuid4().hex[:12]
            task = TaskStatus(task_id=task_id, status="PENDING", stage="排队中",
                              code=code, bk=bk, report_type=report_type)
            self._tasks[task_id] = task
            return task

    def get(self, task_id: str) -> Optional[TaskStatus]:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **fields) -> None:
        """更新任务字段；不存在则静默忽略。回调式上报，失败不应抛异常。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            for k, v in fields.items():
                if hasattr(t, k):
                    setattr(t, k, v)
