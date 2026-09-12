"""编排入口：解析参数 → 实体解析 → 取数 → 六引擎 → 渲染。

脚手架阶段：数据层使用 DummyDataClient（或填入 token 后的 TushareClient），
各引擎返回空结果，最终仍渲染出一份结构完整的 HTML 报告壳子。
后续只需填充数据层与引擎内部逻辑，本文件无需改动流程。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


def _ensure_runtime() -> None:
    """若当前解释器缺少核心依赖(pandas)，自动切换到项目 venv 重新执行自身，
    从而保证在 VS Code 中直接点击“运行 Python 文件”也能正常工作，
    不依赖 VS Code 当前选中的解释器。"""
    try:
        import pandas  # noqa: F401
    except ImportError:
        project_root = Path(__file__).resolve().parent.parent.parent
        candidates = [
            r"C:\Users\Administrator\.workbuddy\binaries\python\envs\fundamental_agent\Scripts\python.exe",
            str(project_root / ".venv" / "Scripts" / "python.exe"),
        ]
        for py in candidates:
            if py and os.path.exists(py) and os.path.abspath(py) != os.path.abspath(sys.executable):
                os.execv(py, [py, *sys.argv])
        sys.stderr.write(
            "错误：当前 Python 解释器缺少依赖(pandas)。\n"
            "请使用项目 venv 运行，或先执行：pip install -r requirements.txt\n"
        )
        sys.exit(1)


_ensure_runtime()



# 支持两种运行方式：
#   1) 模块方式（推荐）：在 src/ 下执行 `python -m fundamental_agent.main`
#   2) 脚本方式：VS Code 点"运行"按钮，等价于 `python .../main.py`
# 直接作为脚本运行时没有包上下文，这里把 src/ 加入路径，改用绝对导入即可。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fundamental_agent.data.dummy import DummyDataClient
from fundamental_agent.data.tushare import TushareClient
from fundamental_agent.entity import resolve, Entity
from fundamental_agent.engine import ENGINE_REGISTRY
from fundamental_agent.models import AnalysisResult
from fundamental_agent.report.render import render
from fundamental_agent.llm import summarize_analysis, polish_module_insights
from fundamental_agent.pipeline.company_profile import build_profile, build_financial_summary
from fundamental_agent.report_types.overview import (
    build_overview_data,
    generate_overview_content,
    render_overview,
)

logger = logging.getLogger(__name__)


def _make_client(code: str, *, use_dummy: bool, token: str):
    """选择数据客户端：默认 AkShare（真实），--dummy 或缺失时回退占位。"""
    if use_dummy:
        return DummyDataClient()
    # 优先 AkShare（免费、无需 token）
    try:
        from fundamental_agent.data.akshare import AkShareClient

        akc = AkShareClient()
        if akc.available():
            return akc
    except Exception:
        pass
    # 有 token 时可用 Tushare
    if token:
        return TushareClient(token=token)
    print("[提示] 未检测到 AkShare，且无 --token，使用占位数据(DummyDataClient)。")
    return DummyDataClient()


# 财报取数磁盘缓存：首次成功拉取后落盘，7 天内复用。
# 目的：(1) 避免每次生成都重撞外部网络（含此前「拉取财报数据」卡死 20+ 分钟的根因）；
#       (2) 让 P0/P1 回归验证重跑确定性、秒级完成；(3) 数据冻结——同代码同区间数据恒定，
#       不会因 AkShare 偶发字段漂移污染报告。缓存为空或超龄则回退实时拉取，绝不伪造数据。
_STATEMENTS_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache" / "statements"
_STATEMENTS_CACHE_TTL = 7 * 24 * 3600  # 7 天


def _statements_cache_path(code: str) -> Path:
    _STATEMENTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATEMENTS_CACHE_DIR / f"{code}.pkl"


def _load_cached_statements(code: str) -> dict | None:
    p = _statements_cache_path(code)
    if not p.exists():
        return None
    try:
        if (datetime.now().timestamp() - p.stat().st_mtime) > _STATEMENTS_CACHE_TTL:
            return None
        import pickle

        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_cached_statements(code: str, data: dict) -> None:
    try:
        import pickle

        with open(_statements_cache_path(code), "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        pass


def build_statements(client, code: str) -> dict:
    """拉取三表 + 财务分析指标 + 公司画像 + 行业可比，组装为统一字典。

    带磁盘缓存：命中且在 TTL 内直接复用，避免重复网络往返与挂起风险。
    """
    cached = _load_cached_statements(code)
    if cached is not None:
        return cached
    data = {
        "income": client.get_income_statement(code),
        "balance": client.get_balance_sheet(code),
        "cash": client.get_cash_flow(code),
        "indicators": client.get_financial_indicators(code),
        "profile": client.get_company_profile(code),
        "peers": client.get_industry_peers(code),
    }
    _save_cached_statements(code, data)
    return data



def analyze(name: str, code: str, *, use_dummy: bool = False, token: str = "") -> AnalysisResult:
    """执行一次完整分析，返回 AnalysisResult（未落盘）。

    保留原签名与行为，内部转发 run_pipeline，供 CLI 与旧调用方使用。
    """
    return run_pipeline(
        name=name, code=code, use_dummy=use_dummy, token=token,
        use_llm=False, out_path=None,
    )["result"]


def _overview_layers_worker(code: str, self_name: str, include_holders: bool,
                            budget_s: int, conn) -> None:
    """OV-2：概况六层取数的**子进程** worker（spawn，进程级隔离）。

    原因（2026-09-08 实测）：六层并发池内的 akshare C 扩展调用存在偶发的进程级
    静默崩溃（exit 0、无 traceback、faulthandler 无输出；单任务隔离全部正常，
    仅多任务并发组合触发，与 fitz/MiniRacer 加载顺序无关、新旧代码皆可复现）。
    取数放入子进程后：崩溃不影响 API 服务与主流程，父进程可安全重试一次；
    全部失败时按既有「单层失败回退空值」语义降级，绝不阻断报告生成。
    """
    data: dict = {}
    status: dict = {}   # 层 → ok / empty / timeout / error:xxx（空值可归因）
    try:
        from concurrent.futures import ThreadPoolExecutor, wait

        import pandas as pd

        from fundamental_agent.data.akshare import AkShareClient

        client = AkShareClient()

        def _records(df):
            if df is None or not hasattr(df, "empty") or df.empty:
                return []
            return df.where(pd.notna(df), None).to_dict(orient="records")

        def _safe_records(key, fn):
            """带状态记录的取数：异常与「上游真空」必须可区分（否则空值不可归因）。"""
            try:
                return _records(fn())
            except Exception as e:  # noqa: BLE001
                status[key] = "error:" + str(e)[:80]
                return []

        def _job_peers():
            # 同业可比：优先按业务实质精选的可比集；无精选集退化为申万二级口径
            try:
                curated = getattr(client, "get_curated_peers_overview", None)
                peers = curated(code, self_name) if curated else []
                if not peers:
                    fn = getattr(client, "get_industry_peers_overview", None)
                    peers = fn(code) if fn else []
                return peers
            except Exception:
                return []

        def _wrap(k, fn):
            """包裹任务：记录每层状态，异常不再被无声吞掉。"""
            def _run():
                try:
                    v = fn()
                    if status.get(k) is None or not str(status.get(k)).startswith("error"):
                        status[k] = "ok" if v not in (None, [], {}, 0) else "empty"
                    return v
                except Exception as e:  # noqa: BLE001
                    status[k] = "error:" + str(e)[:80]
                    return None
            return _run

        jobs = {
            "cninfo": (lambda: client.get_identity(code) or {}),
            "dividend": (lambda: _safe_records("dividend", lambda: client.get_dividend(code))),
            "news": (lambda: _safe_records("news", lambda: client.get_news(code))),
            "main_business": (lambda: _safe_records("main_business", lambda: client.get_main_business(code))),
            "pledge": (lambda: _safe_records("pledge", lambda: client.get_pledge(code))),
            "share_capital": (lambda: client.get_share_capital(code) or 0),
            "peers": _job_peers,
        }
        try:
            from fundamental_agent.data.market import get_market_snapshot
            jobs["market"] = (lambda: get_market_snapshot(code))
        except Exception:
            pass

        # OV-2 holders 独立通道：get_holders 是全市场大表逐期拉取（实测 39s），
        # 放在六层池内会挤占公共预算（2026-09-09 复现：60s 预算超时后 cancel_futures
        # 把 dividend/pledge/peers 一起取消，导致治理与行业坐标整块为空）。
        # 改为独立单线程 + 独立超时，与六层池并行但不共享预算、不牵连其它层。
        holders_ex = None
        holders_fut = None
        if include_holders:
            try:
                holders_ex = ThreadPoolExecutor(max_workers=1)
                holders_fut = holders_ex.submit(
                    _wrap("holders", lambda: _safe_records("holders", lambda: client.get_holders(code)))
                )
            except Exception:  # noqa: BLE001 — 独立通道失败回退空值
                holders_fut = None

        ex = ThreadPoolExecutor(max_workers=6)
        try:
            futs = {ex.submit(_wrap(k, fn)): k for k, fn in jobs.items()}
            # 关键：用 wait 收集**已完成**层；超时层只标记、不牵连其它层
            # （原 as_completed(timeout) 会在超时时抛错并 cancel 掉所有未完成的层）。
            done, not_done = wait(futs, timeout=budget_s)
            for fut in done:
                _k = futs[fut]
                try:
                    _v = fut.result()
                    if _v is not None:
                        data[_k] = _v
                except Exception as e:  # noqa: BLE001
                    status[_k] = "error:" + str(e)[:80]
            for fut in not_done:
                status[futs[fut]] = "timeout"
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        # 收 holders 结果（独立超时 45s，失败/超时可在父进程补取）
        if holders_fut is not None:
            try:
                _hv = holders_fut.result(timeout=45)
                if _hv is not None:
                    data["holders"] = _hv
            except Exception as e:  # noqa: BLE001
                status["holders"] = "timeout_or_error:" + str(e)[:60]
            finally:
                holders_ex.shutdown(wait=False, cancel_futures=True)

        conn.send({"ok": True, "data": data, "status": status})
    except Exception as e:  # noqa: BLE001 — worker 级异常回传父进程
        try:
            conn.send({"ok": False, "error": str(e)[:300]})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_pipeline(
    name: str = "",
    code: str = "",
    *,
    use_dummy: bool = False,
    token: str = "",
    use_llm: bool = True,
    report_type: str = "fundamental",
    out_path: str | None = None,
    on_progress=None,
    bk: str = "",
    bk_name: str = "",
) -> dict:
    """执行一次完整分析并（可选）落盘，是 CLI 与 Web API 共用的唯一入口。

    on_progress: 可选回调 `f(stage: str, pct: int)`，用于异步任务上报进度。
                 回调自身抛异常不影响主流程。

    返回 dict：{"result": AnalysisResult, "out_path": str|None, "code": str, "name": str}
    """
    def _emit(stage: str, pct: int) -> None:
        if on_progress is None:
            return
        try:
            on_progress(stage, max(0, min(100, int(pct))))
        except Exception:
            pass  # 进度上报失败绝不影响主流程

    _emit("准备数据源", 5)
    client = _make_client(code, use_dummy=use_dummy, token=token)

    # 名称 → 代码（AkShare 路径下支持中文名解析）
    if not code and name and hasattr(client, "resolve_code"):
        resolved = client.resolve_code(name)  # type: ignore[attr-defined]
        if resolved:
            code = resolved

    entity = resolve(name=name, code=code) if code else Entity(code=code, name=name or code)

    # ==================== 性能优化·措施1：行业对比报告旁路 ====================
    # 行业报告真正需要的只有：竞品发现 + 定量指标 + 公司画像 + LLM 并列对比，
    # 全部由 build_industry_report_data 内部完成。旧流程却让它先跑完
    #   build_statements(三表+财务指标+画像) → 六层概况取数 → 年报 PDF 下载解析
    #   （公司行动/上下游/股东名册）→ 六大分析引擎
    # 这些是行业报告用不到的无效前置（实测占其端到端 90% 时间，见
    # docs/报告生成性能优化方案.md）。故在此直接进入 industry 专属路径；
    # 渲染与落盘与函数底部 overview/fundamental 分支同构，类型物理隔离不变。
    if report_type == "industry":
        _emit("识别行业对比样本", 30)
        from fundamental_agent.report_types.industry import (
            build_industry_report_data,
            render_industry,
        )

        ind_data = build_industry_report_data(
            entity.code, entity.name or code, use_llm=use_llm, progress=_emit,
            bk=bk, bk_name=bk_name,
        )
        written: str | None = None
        if out_path:
            template_dir = str(Path(__file__).resolve().parent / "report")
            written = render_industry(ind_data, template_dir=template_dir, out_path=out_path)
        _emit("完成", 100)
        _name = str(ind_data.get("target_name") or "").replace(" ", "").strip()
        return {
            "result": AnalysisResult(
                name=_name or entity.name or code,
                code=entity.code,
                modules=[],
                profile={},
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            ),
            "out_path": written,
            "code": entity.code,
            "name": _name or entity.name or code,
            # 行业报告不装配六层 JSON 契约（模板独立），无缺失校验语义
            "validation": {"missing_marked": [], "warnings": [], "suspicious_llm_numbers": []},
            "profile_json": None,
        }
    # ==========================================================================

    _emit("拉取财报数据", 15)

    # B1（速度优化 2026-09-10）：六层概况取数子进程先启动，与财报拉取**并行**——
    # 两者数据源独立（子进程 worker 只依赖 code/self_name），串行是纯白等；
    # 汇合点在六层子进程结束之后（见下方 _stmt_fut.result()）。
    _code = entity.code
    _self_name = entity.name
    profile = {}

    from concurrent.futures import ThreadPoolExecutor as _StmtEx

    _stmt_ex = _StmtEx(max_workers=1)

    def _stmt_task():
        _st = build_statements(client, entity.code)
        _prof = _st.get("profile", {}) or {}
        # 财务速写摘要：从「财务分析指标表 + 三大报表」直接计算（不依赖引擎、不依赖 LLM）。
        # 必须在 build_profile 之前写入 profile：概况报告的财务数字全部由此提供，
        # 引擎产出的 Metric.value 实测多为 None，不足以支撑「05 财务速写」模块。
        try:
            _prof["fin_summary"] = build_financial_summary(_st)
        except Exception:
            _prof["fin_summary"] = {}
        return _st, _prof

    _stmt_fut = _stmt_ex.submit(_stmt_task)

    # 概况六层取数（cninfo / 同花顺 / 东财可用端点），并入 profile 供 build_profile 装配。
    # DataFrame 统一转为 records（list[dict]），保证 result.profile 全程可 JSON 序列化
    # （result.profile 会被 validate.py 遍历、render.py 读取，不可直接存 DataFrame）。
    _emit("拉取公司概况数据", 22)

    def _records(df):
        if df is None or not hasattr(df, "empty") or df.empty:
            return []
        return df.where(pd.notna(df), None).to_dict(orient="records")

    # ===== 性能优化·措施2：六层取数并发池（原为逐接口串行, 实测占报告 22% 阶段约 2 分钟）=====
    # 各类取数相互独立：东财/申万 HTTP 类接口可自由并行；cninfo(MiniRacer) 类调用在各自
    # 实现内部经 data/cninfo_guard.py 全局锁串行（同一时刻至多一个 V8 isolate），从结构上
    # 杜绝并发 FATAL 崩溃（2026-09-06 两次独立复现, 崩溃偶发不可赌）。单层失败回退空值,
    # 与原先逐 try 的语义一致, 绝不拖垮主流程。
    _code = entity.code
    profile.setdefault("cninfo", {})
    profile["holders"] = []
    profile["dividend"] = []
    profile["news"] = []
    profile["main_business"] = []
    profile["pledge"] = []
    profile["share_capital"] = 0
    profile["peers"] = []
    profile["market"] = {}

    def _safe_records(fn):
        try:
            return _records(fn())
        except Exception:
            return []

    def _job_peers():
        # 概况报告「同业可比」：优先取按**业务实质**精选的可比集（含本公司自身，便于定位）；
        # 该公司无精选集时，退化为申万二级口径（仅 AkShare 客户端提供，隔离于基本面引擎）。
        try:
            curated = getattr(client, "get_curated_peers_overview", None)
            peers = curated(_code, _self_name) if curated else []
            if not peers:
                fn = getattr(client, "get_industry_peers_overview", None)
                peers = fn(_code) if fn else []
            return peers
        except Exception:
            return []

    def _job_market():
        # P2-E1：实时-延时行情 + 融资融券余额（东方财富 push2delay，规避 push2 封禁端点）。
        try:
            from fundamental_agent.data.market import get_market_snapshot
            return get_market_snapshot(_code)
        except Exception:
            return {}

    def _job_share_capital():
        try:
            return client.get_share_capital(_code) or 0
        except Exception:
            return 0

    # 2026-09-09 修订：不再按「本地 PDF 缓存是否存在」跳过 get_holders。
    # 原跳过判定（OV-2）有洞：缓存存在 ≠ 名册抽得出来；一旦名册抽取失败（
    # 688981 实测：缓存有 PDF 但名册 <5 行），holders 既没在子进程取、又因旧兜底
    # 条件被跳过，治理区块整块为空。现改为：holders 在子进程**独立通道**并发取
    # （不占六层预算、不牵连其它层），名册抽取仍在池后进行并覆盖 holders；
    # 父进程保留最后的串行补取作为兜底。
    # 注意：此处**严禁导入 annual_report**（其模块级 import fitz 会让 PyMuPDF 先于
    # MiniRacer 加载，与并发池内 cninfo V8 调用交互会间歇性静默崩溃整个进程）。

    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FutTimeout  # noqa: F401（引擎段沿用）

    # OV-2（概况性能专项）：六层取数改在**子进程**执行（spawn）+ 失败重试一次。
    # 原因：并发池内的 akshare C 扩展调用存在偶发进程级静默崩溃（exit 0、无
    # traceback，实测约 4/10；单任务隔离全部正常，仅并发组合触发，与 fitz/
    # MiniRacer 加载顺序无关）。子进程隔离后崩溃不拖垮 API 服务；两层全失败
    # 时各层保持默认空值继续（与旧「单层失败回退空值」语义一致）。
    _layers: dict = {}
    try:
        import multiprocessing as _mp

        _ctx = _mp.get_context("spawn")
        for _attempt in (1, 2):
            _parent_conn, _child_conn = _ctx.Pipe(duplex=False)
            _p = _ctx.Process(
                target=_overview_layers_worker,
                args=(_code, _self_name, True, 420, _child_conn),
                daemon=True,
            )
            _p.start()
            _child_conn.close()
            _msg = None
            try:
                if _parent_conn.poll(420 + 45):
                    _msg = _parent_conn.recv()
            except (EOFError, OSError):
                _msg = None
            finally:
                try:
                    _parent_conn.close()
                except Exception:
                    pass
                if _p.is_alive():
                    _p.terminate()
                _p.join(5)
            if _msg and _msg.get("ok"):
                _layers = _msg.get("data") or {}
                _st = _msg.get("status") or {}
                if _st:
                    logger.info(
                        "[main] 概况六层取数状态：%s",
                        "，".join(f"{k}={v}" for k, v in sorted(_st.items())),
                    )
                break
            logger.warning(
                "[main] 概况六层取数子进程第 %d 次尝试失败（%s）→ %s",
                _attempt,
                str((_msg or {}).get("error") or "进程异常退出")[:120],
                "重试一次" if _attempt == 1 else "以空值继续",
            )
    except Exception as _e:  # noqa: BLE001 — 隔离层整体失败不阻断报告
        logger.warning("[main] 概况六层子进程取数异常，以空值继续：%s", _e)

    for _k, _v in _layers.items():
        profile[_k] = _v

    # B1 汇合：取回与六层子进程并行执行的财报拉取结果（异常时 statements 为空，
    # 后续引擎/渲染沿用「缺数据→未获取」的既有降级语义）。
    try:
        statements, _stmt_prof = _stmt_fut.result()
        profile.update(_stmt_prof or {})
    except Exception as _e:  # noqa: BLE001
        statements = {}
        logger.warning("[main] 并行财报拉取失败，以空数据继续：%s", _e)
    if "fin_summary" not in profile:
        profile["fin_summary"] = {}
    _stmt_ex.shutdown(wait=False)

    # 回归修复（措施2 引入）：curated peers 的本公司行 name 取自 _self_name，
    # 而 _self_name 在池启动前求值——彼时公司名尚未就绪，code-only 请求会把
    # 本公司行落成股票代码（601138 报告对比图/表显示"601138"）。原串行流程
    # 先 identity 后 peers 无此问题。此处池已结束，从 identity 层结果取真实
    # 公司名回填本公司行（hl 标记或代码精确匹配），仅替换纯代码形态的名字。
    _cninfo = profile.get("cninfo") or {}
    # 简称优先：cninfo 的 name 在「东财画像失败」场景下可能被填成工商全称，
    # 故 short_name（A股简称）优先于 name，保证抬头是「东山精密（002384）」口径。
    _pn = (
        profile.get("name")
        or _cninfo.get("short_name")
        or _cninfo.get("name")
        or entity.name
    )
    _pn = str(_pn).strip() if _pn else ""
    if re.fullmatch(r"\d{6}", _pn):
        # 抬头修复（2026-09-12）：纯股票代码不是公司名，视为未取到
        _pn = ""
    if _pn:
        # 抬头修复（2026-09-12）：上述链路此前**只用于回填 peers 本公司行**，
        # 未回写 profile["name"]。东财个股画像接口（get_company_profile 的
        # stock_individual_info_em）限流时拿不到名称，顶层 name 便退化为股票
        # 代码，概况报告抬头渲染成「002384（002384）」——而正确名称其实已在
        # 六层 identity 层（cninfo 口径）里。此处回写顶层 name，使 result.name
        # 与抬头同源，不再退化为代码。（也顺带修正基本面报告与产物复用的名称）
        profile["name"] = _pn
    if _pn:
        for _p in profile.get("peers") or []:
            if not isinstance(_p, dict):
                continue
            if (_p.get("hl") or _p.get("code") == _code) and re.fullmatch(
                r"\d{6}", str(_p.get("name") or "")
            ):
                _p["name"] = _pn

    # 年报一手来源兜底：cninfo 分红接口缺最新年度（如 2025），从年报 PDF 抽取
    # 利润分配决策与股份回购实施结果，并入 dividend（作为最新一期）与 repurchase 字段，
    # 杜绝把历史方案误当最新、或 LLM 凭记忆编造回购股数。
    try:
        from fundamental_agent.data.annual_report import get_corporate_actions
        _ca = get_corporate_actions(entity.code)
        if _ca:
            if _ca.get("dividend"):
                profile["dividend"] = _ca["dividend"] + (profile.get("dividend") or [])
            if _ca.get("repurchase"):
                profile["repurchase"] = _ca["repurchase"]
            if _ca.get("controller"):
                # 实际控制人（年报口径）：前十大股东快照只反映最新报告期直接持股，
                # 会弱化「自然人低比例控股」识别，故单列年报一手口径。
                profile["actual_controller"] = _ca["controller"]
    except Exception as _e:  # noqa: BLE001
        logger.warning("[main] 年报公司行动抽取失败：%s", _e)

    _emit("运行分析引擎", 35)
    modules: list = []
    total = max(1, len([e for e in ENGINE_REGISTRY if not e.placeholder]))
    for i, engine in enumerate(ENGINE_REGISTRY):
        if getattr(engine, "placeholder", False):
            continue  # 措施5：占位引擎直接跳过（当前无占位，防回归开关）
        try:
            modules.append(engine(client).run(entity.code, statements))
        except Exception:
            # 单引擎失败不应中断整份报告
            pass
        _emit("运行分析引擎", 35 + int(40 * (i + 1) / total))

    # P1：年报一手来源抽取上下游（PyMuPDF 解析，PDF 本地缓存；失败优雅降级）
    try:
        from fundamental_agent.data.annual_report import get_business_chain
        profile["business_chain"] = get_business_chain(entity.code)
    except Exception as e:  # noqa: BLE001
        print(f"[overview] 年报上下游抽取失败，跳过：{e}", file=sys.stderr)
        profile["business_chain"] = None

    # P1 补齐传播：build_profile 在上面的抽取之前已运行，故 layers.business.business_chain
    # 当时为 None；此处把顶层 business_chain 同步进 layers.business，否则概况报告渲染层
    # （overview.py）读取不到，上下游一手来源 + 交叉验证章节会整段缺失。
    if profile.get("business_chain"):
        profile.setdefault("layers", {}).setdefault("business", {})["business_chain"] = profile["business_chain"]

    # 前十大股东：定期报告**股东名册**（监管披露口径，含性质/总持股/质押数量）。
    # 东财「前十大股东」为流通快照口径（实控人仅显示无限售部分 2.78%），与名册
    # （11.10%，含限售）差异巨大，须以名册为准。半年报优先，缓存命中零网络。
    # 注意：本块含 fitz（PyMuPDF），必须保持在并发池**之后**执行——fitz 先于
    # MiniRacer 加载会与池内 cninfo V8 调用交互导致间歇性进程崩溃（OV-2 实测）。
    try:
        import re as _re
        from fundamental_agent.data.annual_report import (
            fetch_annual_report as _fetch_rep,
            extract_top_holders as _ext_holders,
        )
        _rep_meta = _fetch_rep(entity.code, kind="semi") or _fetch_rep(entity.code)
        if _rep_meta:
            _rhs = _ext_holders(_rep_meta["pdf_path"])
            if len(_rhs) >= 5:
                _t = _rep_meta.get("title") or ""
                _ym = _re.search(r"(\d{4})年(半)?年度报告", _t)
                if _ym:
                    _period = f"{_ym.group(1)} 年中报" if _ym.group(2) else f"{_ym.group(1)} 年年报"
                else:
                    _period = _t
                profile["report_holders"] = {
                    "holders": _rhs,
                    "title": _t,
                    "period": _period,
                }
                # OV-2：名册覆盖 holders（更优口径）；消费方读 股东名称/持股数/名次
                profile["holders"] = [
                    {
                        "名次": _i + 1,
                        "股东名称": _h.get("name") or "",
                        "报告期": _period,
                        "持股数": _h.get("shares"),
                        "增减": "—",
                        "变动比率": "—",
                        "性质": _h.get("nature") or "",
                        "质押数量": _h.get("pledge_num"),
                    }
                    for _i, _h in enumerate(_rhs)
                ]
    except Exception as e:  # noqa: BLE001
        print(f"[overview] 股东名册抽取失败，回退东财口径：{e}", file=sys.stderr)

    # OV-2 兜底（2026-09-09 解耦）：只要 holders 为空就串行补取东财口径，
    # 不再以是否跳过 get_holders 为前置条件——此前名册未覆盖且六层取数层丢失时
    # 该兜底永不触发，导致治理区块（前十大股东）整块为空且无法自愈。
    if not (profile.get("holders") or []):
        try:
            profile["holders"] = _safe_records(lambda: client.get_holders(entity.code))
            logger.info(
                "[main] holders 为空，已串行补取东财口径：%d 行",
                len(profile.get("holders") or []),
            )
        except Exception:
            profile["holders"] = []

    result = AnalysisResult(
        name=profile.get("name") or entity.name or code,
        code=entity.code,
        modules=modules,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        profile=profile,
    )

    # 校验闸门（副作用：标记缺失指标为「未获取」）+ 装配六层 JSON 契约
    _emit("校验数据", 78)
    profile_dict = build_profile(result)
    validation = profile_dict["validation"]

    if use_llm:
        # OV-1（性能优化·概况专项）：summarize_analysis 仅 fundamental 需要——
        # overview 的 AI 结论由 generate_overview_content.conclusion_html 提供，
        # overview.py 全链路不消费 llm_summary，此前对 overview 是一次无用 LLM 调用（10-30s）。
        if report_type == "fundamental":
            _emit("生成 AI 结论", 80)
            # B2（速度优化）：AI 结论与模块润色互不依赖（都只读 result），并行执行——
            # LLM 阶段耗时 = max 而非 sum（实测省 10-30s）；单调用失败仍回退空值。
            from concurrent.futures import ThreadPoolExecutor as _TPE2

            def _sum_task():
                try:
                    return summarize_analysis(result)
                except Exception:
                    return ""

            def _polish_task():
                try:
                    return polish_module_insights(result) or {}
                except Exception:
                    return {}

            with _TPE2(max_workers=2) as _ex2:
                _f_sum = _ex2.submit(_sum_task)
                _f_pol = _ex2.submit(_polish_task)
                result.llm_summary = _f_sum.result()
                result.llm_insights = _f_pol.result()

    search_used = False
    written: str | None = None
    if out_path:
        _emit("渲染报告", 92)
        template_dir = str(Path(__file__).resolve().parent / "report")
        if report_type == "overview":
            # 公司概况报告：独立模板 + LLM 生成内容（B①）；失败降级空壳，不污染基本面
            # Step 2（WorkBuddy 复刻 M2）：检索代理补充定性层（合作关系/行业格局/近期动态）。
            # 摘要注入 dynamics.research 后随 profile_dict 一并进入 LLM 输入（见 overview.md
            # 第 9 条使用规则）；任何失败降级为无检索，pipeline 不中断，等价现状。
            if use_llm:
                try:
                    from fundamental_agent.agents.search_agent import run_search_agent

                    research = run_search_agent(result.name, entity.code)
                    if research:
                        profile_dict.setdefault("layers", {}).setdefault(
                            "dynamics", {}
                        )["research"] = research
                        search_used = True
                        print(
                            f"[search_agent] 已注入检索摘要：后端={'+'.join(research['backends'])}，"
                            f"条目={research['sources_count']}，"
                            f"综合={'成功' if research['synthesis_ok'] else '失败（条目仍注入）'}"
                        )
                except Exception as e:  # noqa: BLE001 — 检索代理异常绝不阻断报告
                    logger.warning("[main] search_agent 失败，按无检索继续：%s", e)
            llm_json = None
            if use_llm:
                try:
                    llm_json = generate_overview_content(result, profile_dict, result.name, entity.code)
                except Exception as e:  # noqa: BLE001 — 失败降级空壳，但必须留痕（曾静默失败难排查）
                    logger.warning("[main] generate_overview_content 异常，降级空壳：%s", e)
            ov_data = build_overview_data(result, profile_dict, llm_json, use_llm=use_llm)
            written = render_overview(ov_data, template_dir=template_dir, out_path=out_path)
            # Step 3（WorkBuddy 复刻 M4）：渲染后 JS 语法校验闭环（node --check）。
            # 校验失败不阻断产物：contract 模式模板 JS 静态可信，此闭环主要为
            # writer 直写模式（M3）兜底；失败详情留日志，未来据以回传重试。
            if written:
                try:
                    from fundamental_agent.agents.validate_html import check_html_js

                    _js_cfg: dict = {}
                    try:
                        import yaml as _yaml

                        _cfg_path = Path(__file__).resolve().parents[2] / "config.yaml"
                        with open(_cfg_path, encoding="utf-8") as _f:
                            _js_cfg = (
                                (_yaml.safe_load(_f) or {}).get("overview_writer") or {}
                            ).get("js_check") or {}
                    except Exception:  # noqa: BLE001 — 配置缺失按默认开
                        pass
                    if _js_cfg.get("enabled", True):
                        _html = Path(written).read_text(encoding="utf-8")
                        _res = check_html_js(
                            _html,
                            node_path=_js_cfg.get("node_path") or None,
                            timeout=int(_js_cfg.get("timeout") or 30),
                        )
                        if _res["status"] == "passed":
                            print(
                                f"[js_check] PASS：{_res['blocks']} 个内联脚本块语法校验通过"
                            )
                        elif _res["status"] == "skipped":
                            logger.warning(
                                "[js_check] SKIP：%s（%d 块未校验）",
                                _res.get("reason"),
                                _res["blocks"],
                            )
                        else:
                            for _e in _res["errors"]:
                                logger.warning(
                                    "[js_check] FAIL block %s：%s",
                                    _e["block"],
                                    _e["message"],
                                )
                except Exception as e:  # noqa: BLE001 — 校验器自身异常绝不阻断报告
                    logger.warning("[main] js_check 异常，跳过：%s", e)
        else:
            # 公司基本面报告：沿用既有 render() + template.html；LLM 润色的解读在 build_data 内覆盖
            # （行业对比报告在函数前部已旁路返回，此处不再处理）
            written = render(result, template_dir=template_dir, out_path=out_path)

    _emit("完成", 100)
    return {
        "result": result,
        "out_path": written,
        "code": entity.code,
        "name": result.name,
        "validation": {
            "missing_marked": validation["missing_marked"],
            "warnings": validation["warnings"],
            "suspicious_llm_numbers": validation["suspicious_llm_numbers"],
        },
        "profile_json": profile_dict,
        "search_used": search_used,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="公司基本面分析 Agent")
    parser.add_argument("--name", default="", help="公司名称（支持中文名，自动解析为代码）")
    parser.add_argument("--code", default="", help="股票代码，如 600519")
    parser.add_argument("--source", default="akshare", choices=["akshare", "dummy", "tushare"],
                        help="数据源：akshare(默认,真实免费) / dummy(占位) / tushare(需--token)")
    parser.add_argument("--dummy", action="store_true", help="等价 --source dummy")
    parser.add_argument("--token", default="", help="Tushare Pro token（--source tushare 时使用）")
    parser.add_argument("--type", default="fundamental",
                        choices=["fundamental", "overview", "industry"],
                        help="报告类型：fundamental(基本面,默认) / overview(公司概况) / industry(行业对比)")
    parser.add_argument("--no-llm", action="store_true", help="跳过 DeepSeek AI 结论生成")
    _project_root = Path(__file__).resolve().parent.parent.parent
    parser.add_argument("--out", default=str(_project_root / "output" / "report.html"),
                        help="输出 HTML 路径（默认 project_root/output/report.html）")
    args = parser.parse_args()

    use_dummy = args.dummy or args.source == "dummy"
    use_tushare = args.source == "tushare"

    def _print_progress(stage: str, pct: int) -> None:
        print(f"[{pct:3d}%] {stage}")

    info = run_pipeline(
        name=args.name,
        code=args.code,
        use_dummy=use_dummy,
        token=args.token if use_tushare else "",
        use_llm=not args.no_llm,
        report_type=args.type,
        out_path=args.out,
        on_progress=_print_progress,
    )
    print(f"[OK] 报告已生成：{info['out_path']}")


if __name__ == "__main__":
    main()
