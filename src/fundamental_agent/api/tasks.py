"""异步任务管理（进程内，线程安全）。

M1 阶段用内存字典即可；若后续多进程/多实例部署，把 _tasks 换成 Redis 即可，
对外接口不变。状态机：PENDING → RUNNING → SUCCESS / FAILED。

**F-04 报告任务中心（2026-09-26）**：本模块从「只能按 task_id 单点查询」扩展为
「能按用户列出任务」，供 `GET /api/tasks` 使用。三条必须守住的约束：

1. **按 `user_id` 硬过滤**（`list()` 的 user_id 参数是必填语义，虽然签名给了默认值）。
   任务里含 `html_path`（磁盘绝对路径）与 `error`（可能带内网信息），不过滤就是跨用户
   泄露 —— 与 F-06 D1「键漏一个维度」同型：当时落盘路径漏了 user 段，这一次是列表
   查询漏了 user 条件。`user_id=None` **只**匹配 `user_id` 也是 `None` 的任务，
   绝不会把某个真实用户的任务带出来。
2. **`list()` 返回的是同一个对象引用**（不复制）——调用方若要改字段必须走 `update()`。
   这样做的理由是：任务状态在 worker 线程里持续被改写，快照式复制会让列表与详情
   在同一秒内给出互相矛盾的两个进度。
3. **容量上限只淘汰终态任务**（`_evict_locked`）。淘汰正在跑的 PENDING/RUNNING 任务
   会让 worker 的 `update()` 变成静默 no-op（`_tasks.get()` 返回 None 直接 return），
   前端永远等不到终态 —— 现象是「进度条卡在 47% 不动」。
4. **用户主动删除也只看终态**（`remove()`）。用户要清掉的是「需要处理」区里那堆失败记录，
   而不是正在跑的任务 —— 后者同样会把进度条钉死。**本约束有两个入口**（容量淘汰 +
   用户删除），只在其中一个上守住等于没守（see `remove()` 的 docstring 第 1 条）。

本模块**不**负责落盘 / 持久化：跑中任务本就是瞬态，持久的是磁盘产物（F-06）。
二者用 `TaskStatus.report_id` 接缝（任务 SUCCESS 时回填），所以 8002 重启后
「已完成的任务列表」会清空、但它指向的**报告仍在报告库中可打开** —— 这是设计取舍，
不是缺陷（F-27「任务持久化」是独立议题）。

## 排序：`_tasks` 的字典插入序就是「创建序」

`list()` 的最新在前与 `_evict_locked()` 的最旧先淘汰**共用同一条规则** ——
`dict` 的插入序。**不要改用 `created_at`**：它只有秒精度，同一秒内提交的多个任务
会在排序时退化成任意顺序（实测两处都会被这个坑咬到：列表顺序在两次请求间不一致、
淘汰掉的不是最旧那个）。`created_at` / `updated_at` 只用于**展示**（耗时、完成时刻）。
若将来把 `_tasks` 换成 Redis，新实现**必须**保留"新→旧"这个顺序契约。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Optional

from .schemas import TaskStatus

# 活动状态（尚未进入终态）。**收敛成常量**：此前这个元组在 create() 里以字面量出现，
# 而 list(active_only=)/_evict_locked() 又要用同一组值 —— 三处各写一遍字面量，
# 改动时漏一处就会出现「去重说在跑、列表说已结束」这种自相矛盾。
ACTIVE_STATUSES: tuple[str, ...] = ("PENDING", "RUNNING")
TERMINAL_STATUSES: tuple[str, ...] = ("SUCCESS", "FAILED")

# 进程内保留的任务条数上限（超出时淘汰最旧的**终态**任务）
MAX_TASKS = 200

# 任务中心默认返回条数上限（防止前端一次拉回几百条）
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


def _now_iso() -> str:
    """带本地时区偏移的 ISO8601（秒精度）。前端据 created_at/updated_at 算耗时。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class TaskManager:
    def __init__(self, max_tasks: int = MAX_TASKS) -> None:
        self._tasks: dict[str, TaskStatus] = {}
        self._lock = threading.Lock()
        self._max_tasks = int(max_tasks)

    # ===== 写 =====

    def create(self, code: str = "", bk: str = "", report_type: str = "",
               user_id: int | None = None, name: str = "",
               bk_name: str = "") -> TaskStatus:
        """创建任务。仅当 **同一 user_id+code+bk+report_type** 已有 PENDING/RUNNING 任务时复用。

        去重键必须包含 report_type（2026-09-11 修复状态串扰根因）：此前只按 code+bk
        去重，概况任务尚在 RUNNING 时点「基本面」会命中该任务并原样返回，而调用方仍以
        report_type=fundamental 启动第二个 worker —— 两个 worker 共用同一 task_id 交替
        写 update()，stage/progress 与最终 html_path 被互相覆盖，前端两个面板轮询同一
        任务，出现「概况报告被基本面报告覆盖」。
        bk 为行业对比报告的对比基准板块：切换板块后再点生成必须新建任务而非复用旧任务。

        去重键还必须包含 user_id（F-06 D1）：否则两个用户同时生成同一只票会命中同一个
        RUNNING 任务，状态与 html_path 互相覆盖 —— 与上述事故**完全同型**（又一次
        「键漏一个维度」）。落盘路径已按用户隔离，任务侧必须同步，否则任务层仍是公共的。
        """
        with self._lock:
            for t in self._tasks.values():
                if (t.code == code and t.bk == bk
                        and getattr(t, "report_type", "") == report_type
                        and getattr(t, "user_id", None) == user_id
                        and t.status in ACTIVE_STATUSES):
                    return t
            task_id = uuid.uuid4().hex[:12]
            now = _now_iso()
            task = TaskStatus(task_id=task_id, status="PENDING", stage="排队中",
                              code=code, bk=bk, report_type=report_type, user_id=user_id,
                              name=name, bk_name=bk_name,
                              created_at=now, updated_at=now)
            self._tasks[task_id] = task
            self._evict_locked()
            return task

    def get(self, task_id: str) -> Optional[TaskStatus]:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **fields) -> None:
        """更新任务字段；不存在则静默忽略。回调式上报，失败不应抛异常。

        `updated_at` 每次调用都会被刷新（即使调用方没传）——它是「状态最近何时变化」，
        不是「谁改了什么」，因此不接受外部覆盖。
        """
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            for k, v in fields.items():
                if hasattr(t, k):
                    setattr(t, k, v)
            t.updated_at = _now_iso()

    # ===== 删（F-04 任务中心「删除失败记录」，2026-09-26）=====

    def remove(self, task_id: str, user_id: int | None) -> str:
        """删除**自己的一条终态任务**，返回 `"deleted"` / `"not_found"` / `"active"`。

        为什么需要：失败的任务**不落盘**（没有产物），所以它只活在任务中心里，而且
        `list()` 会一直把它列出来。跑几次参数错误的生成（例如不存在的板块 BK9999），
        「需要处理」区就会堆满同一行字，用户没有任何办法清掉 —— 本方法就是那个出口。

        四条语义（每条都对应一个已发生的坑）：

        1. **PENDING / RUNNING 一律拒删（返回 `active`）**：删掉运行中的任务会让正在跑的
           那个 worker 的 `update()` 变成静默 no-op（`_tasks.get()` 返回 None 直接 return），
           前端进度条**永久卡死**。这与 `_evict_locked()` 只淘汰终态是**同一条约束的第二个
           入口** —— 只在容量淘汰里守住、却在这里开个后门，护栏等于没做。
        2. **不属于自己的任务返回 `not_found`，与「不存在」同形**：若区分出 `forbidden`，
           本端点就变成 task_id **存在性探测器**（据此可推断别人正在生成什么报告）。
           归属判断与 `get()` 的语义保持一致：`user_id=None` 只匹配同样无归属的任务。
        3. **只删任务记录，不删磁盘产物**：SUCCESS 的报告仍在报告库（报告库扫磁盘，不看
           `_tasks`）。即本操作**不是**"删除报告"——所以 UI 只把它放在失败/异常行上，
           避免用户误以为删掉的是报告本身。
        4. **删除后同一个 task_id 不会复活**：`seen` 状态由前端清理（见 `task_center.js`），
           服务端这边 `_tasks.pop()` 之后即便有迟到的 `update()` 也只是 no-op。
        """
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None or t.user_id != user_id:
                return "not_found"
            if t.status not in TERMINAL_STATUSES:
                return "active"
            self._tasks.pop(task_id, None)
            return "deleted"

    def remove_many(self, task_ids, user_id: int | None) -> dict:
        """批量删除（`POST /api/tasks/remove` 的唯一实现；单个删除传单元素列表）。

        不做"一条活跃就整批拒绝"：删除是幂等清理操作，部分成功对用户更有用；真正要守住
        的不变量是**活跃任务绝不被删除**，故用 `skipped_active` 显式回报，让前端能说清
        哪条没删掉（而不是静默少删一条）。同一 id 重复传入只处理一次（否则第二次会以
        `not_found` 计入，让 `deleted + not_found` 加起来超过请求条数，前端报数就错了）。
        """
        deleted, not_found, skipped = 0, 0, []
        seen: set[str] = set()
        for tid in task_ids or []:
            if not isinstance(tid, str) or not tid or tid in seen:
                continue
            seen.add(tid)
            r = self.remove(tid, user_id)
            if r == "deleted":
                deleted += 1
            elif r == "active":
                skipped.append(tid)
            else:
                not_found += 1
        return {"deleted": deleted, "not_found": not_found, "skipped_active": skipped}

    # ===== 读（F-04 报告任务中心）=====

    def list(self, user_id: int | None, *, limit: int = DEFAULT_LIST_LIMIT,
             active_only: bool = False) -> list[TaskStatus]:
        """列出**该 user_id 的**任务，最新创建的在前。

        ⚠️ 这是本模块唯一的列表出口，也是唯一的跨用户泄露面：`user_id=None` 与
        `user_id=1` 是**两个不同的集合**（不是「不过滤」）。调用方（`GET /api/tasks`）
        必须传登录用户的 id，绝不接受前端传入的 user_id。

        active_only=True 只返回 PENDING/RUNNING（任务中心的「进行中」区）。
        返回的是对象引用（见模块 docstring 第 2 条）。

        **排序用 `_tasks` 的字典插入序，不用 `created_at`**：`created_at` 是秒精度，
        同一秒内连续提交的多个任务用它会得到任意顺序 —— 实测表现为「列表顺序在两次
        请求间不一致」，而前端会据此把同一个任务重复判成「新完成」并重复弹提示。
        CPython 的 dict 保持插入序，等价于创建序，且与淘汰顺序天然一致。
        （把 `_tasks` 换成 Redis 时，实现方**必须**继续保证这个"新→旧"的顺序契约。）
        """
        with self._lock:
            items = [t for t in self._tasks.values() if t.user_id == user_id]
        if active_only:
            items = [t for t in items if t.status in ACTIVE_STATUSES]
        items.reverse()                     # 插入序 → 最新在前
        n = int(limit or 0)
        if n > 0:
            items = items[:min(n, MAX_LIST_LIMIT)]
        return items

    def active_count(self, user_id: int | None) -> int:
        """该用户进行中的任务数（顶部导航红点 / 悬浮胶囊用）。"""
        return len(self.list(user_id, limit=0, active_only=True))

    def active_keys(self, user_id: int | None) -> set[tuple[str, str]]:
        """该用户**活跃任务**的 `(标的主体, 报告类型)` 集合。

        用途：报告库删除时判断「这份报告是不是正在被重新生成」。删掉正在写的目录会让
        worker 的 `save_report()` 中途失败，或写回一个用户以为已经删掉的目录 ——
        这是「活跃任务绝不能被删除」这条约束的**第三个入口**（另两个：`remove()` 与
        `_evict_locked()`）。只在两处守住、在这里开个后门，护栏等于没做。

        只看 `(主体, 类型)`、**不看日期段**：日期段由 worker 自己按当天算（行业对比还带
        `__{bk}` 后缀），从任务侧复算它等于把 `storage` 的路径规则抄第二份 —— 那正是
        N-15/N-18「同一件事有两个定义处」的老坑。保守拒绝的代价只是「同日重新生成时
        旧报告要多等几十秒」，比路径规则抄错小得多。
        """
        keys: set[tuple[str, str]] = set()
        for t in self.list(user_id, limit=0, active_only=True):
            subject = t.bk if t.report_type == "board" else t.code
            if subject:
                keys.add((subject, t.report_type))
        return keys

    def forget_report_ids(self, user_id: int | None, report_ids) -> int:
        """移除指向这些 `report_id` 的**终态**任务记录，返回移除条数。

        为什么报告库删除必须连带做这件事：任务中心对 SUCCESS 任务下发的是
        `/api/report/view/{report_id}`。报告被删掉之后，那条记录仍会在「最近完成」里
        挂着一个**点开就 404 的链接** —— 这正是 N-21 的形状（列表广告了详情拒绝的条目），
        也是 F-04 反复强调的那条：**任务下发的每个链接都必须真的能打开**。

        清理范围严格限定为**本人 + 终态 + `report_id` 精确相等**：不模糊匹配、不猜。
        返回移除条数，交给调用方如实回报（用户需要知道「删这份报告顺带清掉了 1 条任务记录」）。
        """
        want = {r for r in (report_ids or []) if isinstance(r, str) and r}
        if not want:
            return 0
        n = 0
        with self._lock:
            for tid, t in list(self._tasks.items()):
                if t.user_id != user_id:
                    continue
                if t.status not in TERMINAL_STATUSES:
                    continue
                if t.report_id and t.report_id in want:
                    self._tasks.pop(tid, None)
                    n += 1
        return n

    # ===== 容量治理 =====

    def _evict_locked(self) -> None:
        """超出容量时淘汰**最旧的终态**任务；PENDING/RUNNING 永不淘汰。

        必须持锁调用。淘汰终态是安全的：它的结果已经落盘（SUCCESS）或已失败，
        用户仍能在报告库（SUCCESS）看到产物；而淘汰运行中的任务会让正在跑的那个
        worker 的 `update()` 静默失效，前端进度条永久卡死。

        淘汰顺序与 `list()` 的排序**同源**：`_tasks` 的字典插入序（= 创建序）。
        此前用 `created_at` 排序，在"同一秒内建了多个任务"时会退化成按 task_id
        随机排序 —— 表现为淘汰掉的不是最旧那个（列表里会冒出"刚建的任务消失了、
        更早的还留着"）。
        """
        if self._max_tasks <= 0 or len(self._tasks) <= self._max_tasks:
            return
        for tid, t in list(self._tasks.items()):        # 插入序 = 最旧在前
            if len(self._tasks) <= self._max_tasks:
                break
            if t.status in TERMINAL_STATUSES:
                self._tasks.pop(tid, None)
