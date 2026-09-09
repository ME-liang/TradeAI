"""巨潮(cninfo)系取数进程级串行闸（线程安全）。

背景（性能优化·措施2 的安全前提，2026-09-06 复现证据）：
- akshare 的 cninfo 系接口（stock_dividend_cninfo / stock_profile_cninfo 等）每次调用
  都用 py_mini_racer 新建一个 V8 isolate（mini_racer.dll）执行解密 JS；
- 实测并发创建/使用多个 V8 isolate 会触发 V8 FATAL，进程以 exit code 3 静默崩溃
  （栈顶为 mini_racer.dll，本日两次独立复现：industry 画像 6 路并发、六层取数并发）。
- 崩溃具有偶发性（同组合有时存活），因此不能依赖「不崩」赌概率，必须从结构上杜绝
  两个 cninfo 系调用在同一时刻重叠。

本模块提供进程内唯一锁（可重入 RLock）：
同一时刻至多一个线程处于 cninfo 系调用内；纯 HTTP 的东财 / 申万 / baostock 接口
不受本锁约束，可自由并行。所有会触碰 cninfo 的代码路径（AkShareClient.get_identity
的 cninfo 段 / get_dividend / report_types.industry.fetch_company_profile 的 cninfo 段）
都必须包在 cninfo_guard() 内。
"""
from __future__ import annotations

import threading

_lock = threading.RLock()


def cninfo_guard():
    """上下文管理器：持有全局 cninfo 串行锁（可重入，内部嵌套安全）。"""
    return _lock
