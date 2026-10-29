"""报告导出（F-07）：把已落盘报告变成**能带离产品、能发给别人**的文件。

## 目标与三种格式的分工

| 格式 | `fmt` | 使用场景 | 实现 |
|---|---|---|---|
| PDF | `pdf` | **发给同事 / 客户 / 存档打印**——通用载体，对方不需要账号 | 无头浏览器打印（A4、无页眉页脚） |
| 长图 | `png` | **发微信 / 钉钉 / 贴进文档**——一张图，对方不必装阅读器 | 无头浏览器整页截图 + 自适应裁掉底部空白 |
| Word | `docx` | **引用 / 编辑**——把结论与数据摘进自己的材料 | HTML → 结构化 OOXML + 从 PDF 抽图表图片 |

## 为什么不用 `fitz` 直接把 HTML 转 PDF（清单原文的设想）

清单 F-07 的原始设想是"`requirements.txt` 已含 `pymupdf`，可零新增依赖用于 HTML→PDF"。
实测**这条路走不通**：报告里的图表是 **JS 在运行时画到 `<canvas>` 上的**（概况报告 6 个
Chart.js canvas、板块报告 ECharts 动态建 canvas），而 `fitz.Story` **不执行 JavaScript**，
转出来的 PDF **图表整块空白**。那正是本项目明令禁止的形态——"看起来有文件、实际缺内容"。

于是改为**用真实浏览器渲染**：本机已装的 Chrome/Edge 提供 `--print-to-pdf` 与
`--screenshot`，与"用户点开报告"走**同一条渲染路径**，因此有一条朴素而重要的推论：

> **报告里能看到什么，导出里就能看到什么。**（反之，报告因图表库 CDN 缺失而空白时，
> 导出也空白 —— 这不是导出引入的问题，且已由 F-06 的 `chart_offline` 字段在列表里暴露。）

同时仍然满足清单的"零新增依赖"：Python 侧只用已装的 `pymupdf`（校验 PDF、抽图表图片）
与 `Pillow`（长图裁剪），Word 侧是自写的 OOXML（`docx_writer.py` + 内置 `zipfile`）。

## 与现有产品的接口一致性

- **身份复用 F-06 的持久 `report_id`**：导出目标与"列表里能打开的那一份"是同一个标识。
- **路径解析复用 `report_index.resolve_report_dir`**：因此
  **「能打开的报告」⟺「能导出的报告」**（含 legacy 一律 404，与 view 同口径 ——
  绝不让导出成为"绕过 view 路径边界的第二条路"）。
- **端点挂既有 `/api/report` 前缀**：8000 网关不需要新增分支（N-23 的正面样本）。
- **不做落盘缓存**：导出物是**纯派生**数据，落盘就会引出"报告被重新生成后导出物何时失效"
  的第二个失效模型，而 F-06 刚把"文件即索引"定死。实测单次导出 2~8 秒，可直接按需生成。

## 资源治理（不治理就是把机器打爆）

浏览器每次调用要起一个完整进程（约 150MB、2~8 秒）。故：
- **有界并发**（`MAX_CONCURRENT_EXPORTS`），拿不到槽位立刻返回"导出繁忙"而不是排队堆积；
- 每次调用用**独立的 `--user-data-dir`**（否则 Chrome 会把请求交给已有会话并立刻返回，
  **产物根本不生成** —— 这是本机实测踩到的第一个坑）；
- 每次调用一个临时工作目录，用完即删，并顺带清理历史残留。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote

from fundamental_agent import docx_writer

# ===== 格式注册表（单一事实源） =====
#
# 与 `report_index.REPORT_TYPES` 同一取向：中文名与 MIME 只在这里写一次，
# 接口自省、文件名、前端选择项都从它派生，避免"新增一种格式要改四处"。
FORMATS: dict[str, dict[str, str]] = {
    "pdf": {
        "label": "PDF",
        "ext": "pdf",
        "mime": "application/pdf",
        "hint": "适合发给同事、客户或存档打印",
    },
    "png": {
        "label": "长图",
        "ext": "png",
        "mime": "image/png",
        "hint": "一张图，适合发微信、钉钉或贴进文档",
    },
    "docx": {
        "label": "Word",
        "ext": "docx",
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "hint": "结构化文档，适合摘取与二次编辑",
    },
}
FORMAT_ORDER: list[str] = ["pdf", "png", "docx"]

# ===== 可调参数 =====

#: 显式指定浏览器可执行文件。设了但不存在 → **视为不可用而不是悄悄换别的**：
#: 静默回退会让人以为"我配的那个就是被用的那个"。
BROWSER_ENV = "REPORT_EXPORT_BROWSER"

#: 本机已验证可用的常见安装位置（先 Chrome 后 Edge：Edge 对 `--print-to-pdf` 的
#: 页眉页脚行为与 Chrome 略有差异，Chrome 优先更可预期）。
_BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

#: 等 JS 把图表画完的虚拟时间预算（毫秒）。给足是为了让 Chart.js / ECharts 的
#: 入场动画在打印前跑完 —— 动画没跑完时 `<canvas>` 是**空的**，PDF 里就是白框。
VIRTUAL_TIME_BUDGET_MS = 10000

#: 浏览器单次调用超时（秒）。长图要渲染很高的页面，给得更宽。
PDF_TIMEOUT_S = 120
PNG_TIMEOUT_S = 180

#: 长图目标宽度（CSS px）。选 1080 的理由：①大于模板的 920px 桌面断点，
#: 不会被切到移动版式；②与手机屏宽同量级，发出去不缩水。
PNG_WIDTH = 1080
#: 长图视口高度上限。报告实测 2700~7800px，20000 有足够余量；
#: 真超了就**报错**而不是给半张图（见 `_shoot` 的截断判定）。
PNG_HEIGHT_CAP = 20000
#: 截断后自动重试的上限（再超就明确失败）
PNG_HEIGHT_CAP_MAX = 32000
#: 判定"这次视口高度够用"所需的底部空白余量（px）。见 `_trim_bottom`：
#: 1px 判据区分不了"页面到此结束"与"恰好在一条空白缝里被切断"（后者会静默丢掉下半页），
#: 改用一个宽松余量把这种巧合压到极小。代价只是长报告多渲染一次。
PNG_SAFE_MARGIN = 120

#: 内联 SVG 图表的栅格化倍率。x2 是为了贴进 Word 后不糊（图表里全是 12~14px 的小字）。
SVG_CHART_SCALE = 2
#: 拿不到 viewBox 时的兜底尺寸 —— 与模板 `drawChartSVG` 的 760×280 一致。
SVG_CHART_DEFAULT_W = 760
SVG_CHART_DEFAULT_H = 280
#: 一次栅格化的总高度上限（超大时明确失败，而不是让浏览器画出半张）
SVG_TOTAL_H_CAP = 16000

#: 同时进行的浏览器调用数。超过就返回"繁忙"——排队会让用户以为"点了没反应"。
MAX_CONCURRENT_EXPORTS = 2

#: 临时目录名。
#: ⚠️ 两者必须**分开**：工作目录每次用完即删，而 profile 要**常驻复用** ——
#: 实测删除一个 Chrome profile（上万个碎文件）在 Windows 上要 20 秒以上，
#: 每轮新建+删除会让一次 4 秒的导出变成 30 秒。profile 按并发槽位固定分配，
#: 于是既不会两个进程抢同一个目录，也不用反复付创建/删除的钱。
_WS_PREFIX = "tradeai_exp_ws_"
_PROFILE_PREFIX = "tradeai_exp_profile_"
#: 残留工作目录的清理阈值（分钟）：兜住"客户端中断导致清理没跑"的情况。
_WS_STALE_MIN = 30

# 槽位池：索引即 profile 编号。用一组锁而不是 BoundedSemaphore，就是为了拿到编号。
_SLOT_LOCKS: list[threading.Lock] = [threading.Lock() for _ in range(MAX_CONCURRENT_EXPORTS)]

#: A4 打印样式。**注入到临时副本**而不是改报告本体：
#: 导出是"把报告换一个载体"，不该反过去改产物（改了就与报告库显示的不一致）。
#: 宽度/边距按 A4（210×297mm）+ 12mm 边距，符合国内打印与阅读习惯。
_PRINT_CSS = (
    "<style id=\"__export_print__\">"
    "@page{size:A4;margin:12mm}"
    "html,body{background:#fff!important}"
    "</style>"
)

#: 供 `--dump-dom` / 断言用的注入标记（说明导出确实走了预处理副本这条路径）
PRINT_STYLE_MARK = "__export_print__"


class ExportError(Exception):
    """导出失败（有明确原因，可回给用户）。"""


class ExportUnavailable(ExportError):
    """当前环境下该格式不可用（如找不到浏览器）——调用方应回 503 并带上 reason。"""


class ExportBusy(ExportError):
    """并发槽位已满 —— 调用方应回 503 让用户稍后重试（而不是排队阻塞）。"""


# ===== 并发槽位与临时目录 =====


def _acquire_slot() -> "_Slot":
    """占用一个并发槽位；占不到立刻抛 `ExportBusy`。

    **不阻塞排队**：导出要跑 4~8 秒，排队只会让用户以为"点了没反应"，
    明确回一句"导出繁忙，请稍后重试"要好得多（与 N-24「进度条永远转圈」同一取向）。
    """
    for idx, lk in enumerate(_SLOT_LOCKS):
        if lk.acquire(blocking=False):
            return _Slot(idx, lk)
    raise ExportBusy("导出任务已达并发上限（%d），请稍后重试" % MAX_CONCURRENT_EXPORTS)


class _Slot:
    """上下文管理器：保证异常路径也归还槽位（漏归还 = 该功能永久瘫痪）。"""

    def __init__(self, idx: int, lock: threading.Lock) -> None:
        self.idx = idx
        self._lock = lock

    def __enter__(self) -> "_Slot":
        return self

    def __exit__(self, *exc) -> bool:
        self._lock.release()
        return False


class ExportContext:
    """一次导出的运行上下文：**独占**的工作目录与浏览器 profile。

    两者都跟着并发槽位走，因此"两个导出同时进行"不会互相踩 profile，
    也不会互相删掉对方的工作目录。
    """

    def __init__(self, workdir: Path, profile: Path) -> None:
        self.workdir = workdir
        self.profile = profile


def profile_dir(idx: int) -> Path:
    """该槽位专用的浏览器 profile 目录（常驻，不随单次导出删除）。"""
    return Path(tempfile.gettempdir()) / ("%s%d" % (_PROFILE_PREFIX, idx))


def _tmp_workspace() -> Path:
    """建一个本次导出专用的工作目录，并顺手清理历史残留。

    只放**本次产物**（打印副本、截图、日志、临时 PDF），很小，用完全删得很快。
    浏览器 profile 不走这里（见 `profile_dir` 的说明）。
    """
    root = Path(tempfile.gettempdir())
    _sweep_stale(root)
    return Path(tempfile.mkdtemp(prefix=_WS_PREFIX))


def _sweep_stale(root: Path) -> int:
    """清理超过 `_WS_STALE_MIN` 分钟的**工作目录**。

    为什么需要：客户端中断连接时后台清理可能不跑，残留会无限增长 ——
    "只在正常路径清理"是典型的资源泄漏写法。

    ⚠️ 显式排除 profile 目录：它是常驻复用的，被清掉会退化成"每轮重建 profile"
    （而那正是 30 秒耗时的来源）；更糟的是可能删掉另一个正在跑的导出的 profile。
    """
    cutoff = time.time() - _WS_STALE_MIN * 60
    n = 0
    try:
        for d in root.glob(_WS_PREFIX + "*"):
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
            except OSError:
                continue
    except OSError:
        pass
    return n


def _reset_profile(profile: Path) -> None:
    """清掉某槽位的 profile（仅在上一次运行留下锁文件时使用）。

    profile 会被复用，因此必须考虑"上一次 Chrome 被强杀、锁文件还在"这一情形：
    Chromium 这时会拒绝启动，日志里是 `profile appears to be in use`。清掉重来即可。
    """
    shutil.rmtree(profile, ignore_errors=True)


# ===== 浏览器发现 =====


def browser_path() -> str | None:
    """返回可用的浏览器可执行文件路径；找不到返回 None。

    顺序：环境变量 → 本机常见安装位置 → PATH。
    环境变量指向的文件不存在时**直接返回 None**（不静默回退）—— 否则"配了却没生效"
    这类问题会以"导出结果与预期不符"的形式出现，极难排查。
    """
    env = (os.environ.get(BROWSER_ENV) or "").strip().strip('"')
    if env:
        return env if Path(env).is_file() else None
    local = os.environ.get("LOCALAPPDATA")
    cands = list(_BROWSER_CANDIDATES)
    if local:
        cands.insert(2, str(Path(local) / "Google/Chrome/Application/chrome.exe"))
    for p in cands:
        try:
            if Path(p).is_file():
                return p
        except OSError:
            continue
    for name in ("google-chrome", "chrome", "chromium", "chromium-browser", "msedge"):
        w = shutil.which(name)
        if w:
            return w
    return None


def capabilities() -> dict:
    """自省：当前进程能导出哪些格式。**由服务端下发，前端不自己猜**。

    与列表契约里的 `deletable` 同一取向：能力判断只做一次，前端如实呈现。
    """
    b = browser_path()
    ok = b is not None
    return {
        "available": ok,
        "browser": (Path(b).name if b else ""),
        "reason": "" if ok else (
            "未找到可用的浏览器（Chrome / Edge）。导出依赖浏览器执行报告页的 JavaScript "
            "（图表是运行时绘制到 canvas 上的），故无法在缺少浏览器时降级为其他方式。"
            "可设置环境变量 %s 指定浏览器可执行文件。" % BROWSER_ENV
        ),
        "concurrency": MAX_CONCURRENT_EXPORTS,
        "formats": [
            {"key": k, "label": FORMATS[k]["label"], "ext": FORMATS[k]["ext"],
             "mime": FORMATS[k]["mime"], "hint": FORMATS[k]["hint"]}
            for k in FORMAT_ORDER
        ],
    }


# ===== 浏览器调用 =====


def _file_url(p: Path) -> str:
    """本地文件 URL。Windows 盘符必须写成 `file:///E:/...`（三条斜杠）。"""
    s = Path(p).resolve().as_posix()
    if not s.startswith("/"):
        s = "/" + s
    return "file://" + quote(s, safe="/:()!$&'*,;=@~-._")


def _base_args(profile: Path) -> list[str]:
    """所有调用共用的启动参数。

    ⚠️ `--user-data-dir` 必须是**该调用独占的目录**：本机实测，指向已有 Chrome 会话时
    Chrome 会把请求转交给那个实例并**立刻返回 0**，而产物根本没生成 —— 症状是
    "命令成功、文件不存在"，属于最难查的一类（与"失败伪装成不存在"同族）。
    但也不能每次新建：profile 里上万个碎文件，Windows 下建/删各要十几秒。
    故按**并发槽位**固定分配一个常驻 profile（见 `profile_dir`）。

    ⚠️ **不要加 `--disable-background-networking`**：报告在图表库内联失败时会回退 CDN
    （D4 的 `chart_offline:false`），封掉网络就等于让那类报告的图表全空白 ——
    导出会因此变成"静默缺内容"。
    """
    return [
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-default-apps",
        "--disable-component-update",
        "--disable-sync",
        # 关掉崩溃上报：它会在主进程退出后继续驻留，是"命令已经成功但还占着句柄"的来源之一
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--disable-background-timer-throttling",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--disk-cache-size=1048576",
        "--user-data-dir=%s" % profile,
    ]


_PROFILE_LOCK_RE = re.compile(
    r"already in use|SingletonLock|ProcessSingleton|profile appears to be in use", re.IGNORECASE)


def _run_browser(exe: str, make_args, timeout: int, ctx: "ExportContext",
                 *, want_stdout: bool = False) -> tuple[int, str]:
    """跑一次浏览器，返回 `(退出码, 合并后的输出文本)`。

    `make_args()` 是参数构造函数（而不是现成的列表）：profile 被占用时需要清掉重来，
    参数里的 `--user-data-dir` 也就得重新拼。

    `want_stdout=True` 时把 stdout 单独落盘（用 `_read_browser_stdout` 取回）——
    `--dump-dom` 需要它，见下面的说明。

    ⚠️ **绝不能用 `capture_output=True`**（= PIPE）。本机实测：Chrome 的 crashpad /
    utility 子进程会继承管道写端，于是 `communicate()` 要等到**所有**子进程退出才返回，
    一次 4 秒的打印会变成 **40 秒**（提速前后都量过）。改成把输出**重定向到文件**：
    只等主进程，同时仍然拿得到 stderr 供排错。

    ⚠️ 输出必须按 `errors="replace"` 方式解码：Windows 上 Chrome 会写**本地编码**
    （GBK）的日志，硬解 UTF-8 会抛 `UnicodeDecodeError`，表现为"命令跑完了却看不到原因"。
    """
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    last: tuple[int, str] = (0, "")
    for attempt in (1, 2):
        log = ctx.workdir / ("browser.log" if attempt == 1 else "browser_retry.log")
        # `out_file` 非空时把 **stdout 单独落盘**（stderr 仍进 log）：`--dump-dom` 会往 stdout
        # 打印整份 DOM，与 Chrome 的 stderr 噪音混在一个文件里会被**交错写坏**
        # （两个流共用一个 fd，写入边界不保证原子）。需要读回页面数据时必须分开。
        out_file = (ctx.workdir / ("browser.out" if attempt == 1 else "browser_retry.out")
                    if want_stdout else None)
        try:
            with open(log, "wb") as fh:
                fo = open(out_file, "wb") if out_file else fh
                try:
                    proc = subprocess.run(
                        [exe, *make_args()],
                        stdout=fo,
                        stderr=fh,
                        stdin=subprocess.DEVNULL,
                        timeout=timeout,
                        creationflags=creationflags,
                        check=False,
                    )
                finally:
                    if out_file:
                        fo.close()
            rc = proc.returncode
        except FileNotFoundError as e:
            raise ExportUnavailable("浏览器无法启动：%s" % e) from e
        except subprocess.TimeoutExpired as e:
            raise ExportError("导出超时（超过 %d 秒）" % timeout) from e
        try:
            text = log.read_bytes().decode("utf-8", "replace")
        except OSError:
            text = ""
        last = (rc, text)
        # profile 被上一次异常退出留下的锁占住 → 清掉重试一次（只重试这一种原因）
        if attempt == 1 and rc != 0 and _PROFILE_LOCK_RE.search(text):
            _reset_profile(ctx.profile)
            continue
        return last
    return last


def _read_browser_stdout(ctx: "ExportContext") -> str:
    """读 `_run_browser(want_stdout=True)` 落盘的 stdout（取重试后的那一份）。"""
    for name in ("browser_retry.out", "browser.out"):
        p = ctx.workdir / name
        try:
            return p.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
    return ""


def _log_tail(text: str, limit: int = 400) -> str:
    """取浏览器输出里**有用**的尾部：Chrome 的 stderr 噪音很大，
    真正的原因通常混在最后几行，故先过滤掉明显无关的行。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    keep = [ln for ln in lines
            if not re.search(r"externally_managed_app_manager|install source|"
                             r"voice_transcription|DEPRECATED_ENDPOINT|GCM|Registration response",
                             ln, re.IGNORECASE)]
    tail = " ".join(keep[-4:]) if keep else ""
    return re.sub(r"\s+", " ", tail)[-limit:]


