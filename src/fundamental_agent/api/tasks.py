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

    def create(self, code: str = "", bk: str = "") -> TaskStatus:
        """创建任务。若同一 code+bk 已有 PENDING/RUNNING 任务，直接复用，避免重复打接口。

        bk 为行业对比报告的对比基准板块：切换板块后再点生成必须新建任务而非复用旧板块任务。
        """
        with self._lock:
            for t in self._tasks.values():
                if t.code == code and t.bk == bk and t.status in ("PENDING", "RUNNING"):
                    return t
            task_id = uuid.uuid4().hex[:12]
            task = TaskStatus(task_id=task_id, status="PENDING", stage="排队中",
                              code=code, bk=bk)
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