def _check_output(out: Path, what: str, rc: int | None = None, log: str = "") -> int:
    """产物校验：文件必须存在且非空。**空产物必须报错，不许当成成功**。"""
    try:
        size = out.stat().st_size
    except OSError:
        # 把退出码与浏览器输出带进文案：`--print-to-pdf` 失败时 Chrome 的退出码常常是 0，
        # 唯一的原因线索就在 stderr 里。不带上它，排错只能靠回手工复现。
        tail = _log_tail(log)
        raise ExportError("浏览器未能生成%s（退出码 %s）%s"
                          % (what, rc, ("： " + tail) if tail else "")) from None
    if size <= 0:
        raise ExportError("浏览器生成的%s为空文件" % what)
    return size


# ===== PDF =====

_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)


def print_ready_html(html: str) -> str:
    """给报告 HTML 加上 A4 打印样式（导出专用副本）。

    注入点选 `</head>` 之前：@page / 背景色的取值以**后出现者为准**，放最后才能
    盖住模板自带的口径（`template.html` 里有一条 `@media print{@page{margin:10mm}}`）。
    没有 `</head>` 时（早期/外部产物）退化为加在文档最前 —— 不因为一处结构缺失就放弃整份文档。
    """
    if PRINT_STYLE_MARK in html:
        return html
    if _HEAD_CLOSE_RE.search(html):
        return _HEAD_CLOSE_RE.sub(_PRINT_CSS + "</head>", html, count=1)
    return _PRINT_CSS + html


def export_pdf(html_path: str | Path, out_path: str | Path, ctx: "ExportContext") -> dict:
    """HTML → PDF（无头浏览器打印）。返回 `{"pages", "chart_images", "bytes"}`。"""
    exe = browser_path()
    if exe is None:
        raise ExportUnavailable(capabilities()["reason"])

    html_path, out_path = Path(html_path).resolve(), Path(out_path).resolve()
    # 打印专用副本（A4 + 白底）：报告本体不动
    prep = ctx.workdir / "print.html"
    prep.write_text(print_ready_html(html_path.read_text(encoding="utf-8")), encoding="utf-8")

    def make_args() -> list[str]:
        return _base_args(ctx.profile) + [
            "--no-pdf-header-footer",
            "--virtual-time-budget=%d" % VIRTUAL_TIME_BUDGET_MS,
            "--print-to-pdf=%s" % out_path,
            _file_url(prep),
        ]

    rc, log = _run_browser(exe, make_args, PDF_TIMEOUT_S, ctx)
    size = _check_output(out_path, " PDF", rc, log)

    pages, charts = _inspect_pdf(out_path)
    if pages < 1:
        raise ExportError("生成的 PDF 没有可读页面")
    return {"pages": pages, "chart_images": charts, "bytes": size}


def _inspect_pdf(pdf_path: Path) -> tuple[int, int]:
    """校验 PDF 可解析，并数出内嵌图片数。

    为什么要数图片：**图表是通过 `<canvas>` 画的，打印后会成为内嵌位图**。
    图片数为 0 而 HTML 里明明有图表占位符 → 说明图表库没跑起来（CDN 不通 / JS 报错），
    这时导出的 PDF 是"有文件、没图表"。我们把它变成**可见的告警**而不是静默产物。
    """
    try:
        import pymupdf  # 局部导入：本模块只在导出时被调用，避免拖慢后端启动
    except Exception as e:  # noqa: BLE001
        raise ExportError("缺少 pymupdf，无法校验导出结果：%s" % e) from e
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            pages = doc.page_count
            imgs = 0
            for page in doc:
                imgs += len(page.get_images(full=True))
    except Exception as e:  # noqa: BLE001
        raise ExportError("生成的 PDF 无法解析：%s" % e) from e
    return pages, imgs


def extract_pdf_images(pdf_path: Path) -> list[tuple[bytes, str]]:
    """把 PDF 里的内嵌图片按文档序取出，供 Word 版插图。

    为什么不单独给每个图表截图：Chrome 打印时**已经把每个 canvas 光栅化成内嵌位图**了，
    直接复用同一份产物即可 —— 少跑一次浏览器（每次数秒），且 Word 版与 PDF 版看到的是
    **同一张图**（不会出现"两个格式的图表长得不一样"）。
    """
    import pymupdf
    out: list[tuple[bytes, str]] = []
    with pymupdf.open(str(pdf_path)) as doc:
        for page in doc:
            for info in page.get_image_info(xrefs=True):
                xref = info.get("xref")
                if not xref:
                    continue
                try:
                    raw = doc.extract_image(xref)
                except Exception:  # noqa: BLE001 — 抽不出单张图不该让整次导出失败
                    continue
                blob = raw.get("image")
                if blob:
                    out.append((bytes(blob), str(raw.get("ext") or "png")))
    return out


# ===== 长图 PNG =====


def export_png(html_path: str | Path, out_path: str | Path, ctx: "ExportContext") -> dict:
    """HTML → 长图 PNG（整页截图 + 裁掉底部空白）。返回 `{"width","height","bytes"}`。"""
    exe = browser_path()
    if exe is None:
        raise ExportUnavailable(capabilities()["reason"])
    html_path, out_path = Path(html_path).resolve(), Path(out_path).resolve()

    cap = PNG_HEIGHT_CAP
    while True:
        raw = ctx.workdir / ("shot_%d.png" % cap)

        def make_args() -> list[str]:
            return _base_args(ctx.profile) + [
                "--window-size=%d,%d" % (PNG_WIDTH, cap),
                "--virtual-time-budget=%d" % VIRTUAL_TIME_BUDGET_MS,
                "--screenshot=%s" % raw,
                _file_url(html_path),
            ]

        rc, log = _run_browser(exe, make_args, PNG_TIMEOUT_S, ctx)
        _check_output(raw, "长图", rc, log)

        try:
            w, h, complete = _trim_bottom(raw, out_path)
        except Exception as e:  # noqa: BLE001
            raise ExportError("长图裁剪失败：%s" % e) from e
        if complete:
            break
        # 底部余量不足 → 很可能被截断。**绝不输出半张图**：要么加高重试，要么明确报错。
        if cap >= PNG_HEIGHT_CAP_MAX:
            raise ExportError(
                "报告过长（超过 %d px），单张长图无法完整容纳。请改用 PDF 导出。" % cap)
        cap = min(cap * 2, PNG_HEIGHT_CAP_MAX)

    if h < 40:
        raise ExportError("长图内容高度异常（%d px），导出失败" % h)
    return {"width": w, "height": h, "bytes": out_path.stat().st_size,
            "safe_margin": PNG_SAFE_MARGIN}


def _background_rgb(px, w: int, h: int) -> tuple[int, int, int]:
    """估计页面背景色：对**全图抽样做颜色直方图**，取最多的那一档。

    为什么不直接取最后一个像素：报告底部可能是页脚条 / 白卡片，直接取会把背景认错，
    于是一路向上把整页都当成"有内容"（裁不掉）或把内容当背景（**裁掉正文** —— 更糟）。

    返回值是**量化到 8 的倍数**的档位（`r // 8 * 8`），不是原始像素值 —— 屏幕上同一片
    背景会有轻微色差，不量化会让"同一个背景"被拆成几十个颜色、谁都拿不到多数票。

    已知边界：内容像素占比超过一半时（本项目的报告是浅色卡片铺在白底上，远达不到），
    众数会落到内容色上。此时 `_trim_bottom` 的结论是"未完整容纳" → 调用方加高重试，
    最终明确报错 —— 属**安全方向**（宁可报错，也不静默给半张图）。
    """
    from collections import Counter
    c: Counter = Counter()
    for y in range(0, h, max(1, h // 600)):
        for x in range(0, w, max(1, w // 120)):
            r, g, b = px[x, y]
            c[(r // 8 * 8, g // 8 * 8, b // 8 * 8)] += 1
    return c.most_common(1)[0][0]


def _trim_bottom(src: Path, dst: Path) -> tuple[int, int, bool]:
    """裁掉底部空白并另存；返回 `(宽, 内容高, 是否已完整容纳)`。

    `是否已完整容纳` 的判据是**内容底部离视口下沿还剩多少空白**：留了
    `PNG_SAFE_MARGIN` 以上 → 认为这次视口高度足够，内容只是没铺满。少于这个余量
    （包括内容正好贴到最下沿）→ 视为**可能被截断**，让调用方加高重试，最终明确报错。

    ⚠️ 为什么不用"贴到最下沿"这一条 1px 判据：从**截图本身**无法区分"页面到此结束"
    与"页面恰好在一条空白缝里被切断"（后者会静默丢掉下半页）。改用一个宽松的安全余量，
    把这种巧合的概率压到很小 —— 代价只是长报告多渲染一次，而收益是绝不静默给半张图。
    """
    from PIL import Image
    with Image.open(src) as im0:
        im = im0.convert("RGB")
        w, h = im.size
        px = im.load()
        bg = _background_rgb(px, w, h)

        def differs(c) -> bool:
            return (abs(c[0] - bg[0]) > 8 or abs(c[1] - bg[1]) > 8 or abs(c[2] - bg[2]) > 8)

        step_x = max(1, w // 200)
        bottom = 0
        for y in range(h - 1, -1, -1):
            hit = False
            for x in range(0, w, step_x):
                if differs(px[x, y]):
                    hit = True
                    break
            if hit:
                bottom = y + 1
                break
        if bottom <= 0:
            # 注意文案：这里也可能是"内容配色与背景过于接近"而不是空白 —— 别让用户
            # 被一句"长图为空白"带到错误的方向（N-18 的教训：误导性文案会把排查带偏）。
            raise ExportError(
                "长图中未检出背景色以外的内容（页面可能是空白，或内容配色与背景过于接近）")
        complete = bottom <= (h - PNG_SAFE_MARGIN)
        pad = 24
        keep = min(h, bottom + pad)
        # 截断时也存一张供排错对照，但**调用方不会用它**（会加高重试或报错）
        im.crop((0, 0, w, keep)).save(dst, format="PNG", optimize=True)
        return w, keep, complete


# ===== Word =====


#: 图表搬运页里每行的固定高度（CSS px）。取 1200 是因为报告里最高的图表约 460px，
#: 留足余量；比这更高的图表由注入脚本**按比例缩小**（只会缩，不会被裁）。
_CHART_ROW_H = 1200
#: 搬运页宽度。比报告桌面断点（920px）宽，避免图表被切到移动版式。
_CHART_ROW_W = 1400
#: 注入脚本把"实际搬了多少行、每行多宽多高"写在这个属性上，供 `--dump-dom` 读回。
#: 值只含数字与 `,;`，不引引号 —— 属性里不能出现需要转义的字符。
_META_ATTR = "data-exp-capture"

_CAPTURE_JS = """
<script>
(function(){
  var SEL = 'div.chart, svg.svgchart, canvas';
  var ROW = %(row)d;
  function roots(){
    var out = [];
    Array.prototype.forEach.call(document.querySelectorAll(SEL), function(el){
      var a = el.parentElement;
      while(a && a !== document.body && a !== document.documentElement){
        if(a.matches && a.matches(SEL)) return;   /* 已被外层图表元素代表 */
        a = a.parentElement;
      }
      out.push(el);
    });
    return out;
  }
  var done = false;
  function run(){
    if(done) return;
    done = true;
    var nodes = roots(), meta = [], host = document.createElement('div');
    nodes.forEach(function(el){
      var r = el.getBoundingClientRect();
      var w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
      var box = document.createElement('div');
      box.style.cssText = 'width:' + w + 'px;height:' + ROW + 'px;background:#ffffff;'
                        + 'overflow:hidden;margin:0;padding:0;border:0;';
      var inner = document.createElement('div');
      inner.style.cssText = 'width:' + w + 'px;height:' + h + 'px;margin:0;padding:0;'
                          + 'transform-origin:top left;overflow:hidden;';
      if(h > ROW){ inner.style.transform = 'scale(' + (ROW / h) + ')'; }
      inner.appendChild(el);        /* 移动 DOM 节点 → canvas 的位图不会丢 */
      box.appendChild(inner);
      host.appendChild(box);
      meta.push(w + ',' + h);
    });
    while(document.body.firstChild){ document.body.removeChild(document.body.firstChild); }
    document.body.style.cssText = 'margin:0;padding:0;background:#ffffff;';
    document.body.appendChild(host);
    document.documentElement.setAttribute('%(attr)s', meta.join(';'));
  }
  /* ⚠️ 必须留一条**不依赖 load 事件**的兜底定时：报告里只要有一个资源（字体 / CDN 脚本）
     迟迟不返回，`load` 就永不触发，搬运脚本一次都不会跑 —— 症状是"截图里图表都在，
     但取回来的图表数是 0"，排查时极易误判成图表库没加载。实测板块报告就踩了这个坑。
     `done` 标志保证两条路径只跑一次。 */
  try{ if(document.readyState !== 'complete'){ window.addEventListener('load', run); } }catch(e){}
  setTimeout(run, 2500);
})();
</script>
"""


def _inject_capture_script(html: str) -> str:
    """把搬运脚本追加到报告副本里。

    ⚠️ 必须插在**最后一个** `</body>` 之前，不能用第一个匹配：
    报告会把图表库**内联**进 HTML（D4 的离线回退），而 ECharts 的库源码里就含有
    `</body>` 字符串（它的 `saveAsImage` 会拼 HTML）。按第一个匹配注入 = 把脚本塞进
    库源码的字符串中间，**整段库代码被当成正文渲染出来**，页面上出现一大坨 JS 源码、
    所有图表消失。实测板块报告（1050KB，内联了 ECharts）就是这么被打断的。
    """
    js = _CAPTURE_JS % {"row": _CHART_ROW_H, "attr": _META_ATTR}
    i = html.lower().rfind("</body>")
    if i >= 0:
        return html[:i] + js + html[i:]
    return html + js


def _parse_capture_meta(dom: str) -> list[tuple[int, int]]:
    """从 `--dump-dom` 的输出里读回搬运结果：`[(宽度, 高度), …]`。

    读回而不是"按静态标签数猜"：静态 HTML 里数到的图表位置，与页面 JS 实际画出来／
    模板运行时补出来的，未必一一对应。差一个就会让后面的图整体错位，
    而**错位在产物里看不出来**（每张图本身都是好的）。
    """
    m = re.search(r'%s="([0-9,;]*)"' % re.escape(_META_ATTR), dom or "")
    if not m or not m.group(1).strip():
        return []
    out: list[tuple[int, int]] = []
    for part in m.group(1).split(";"):
        bits = part.split(",")
        if len(bits) == 2 and bits[0].isdigit() and bits[1].isdigit():
            out.append((int(bits[0]), int(bits[1])))
    return out


def _trim_image(im, pad: int = 16):
    """裁掉图片**右侧与底部**的空白；整张都是背景色时返回 `None`。

    背景取自"全图抽样直方图的众数"（不是最后一个像素）：图表页每行右侧/底部是大片白，
    但个别图表底部就是深色柱子，取最后一个像素会把背景认错、把正文当空白裁掉。
    """
    w, h = im.size
    px = im.load()
    bg = _background_rgb(px, w, h)

    def differs(c) -> bool:
        return (abs(c[0] - bg[0]) > 8 or abs(c[1] - bg[1]) > 8 or abs(c[2] - bg[2]) > 8)

    step = max(1, min(w, h) // 200)
    right = 0
    for x in range(w - 1, -1, -1):
        if any(differs(px[x, y]) for y in range(0, h, step)):
            right = x + 1
            break
    bottom = 0
    span = right or w
    for y in range(h - 1, -1, -1):
        if any(differs(px[x, y]) for x in range(0, span, step)):
            bottom = y + 1
            break
    if right <= 0 or bottom <= 0:
        return None
    return im.crop((0, 0, min(w, right + pad), min(h, bottom + pad)))


def capture_chart_images(html_path: Path, ctx: "ExportContext",
                         *, expected: int = 0) -> tuple[list[bytes | None], list[str]]:
    """让页面把图表**自己搬出来**，一次截图取回全部图表图片。

    为什么不在 Python 侧从打印结果里抽图：实测**不可靠**。
    - ECharts（板块报告）一张图在页面上是**多层 canvas** 叠加（坐标轴一层、柱子一层），
      打印时各层被当成独立图片嵌入 —— 抽出来的是"只有坐标轴"的半张图；
    - 图表跨页时还会被切成上下两片（板块报告的两个小图正好压在分页线上）。

    为什么"搬"而不是"重画"：图表的像素只存在于运行时。移动 DOM 节点则**保留 canvas 位图**，
    ECharts 的多层 canvas 也一起搬走、合成关系不变。静态 HTML 里根本没有这些像素，
    所以这一步必须由页面自己完成。

    返回 `(按文档序的图片列表, 告警列表)`；取不到的位为 `None`（调用方留可见占位）。
    """
    # 复用 svg 提取时同样的"剥掉 script/style 再找"的思路并不适用于此处 ——
    # 这里必须让报告**完整地跑一遍 JS**，所以直接注入到原文副本。
    page = ctx.workdir / "charts.html"
    page.write_text(_inject_capture_script(html_path.read_text(encoding="utf-8")), encoding="utf-8")
    shot = ctx.workdir / "charts.png"

    exe = browser_path()
    if not exe:
        return [], ["本机未找到可用的浏览器，无法提取图表图片"]

    # 窗口高度按"静态数出来的位置数 + 2 行余量"给：给太多会截出一张几 MB 的巨图，
    # 给太少则多出来的图表落在视口外、**静默消失**。行数不对时下面的告警会兜住。
    win_h = _CHART_ROW_H * max(1, expected + 2)

    def make_args() -> list[str]:
        return _base_args(ctx.profile) + [
            "--window-size=%d,%d" % (_CHART_ROW_W, win_h),
            "--virtual-time-budget=%d" % VIRTUAL_TIME_BUDGET_MS,
            "--screenshot=%s" % shot,
            "--dump-dom",
            _file_url(page),
        ]

    rc, log = _run_browser(exe, make_args, PNG_TIMEOUT_S, ctx, want_stdout=True)
    _check_output(shot, "图表截图", rc, log)
    rows = _parse_capture_meta(_read_browser_stdout(ctx))

    warnings: list[str] = []
    if not rows:
        return [], ["页面未产出任何图表（可能是图表库未加载，或该报告确实没有图表）"]

    from PIL import Image
    out: list[bytes | None] = []
    with Image.open(shot) as im0:
        im = im0.convert("RGB")
        for k in range(len(rows)):
            top = k * _CHART_ROW_H
            if top + _CHART_ROW_H > im.height:
                out.append(None)
                warnings.append("第 %d 张图表超出截图高度，未能取到" % (k + 1))
                continue
            row = _trim_image(im.crop((0, top, im.width, top + _CHART_ROW_H)))
            if row is None or not _has_ink(row):
                out.append(None)
                continue
            buf = io.BytesIO()
            row.save(buf, format="PNG", optimize=True)
            out.append(buf.getvalue())
    return out, warnings


# ===== Word：内联 SVG 图表的兜底栅格化 =====
#
# 这条路径**不依赖报告页面的 JS**：SVG 的标记本来就在 HTML 里，只是它的样式来自模板 CSS、
# 且 PDF 里是矢量（抽不出位图）。所以自己把它交给浏览器画一遍更直接。
# 与 `capture_chart_images` 相互独立 —— 那一条靠页面 JS 把图表搬出来，两条都失效才没图。

#: 内联 SVG 图表的定位：开标签（保留原始大小写）、闭合标签、class/viewBox、样式块、脚本块。
#: ⚠️ 属性名大小写必须原样保留 —— 用 BeautifulSoup 重新序列化会把 `viewBox` 变成 `viewbox`，
#: 而 SVG 属性名大小写敏感，画不出来也不报错。
_SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
_SVG_CLOSE = "</svg>"
_CLASS_ATTR_RE = re.compile(r"\bclass\s*=\s*\"([^\"]*)\"", re.IGNORECASE)
_VIEWBOX_RE = re.compile(r"\bviewbox\s*=\s*\"([^\"]+)\"", re.IGNORECASE)
_CSS_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def extract_svg_charts(html: str) -> list[dict]:
    """取出报告里**标记中就有**的内联 SVG 图表：`[{"svg","w","h"}, …]`（文档序）。

    为什么要单独处理这类图表：它们的样式来自模板 CSS（`.svgchart .grid` 等），
    且是**矢量**的 —— 打印成 PDF 后依然是矢量，`extract_pdf_images()` 一张位图也抽不到，
    于是 Word 版会缺掉这些图。而它们的标记本来就在 HTML 里，**直接交给浏览器画一遍**
    就能拿到位图。

    ⚠️ 必须先从原文里剥掉 `<script>`：模板 JS 里也写着一模一样的
    `'<svg class="svgchart" …>'` 拼接字符串（运行时才塞进 `#xxxChartBox`）。
    不剥就会把这段**源码**当成真图表 —— 导出的 Word 里出现一张空白图，个数也对不上。
    """
    body = _SCRIPT_STYLE_RE.sub("", html)
    out: list[dict] = []
    for m in _SVG_OPEN_RE.finditer(body):
        open_tag = m.group(0)
        cls = _CLASS_ATTR_RE.search(open_tag)
        tokens = set((cls.group(1) if cls else "").split())
        if not ({"svgchart", "chart"} & tokens):
            continue
        end = body.find(_SVG_CLOSE, m.end())
        if end < 0:
            continue
        svg = body[m.start():end + len(_SVG_CLOSE)]
        w, h = SVG_CHART_DEFAULT_W, SVG_CHART_DEFAULT_H
        vb = _VIEWBOX_RE.search(open_tag)
        if vb:
            nums = [float(x) for x in _NUM_RE.findall(vb.group(1))]
            if len(nums) >= 4 and nums[2] > 0 and nums[3] > 0:
                w, h = nums[2], nums[3]
        out.append({"svg": svg, "w": w, "h": h})
    return out


def _svg_with_size(svg: str, w: int, h: int) -> str:
    """给 svg 打上**确定的像素尺寸**。

    ⚠️ 用字符串插入而不是 `BeautifulSoup` 重新序列化：bs4 的 `html.parser` 会把属性名
    **小写化**（`viewBox` → `viewbox`、`preserveAspectRatio` → `preserveaspectratio`），
    而 SVG 属性名大小写敏感 —— 重新序列化出来的 svg 在浏览器里会**画不出来**（或画歪），
    并且不会报任何错。
    """
    if not svg[:4].lower().startswith("<svg"):
        return svg
    return '%s style="width:%dpx;height:%dpx"' % (svg[:4], w, h) + svg[4:]


def render_svg_charts(html: str, charts: list[dict], ctx: "ExportContext",
                      *, scale: int = SVG_CHART_SCALE) -> list[bytes | None]:
    """把内联 SVG 图表栅格化成 PNG，返回与 `charts` **等长**的列表（失败位为 `None`）。

    做法：把所有 svg 竖着排进一个**布局完全确定**的页面（每个 svg 都给了像素宽高、
    行高为 0、无边距），**一次**浏览器截图，然后按"前面几张图的高度之和"把它们裁开。

    这不是"猜坐标"：每张图的高度就是它 `viewBox` 的比值换算出来的固定像素数，
    行与行之间没有任何会变化的间距（`line-height:0` 消掉行盒基线间隙）。
    """
    if not charts:
        return []
    sizes = [(max(120, int(round(c["w"] * scale))), max(80, int(round(c["h"] * scale))))
             for c in charts]
    total = sum(h for _, h in sizes)
    width = max(w for w, _ in sizes)
    if total > SVG_TOTAL_H_CAP:
        raise ExportError("报告含 %d 张矢量图表，一次栅格化高度 %d px 超过上限（%d px）"
                          % (len(charts), total, SVG_TOTAL_H_CAP))

    rows = "".join('<div class="__exprow">%s</div>' % _svg_with_size(c["svg"], w, h)
                   for c, (w, h) in zip(charts, sizes))
    styles = "\n".join(_CSS_BLOCK_RE.findall(html))
    # 报告自身的样式必须带上（svg 的配色/字号全在 `.svgchart .grid` 这类规则里）；
    # 紧跟其后的重置样式负责把布局钉死 —— 放在最后 + `!important` 才能盖住模板里的 body 边距。
    doc = (
        '<!doctype html><html><head><meta charset="utf-8"><style>%s</style>'
        "<style>html,body{margin:0!important;padding:0!important;background:#fff!important}"
        ".__exprow{margin:0!important;padding:0!important;border:0;line-height:0}"
        ".__exprow svg{display:block!important;margin:0!important}</style>"
        "</head><body>%s</body></html>" % (styles, rows))
    page = ctx.workdir / "svgcharts.html"
    page.write_text(doc, encoding="utf-8")
    shot = ctx.workdir / "svgcharts.png"

    exe = browser_path()
    if not exe:
        return [None] * len(charts)

    def make_args() -> list[str]:
        return _base_args(ctx.profile) + [
            "--window-size=%d,%d" % (width, total),
            "--virtual-time-budget=%d" % VIRTUAL_TIME_BUDGET_MS,
            "--screenshot=%s" % shot,
            _file_url(page),
        ]

    rc, log = _run_browser(exe, make_args, PNG_TIMEOUT_S, ctx)
    _check_output(shot, "矢量图表", rc, log)

    from PIL import Image
    out: list[bytes | None] = []
    with Image.open(shot) as im0:
        im = im0.convert("RGB")
        if im.width < width or im.height < total:
            # 视口没画满 → 裁出来的图会缺一截；明确报错而不是给半张图
            raise ExportError("矢量图表截图尺寸异常（%dx%d，期望至少 %dx%d）"
                              % (im.width, im.height, width, total))
        y = 0
        for i, (w, h) in enumerate(sizes):
            box = im.crop((0, y, w, y + h))
            buf = io.BytesIO()
            box.save(buf, format="PNG", optimize=True)
            blob = buf.getvalue()
            # 全空白（说明这张 svg 没画出来）→ 留 None，让 Word 侧走可见占位 + 告警
            out.append(blob if _has_ink(box) else None)
            y += h
    return out


def _has_ink(im) -> bool:
    """图里是否真的画了东西（与背景色不同的像素超过千分之一）。

    用途：拦住"svg 没画出来但截图成功"的情况 —— 那会往 Word 里塞一张纯白图，
    用户看到的是一个**空白框**，比缺图更难解释。
    """
    from collections import Counter
    w, h = im.size
    px = im.load()
    step = max(1, min(w, h) // 120)
    c: Counter = Counter()
    total = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y]
            c[(r // 8 * 8, g // 8 * 8, b // 8 * 8)] += 1
            total += 1
    if not total:
        return False
    bg, n = c.most_common(1)[0]
    return (1.0 - n / total) > 0.001




def export_docx(html_path: str | Path, out_path: str | Path, ctx: "ExportContext",
                *, title: str = "") -> dict:
    """HTML → DOCX（结构化 + 图表作为图片插入）。

    图表走 `capture_chart_images`（页面自己把图表搬出来再截图）；若某张**内联 SVG** 图表
    没能取到，再退回"直接栅格化那份 SVG 标记"—— 后者不依赖报告页面的 JS 是否跑通，
    是两条相互独立的路径。
    """
    html_path, out_path = Path(html_path).resolve(), Path(out_path).resolve()
    html = html_path.read_text(encoding="utf-8")

    kinds = docx_writer.chart_slot_kinds(html)
    slots: list = [None] * len(kinds)
    warnings: list[str] = []

    if kinds:
        imgs, warns = capture_chart_images(html_path, ctx, expected=len(kinds))
        warnings.extend(warns)
        for i, blob in enumerate(imgs):
            if i < len(slots) and blob:
                slots[i] = (blob, "png")
        if imgs and len(imgs) != len(kinds):
            warnings.append("报告里检出 %d 张图表图片，但静态 HTML 标出 %d 个图表位置，"
                            "个数不一致" % (len(imgs), len(kinds)))

        svg_missing = [i for i, k in enumerate(kinds) if k == "svg" and not slots[i]]
        if svg_missing:
            charts = extract_svg_charts(html)
            all_svg = [i for i, k in enumerate(kinds) if k == "svg"]
            if len(charts) == len(all_svg):
                pngs = render_svg_charts(html, charts, ctx)
                for j, i in enumerate(all_svg):
                    if not slots[i] and pngs[j]:
                        slots[i] = (pngs[j], "png")
            else:
                warnings.append("报告里检出 %d 张矢量图表，但图表位置有 %d 个，未能对应"
                                % (len(charts), len(all_svg)))
            still = [i for i in svg_missing if not slots[i]]
            if still:
                # 到这里说明"页面自己搬图"和"直接栅格化 SVG"两条独立路径都没成功。
                # 必须把**原因**说出来并给出替代路径 —— 只说"未取到图片"会让用户以为导出坏了。
                warnings.append("有 %d 张图表是内联 SVG（矢量图），未能写入 Word 版；"
                                "请改用 PDF 或长图导出，那两种格式里图表完整。" % len(still))

    res = docx_writer.html_to_docx(html, out_path, images=slots, title=title)
    res["warnings"] = warnings + list(res.get("warnings") or [])
    res["bytes"] = _check_output(out_path, " Word 文档")
    res["slot_kinds"] = kinds
    return res


# ===== 统一编排 =====


def export_report(
    report_dir: str | Path,
    fmt: str,
    out_path: str | Path,
    *,
    title: str = "",
) -> dict:
    """把一份报告目录导出为指定格式（**调用方负责先解析路径护栏**）。

    `report_dir` 必须来自 `report_index.resolve_report_dir()` —— 本模块
    **故意不自己拼路径**：路径规则一旦有两份实现，就会分化出"能打开却导不出"
    或更糟的"打不开反而导得出来"。测试里有一条源码级断言锁这件事。

    并发：先占槽位再建工作目录 —— 顺序反了会在"繁忙"时白白建一个目录，
    而且槽位编号正是 profile 的编号，两者必须一起确定。
    """
    key = (fmt or "").strip().lower()
    meta = FORMATS.get(key)
    if meta is None:
        raise ExportError("不支持的导出格式：%s（可选：%s）" % (fmt, " / ".join(FORMAT_ORDER)))

    report_dir = Path(report_dir)
    html = report_dir / "report.html"
    if not html.is_file():
        raise ExportError("报告文件不存在，无法导出")

    with _acquire_slot() as slot:
        ctx = ExportContext(_tmp_workspace(), profile_dir(slot.idx))
        try:
            if key == "pdf":
                res = export_pdf(html, out_path, ctx)
            elif key == "png":
                res = export_png(html, out_path, ctx)
            else:
                res = export_docx(html, out_path, ctx, title=title or report_dir.name)
        finally:
            # 只删本次产物；浏览器 profile 常驻复用（删它要二十秒，见模块常量处说明）
            shutil.rmtree(ctx.workdir, ignore_errors=True)

    res["format"] = key
    res["format_label"] = meta["label"]
    res["ext"] = meta["ext"]
    res["mime"] = meta["mime"]
    res["warnings"] = _warnings_for(key, res, html)
    return res


def _warnings_for(key: str, res: dict, html: Path) -> list[str]:
    """把"可能缺内容"的情形变成**可见告警**（而不是让用户拿一份静默残缺的文件）。

    ⚠️ 判定必须建立在**正确的**图表计数上：`docx_writer.count_js_charts` 只数
    `canvas` + 空 `div.chart`（= 需要 JS 图表库在运行时作画的那种）。若用`"chart" in class`
    这种子串判断，`card growth-chart` / `chart-hd` / `chart-empty` 都会被算成图表，
    于是**每次导出都报一条假告警** —— 假告警会让真告警失去意义。
    """
    out: list[str] = list(res.get("warnings") or [])
    if key in ("pdf", "docx"):
        charts = res.get("chart_images") if key == "pdf" else res.get("images_used")
        js_charts = _count_js_chart_slots(html)
        if js_charts > 0 and not charts:
            out.append("报告含 %d 处由 JavaScript 绘制的图表，但导出结果中未检出任何图表图片；"
                       "可能是图表库（CDN）未能加载。建议先在线打开报告确认图表能正常显示，"
                       "再重新导出。" % js_charts)
    return out


def _read_html_text(html: Path) -> str:
    try:
        return html.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _count_js_chart_slots(html: Path) -> int:
    """需要 JS 作画的图表数 —— 直接复用 `docx_writer` 的实现，**不在这里写第二份判定**。

    两份判定必然漂移：一处认 `svgchart`、另一处只认 `chart`，就会出现"导出侧说有图表、
    Word 侧说取到 0 张"这种自相矛盾的告警。
    """
    return docx_writer.count_js_charts(_read_html_text(html))


def safe_filename(name: str, fallback: str = "report") -> str:
    """把用户可见名称洗成安全的文件名主体（保留中文，去掉路径分隔与控制字符）。"""
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "", str(name or "")).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return s[:80] or fallback


def content_disposition(filename: str, ascii_name: str | None = None) -> str:
    """RFC 5987 的 `Content-Disposition: attachment`。

    中文文件名必须走 `filename*=UTF-8''…`，否则浏览器会拿到乱码名（甚至拒绝保存）。
    同时给出 ASCII 兜底 `filename=`，照顾**不看 `filename*` 的老客户端**（下载工具、
    部分老浏览器）—— 这些客户端会退化成用兜底名保存。

    ⚠️ 兜底名不能靠"把中文替换成下划线"临时凑：`贵州茅台_公司概况_20260926.pdf`
    会变成 `___________20260926.pdf`，主人名全丢，比没有名字还糟。
    故允许调用方传一个 ASCII 版本（用**代码**这类天然 ASCII 的信息拼），
    只有在它真的含足够多的字母数字时才采用。
    """
    fn = safe_filename(filename, "report")
    ascii_fn = safe_filename(ascii_name, "") if ascii_name else ""
    if len(re.sub(r"[^A-Za-z0-9]", "", ascii_fn)) < 3:
        ascii_fn = re.sub(r"[^A-Za-z0-9._-]", "_", fn)
    if not re.sub(r"[^A-Za-z0-9]", "", ascii_fn):
        ascii_fn = "report"
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_fn, quote(fn, safe=""))


def export_filename(name: str, type_label: str, date_seg: str, fmt: str) -> str:
    """导出文件名：`{名称 或 代码}_{报告类型}_{日期}.{扩展}`。

    与列表项的展示信息同源（名称 + 类型中文名 + 日期段）—— 用户下载后**不必打开就知道
    这是哪一份**，这正是"能发给别人"的一部分。

    ⚠️ 每段用 `safe_filename(p, "")`（**空兜底**）而不是默认兜底：默认兜底会把空段
    替换成 `report`，于是 `("力源信息", "", "")` 会拼出 `力源信息_report_report.png`
    —— 文件名开始说谎。只在整体为空时才回落 `report`。
    """
    ext = FORMATS.get(fmt, {}).get("ext", "bin")
    parts = [safe_filename(p, "") for p in (name, type_label, date_seg)]
    base = "_".join([p for p in parts if p])
    return "%s.%s" % (safe_filename(base, "report"), ext)


def export_filename_ascii(code: str, report_type: str, date_seg: str, fmt: str) -> str:
    """导出文件名的 **ASCII 兜底版**：`{代码}_{报告类型键}_{日期}.{扩展}`。

    类型直接用它自己的**英文键**（`overview` / `fundamental` / `industry` / `board`），
    而不是另维护一张"中文名 → 英文名"的对照表 —— 那种表就是第二份真相，
    新增报告类型时必然漏掉一项，而症状是"兜底名里少一段"，没人会发现。
    """
    ext = FORMATS.get(fmt, {}).get("ext", "bin")
    parts = [safe_filename(code, ""), safe_filename(report_type, ""), safe_filename(date_seg, "")]
    return "%s.%s" % (safe_filename("_".join([p for p in parts if p]), "report"), ext)


def warnings_header(warnings: Iterable[str]) -> str:
    """把告警编码成可放进 HTTP 头的 ASCII 串（头值只能是 latin-1）。

    用百分号编码承载中文，前端 `decodeURIComponent` 还原。之所以要走到头里，
    是因为响应体是**文件字节**，没有地方再放结构化信息了。
    """
    ws = [str(w) for w in warnings if str(w).strip()]
    if not ws:
        return ""
    return quote(" | ".join(ws), safe="")
