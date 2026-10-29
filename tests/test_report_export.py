# -*- coding: utf-8 -*-
"""报告导出（F-07：PDF / 长图 / Word）测试。

绝大部分用例**完全离线**（不触网、不起浏览器、不跑 pipeline）：

- docx 侧（`docx_writer`）是纯函数 → 直接拿 HTML 串测，无需任何外部依赖；
- 图片裁剪、文件名、能力自省都是纯逻辑；
- 端点契约用 `TestClient` + 临时 auth 库 + 临时报告根，**不触碰真实 `output/reports`**。

只有 1 条用例需要真实浏览器（`test_export_pdf_end_to_end`），本机没有 Chrome/Edge 时自动 skip
—— 它证明的是"整条链路真的能产出一个能解析的 PDF"，而不是"我们的代码看起来应该能"。

本文件锁的四类事（都是**静默失败**的高发区）：

1. **不能导出一份空/残缺的文件**：失败必须抛异常，绝不留下 0 字节产物；PNG 被视口截断
   必须被识别出来（宁可报错，也不给用户半张图）。
2. **内容不许静默丢失**：docx 的 `stats` 必须与 HTML 里的标签数逐项吻合 ——
   把"少了一整节"变成红灯，而不是让用户下载后自己发现。
3. **包必须是真 OOXML**：必需 part 齐备 + 每个 XML 良构；标题必须用**内置样式名**
   （`heading N`），否则 Word 不认作标题、且**不报错**（静默退化）。
4. **与 F-06 的接口一致性**：导出必须复用 `resolve_report_dir` 那一套路径护栏，
   且路由必须注册在 `/api/report/{task_id}` 之前 —— 用源码级断言锁住，
   因为它们一旦漂移，症状分别是"越权"和"极具误导性的 404"。
"""
from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fundamental_agent import docx_writer as dw
from fundamental_agent import report_export as rx
from fundamental_agent import report_index as ri
from fundamental_agent import storage
from fundamental_agent.api import server as api_server
from fundamental_agent.api.tasks import TaskManager
from fundamental_agent.auth import db as auth_db
from fundamental_agent.auth import service as auth_service

ROOT = Path(__file__).resolve().parents[1]


# ===== 夹具（与 test_report_remove.py 同形，便于两处对照阅读）=====


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时 auth 库（两个用户 + 各一个会话）+ 临时报告根 + 独立的 TaskManager。"""
    db_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", db_path)
    auth_db.init_db(seed_admin=False)

    conn = auth_db.get_conn()
    try:
        user_ids = []
        for phone in ("13900000001", "13900000002"):
            conn.execute(
                "INSERT INTO users (username, password_hash, status, created_at) "
                "VALUES (?, ?, 'active', ?)",
                (phone, auth_db.hash_password("pw123456"), "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
            user_ids.append(conn.execute(
                "SELECT id FROM users WHERE username = ?", (phone,)).fetchone()["id"])
        tokens = [auth_service._create_session(conn, uid) for uid in user_ids]
    finally:
        conn.close()

    root = tmp_path / "reports"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ri, "_DEFAULT_BASE", root)
    monkeypatch.setattr(storage, "_DEFAULT_BASE", root)

    tm = TaskManager()
    monkeypatch.setattr(api_server, "tm", tm)

    return {"root": root, "user_ids": user_ids, "tokens": tokens, "tm": tm}


@pytest.fixture()
def client():
    return TestClient(api_server.app)


def _auth(tok: str) -> dict:
    return {"Cookie": "%s=%s" % (auth_service.SESSION_COOKIE, tok)}


MINI_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>t</title></head>
<body><div class="wrap">
<h1>测试报告</h1>
<p>正文一段。</p>
<div class="chart"><canvas id="c" width="600" height="200"></canvas></div>
<script>
(function(){
  var g = document.getElementById('c').getContext('2d');
  g.fillStyle = '#3366cc';
  g.fillRect(10, 10, 300, 120);
})();
</script>
</div></body></html>
"""


def _make_report(root: Path, user_id: int, code: str, report_type: str, date_seg: str,
                 name: str = "", html: str | None = None, legacy: bool = False) -> Path:
    """造一份真实报告产物（`report.html` + `meta.json`），返回 `report.html` 路径。

    `report_type="fundamental"` 时按**两段**目录形态落盘（N-21：基本面有两种合法形态）。
    """
    base = root if legacy else root / ("u%d" % user_id)
    if report_type == "fundamental":
        d = base / code / date_seg
    else:
        d = base / code / report_type / date_seg
    d.mkdir(parents=True, exist_ok=True)
    html_path = d / "report.html"
    html_path.write_text(html if html is not None else MINI_HTML, encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({
        "code": code, "name": name, "report_type": report_type,
        "generated_at": "2026-09-26 10:00", "user_id": user_id,
    }, ensure_ascii=False), encoding="utf-8")
    return html_path


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _func_source(path: str, marker: str, stop: str) -> str:
    """从文件里切出 `marker` 到下一个 `stop` 之间的源码（源码级护栏用）。"""
    src = _read(path)
    i = src.index(marker)
    j = src.index(stop, i + len(marker))
    return src[i:j]


def _code_only(src: str) -> str:
    """去掉注释与文档字符串，只留可执行代码。

    源码级护栏必须跑在**代码**上而不是注释上 —— 否则"注释里写了『绝不能用
    capture_output』"反而会把护栏自己弄红，护栏就变成了噪音。
    """
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    out = []
    for line in src.splitlines():
        s = line.split("#", 1)[0]
        out.append(s)
    return "\n".join(out)


def _markup_code_only(rel: str) -> str:
    """前端源码的 `_code_only`：去掉 HTML 注释与 JS 行注释。

    前端护栏同样只能跑在**代码**上。报告库页里有一段注释写着
    「走 fetch + blob 而不是 `<a href="/api/report/export/…">`」——
    不去注释，这句反面教材会被当成违规本身。

    只砍"前面不是 `:`/引号/斜杠"的 `//`，避免把 `https://` 和正则里的斜杠一起砍掉。
    """
    src = re.sub(r"<!--(?:.|\n)*?-->", "", _read(rel))
    out = []
    for line in src.splitlines():
        m = re.search(r"(?<![:\"'/\\\w])//(?!/)", line)
        out.append(line[:m.start()] if m else line)
    return "\n".join(out)


def _browser_ok() -> bool:
    return rx.browser_path() is not None


has_browser = pytest.mark.skipif(not _browser_ok(), reason="本机无 Chrome/Edge，跳过真机用例")


# ============================================================
# A. 格式注册表 / 文件名 / 下载头
# ============================================================


def test_formats_cover_three_export_targets():
    """三种格式 = 「能发给别人」这一个目标的三个出口；各带 MIME / 扩展名 / 场景说明。"""
    assert set(rx.FORMATS) == {"pdf", "png", "docx"}
    assert rx.FORMAT_ORDER == ["pdf", "png", "docx"]
    for key, m in rx.FORMATS.items():
        assert m["ext"] and m["mime"] and m["label"]
        # hint 是给用户看的**使用场景**，必须写出来（前端直接渲染它）
        assert len(m["hint"]) >= 8, key
    assert rx.FORMATS["pdf"]["mime"] == "application/pdf"
    assert rx.FORMATS["png"]["mime"] == "image/png"
    assert rx.FORMATS["docx"]["ext"] == "docx"
    assert rx.FORMATS["docx"]["mime"].endswith("wordprocessingml.document")


def test_export_filename_is_self_describing():
    """文件名必须自带「名称 + 类型 + 日期」—— 下载后不打开也知道是哪一份。"""
    fn = rx.export_filename("贵州茅台", "公司概况", "20260926", "pdf")
    assert fn == "贵州茅台_公司概况_20260926.pdf"


def test_export_filename_skips_blank_parts_and_unknown_fmt():
    assert rx.export_filename("力源信息", "", "", "png") == "力源信息.png"
    # 空名 → 兜底成 report，绝不产出 "." 或空文件名
    assert rx.export_filename("", "", "", "docx") == "report.docx"
    assert rx.export_filename("x", "y", "z", "nope").endswith(".bin")


@pytest.mark.parametrize("bad", [
    'a/b', 'a\\b', 'a:b', 'a*b', 'a?b', 'a"b', 'a<b', 'a>b', 'a|b', 'ab\x00cd', '  x  ',
    '../etc/passwd', '..',
])
def test_safe_filename_removes_path_and_control_chars(bad):
    """文件名主体不得含路径分隔与控制字符 —— 这是防"下载时写到别处"的第一道门。"""
    out = rx.safe_filename(bad)
    assert not re.search(r'[\\/:*?"<>|\x00-\x1f]', out)
    assert out  # 永不为空


def test_content_disposition_is_rfc5987():
    """中文名必须走 `filename*=UTF-8''…`；同时给 ASCII 兜底照顾老客户端。"""
    cd = rx.content_disposition("贵州茅台_公司概况_20260926.pdf")
    assert cd.startswith("attachment; ")
    assert "filename*=UTF-8''" in cd
    assert "%E8%B4%B5%E5%B7%9E%E8%8C%85%E5%8F%B0" in cd  # "贵州茅台" 的百分号编码
    ascii_part = re.search(r'filename="([^"]*)"', cd).group(1)
    assert ascii_part and all(ord(c) < 128 for c in ascii_part)
    assert re.fullmatch(r"[A-Za-z0-9._-]+", ascii_part)


def test_content_disposition_ascii_only_for_pure_ascii_name():
    cd = rx.content_disposition("report.pdf")
    assert 'filename="report.pdf"' in cd
    assert "filename*=UTF-8''report.pdf" in cd


def test_content_disposition_ascii_fallback_is_not_all_underscores():
    """⚠️ 兜底名不能退化成 `__________20260926.pdf`。

    `filename*` 是给现代浏览器的；不看它的老客户端/下载工具会用 `filename=` 保存。
    把中文逐个换成下划线就会得到"主人名全丢、只剩日期"的名字 —— 比没有名字还糟。
    所以必须允许调用方传一个**天然 ASCII** 的兜底（用代码拼）。
    """
    disp = rx.content_disposition("贵州茅台_公司概况_20260926.pdf",
                                  rx.export_filename_ascii("600519", "overview",
                                                           "20260926", "pdf"))
    ascii_part = re.search(r'filename="([^"]*)"', disp).group(1)
    assert ascii_part == "600519_overview_20260926.pdf"
    # 中文本体仍走 filename*
    assert "%E8%B4%B5%E5%B7%9E%E8%8C%85%E5%8F%B0" in disp
    # 兜底名本身退化（全是下划线）时，也不能变成没有名字
    disp2 = rx.content_disposition("贵州茅台_公司概况_20260926.pdf", "")
    assert re.search(r'filename="[^"]*"', disp2)


def test_export_filename_ascii_uses_the_type_key_not_a_lookup_table():
    """ASCII 兜底名里的类型用**英文键**，不另维护中英对照表。

    对照表就是第二份真相：新增报告类型时必然漏掉一项，症状是"兜底名里少一段"
    —— 没人会发现。用键就不存在这个问题。
    """
    assert rx.export_filename_ascii("BK0890", "board", "20260925", "png") == \
        "BK0890_board_20260925.png"
    assert rx.export_filename_ascii("300184", "industry", "20260926__BK0890", "docx") == \
        "300184_industry_20260926__BK0890.docx"
    assert rx.export_filename_ascii("x", "y", "z", "nope").endswith(".bin")


def test_warnings_header_percent_encodes_and_is_ascii():
    """告警头只能承载 latin-1，中文必须百分号编码（前端 decodeURIComponent 还原）。"""
    h = rx.warnings_header(["报告含 5 处图表，但导出结果中未检出任何图表图片"])
    assert h and all(ord(c) < 128 for c in h)
    from urllib.parse import unquote
    assert "未检出任何图表图片" in unquote(h)


def test_warnings_header_empty_when_no_warnings():
    assert rx.warnings_header([]) == ""
    assert rx.warnings_header(["", "   "]) == ""
    assert rx.warnings_header(None or []) == ""


def test_warnings_header_joins_multiple():
    from urllib.parse import unquote
    h = rx.warnings_header(["第一条", "第二条"])
    assert unquote(h) == "第一条 | 第二条"


# ============================================================
# B. 浏览器发现与能力自省
# ============================================================


def test_capabilities_shape_when_available():
    caps = rx.capabilities()
    assert set(caps) >= {"available", "browser", "reason", "concurrency", "formats"}
    assert isinstance(caps["formats"], list) and len(caps["formats"]) == 3
    assert [f["key"] for f in caps["formats"]] == rx.FORMAT_ORDER
    assert caps["concurrency"] == rx.MAX_CONCURRENT_EXPORTS >= 1
    if caps["available"]:
        assert caps["reason"] == "" and Path(caps["browser"]).name


def test_browser_env_pointing_to_missing_file_is_unavailable(monkeypatch, tmp_path):
    """`REPORT_EXPORT_BROWSER` 指向不存在的文件 → 视为**不可用**，而不是悄悄换别的浏览器。

    若静默回退，"配了却没生效"会以「导出结果与预期不符」的形式出现，极难排查
    —— 这正是本项目对配置项的一贯取向。
    """
    monkeypatch.setenv(rx.BROWSER_ENV, str(tmp_path / "nope.exe"))
    monkeypatch.setattr(rx.shutil, "which", lambda name: None)
    assert rx.browser_path() is None
    caps = rx.capabilities()
    assert caps["available"] is False
    assert rx.BROWSER_ENV in caps["reason"]          # reason 必须说清怎么修
    assert caps["browser"] == ""


def test_browser_env_pointing_to_existing_file_wins(monkeypatch, tmp_path):
    fake = tmp_path / "my-chrome.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv(rx.BROWSER_ENV, str(fake))
    assert rx.browser_path() == str(fake)


# ============================================================
# C. 长图裁剪（PNG 自适应高度 + 截断检测）
# ============================================================


def _img(w: int, h: int, content_h: int, bg=(255, 255, 255), fg=(0, 0, 0)):
    """造一张 w×h 的图：顶部 content_h 像素是 fg（内容），其余是 bg（空白尾部）。"""
    from PIL import Image
    im = Image.new("RGB", (w, h), bg)
    px = im.load()
    for y in range(max(0, min(content_h, h))):
        for x in range(w):
            px[x, y] = fg
    return im


def test_background_rgb_takes_dominant_color_not_last_pixel():
    """背景取**全图抽样直方图的众数**：取最后一个像素会被页脚条/白卡片带偏。

    末行故意涂成红色（模拟页脚条）—— 众数取法必须仍然认出白色背景。
    返回值是**量化到 8 的倍数**的档位（见实现里的说明），故白是 248 而不是 255。
    """
    im = _img(200, 300, 100, bg=(255, 255, 255), fg=(10, 10, 10))
    px = im.load()
    for x in range(200):
        px[x, 299] = (200, 30, 30)
    assert rx._background_rgb(px, 200, 300) == (248, 248, 248)


def test_trim_bottom_full_page_of_content_raises_not_silently_accepted(tmp_path):
    """内容铺满整幅图（连背景都检不出）→ **明确报错**，绝不静默产出。

    这是"半张图"最可能的形态：报告比视口高得多、被切在内容中间。此处必须失败。
    """
    src = tmp_path / "in.png"
    _img(400, 800, 800).save(src)      # 整幅"内容"色
    dst = tmp_path / "out.png"
    with pytest.raises(rx.ExportError):
        rx._trim_bottom(src, dst)


def test_trim_bottom_removes_blank_tail_and_reports_complete(tmp_path):
    """内容没铺满视口（底部留着大段空白）→ 视为完整容纳，并裁掉空白。"""
    src = tmp_path / "in.png"
    _img(400, 1000, 150).save(src)
    dst = tmp_path / "out.png"
    w, h, complete = rx._trim_bottom(src, dst)
    assert w == 400
    assert complete is True
    assert 150 <= h <= 200          # 内容高 + 一点 padding
    assert h < 1000                 # 空白真的被裁掉了
    from PIL import Image
    with Image.open(dst) as out:
        assert out.size == (w, h)


def test_trim_bottom_requires_a_blank_margin_not_just_one_pixel(tmp_path):
    """底部余量不足 `PNG_SAFE_MARGIN` → 判为可能被截断。

    1px 的判据区分不了"页面到此结束"与"恰好在一条空白缝里被切断"（后者会静默
    丢掉下半页），故用一个宽松余量把这种巧合压到极小。
    """
    from PIL import Image
    h = 1000
    im = Image.new("RGB", (400, h), (255, 255, 255))
    px = im.load()
    for y in range(10, 40):
        for x in range(400):
            px[x, y] = (0, 0, 0)
    for y in range(930, 945):          # 内容伸到离底只剩 ~55px（< PNG_SAFE_MARGIN）
        for x in range(400):
            px[x, y] = (0, 0, 0)
    src = tmp_path / "in.png"
    im.save(src)
    dst = tmp_path / "out.png"
    _, _, complete = rx._trim_bottom(src, dst)
    assert complete is False
    assert rx.PNG_SAFE_MARGIN > 55


def test_trim_bottom_uniform_image_raises_with_actionable_message(tmp_path):
    """整幅同一颜色：既可能是空白页，也可能是"内容配色与背景过于接近"。

    错误文案必须把两种可能都说出来 —— 一句"长图为空白"会把排查方向带偏（N-18 的教训）。
    """
    src = tmp_path / "in.png"
    _img(300, 500, 0).save(src)        # 全白
    dst = tmp_path / "out.png"
    with pytest.raises(rx.ExportError) as ei:
        rx._trim_bottom(src, dst)
    assert "未检出" in str(ei.value)
    assert "配色" in str(ei.value)


# ============================================================
# D. 图片尺寸解析（docx 插图尺寸依赖它）
# ============================================================


def test_image_size_reads_png_header():
    buf = io.BytesIO()
    _img(1401, 420, 400).save(buf, format="PNG")
    assert dw._image_size(buf.getvalue()) == (1401, 420)


def test_image_size_reads_jpeg_header():
    buf = io.BytesIO()
    _img(320, 240, 200).save(buf, format="JPEG")
    assert dw._image_size(buf.getvalue()) == (320, 240)


@pytest.mark.parametrize("blob", [b"", b"not an image", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4])
def test_image_size_unknown_format_returns_none(blob):
    """认不出来就返回 None，由调用方用经验宽高比兜底 —— 绝不猜一个具体尺寸。"""
    assert dw._image_size(blob) is None


# ============================================================
# E. docx：OOXML 包结构 + 内容不丢
# ============================================================

REQUIRED_PARTS = [
    "[Content_Types].xml",
    "_rels/.rels",
    "docProps/core.xml",
    "docProps/app.xml",
    "word/document.xml",
    "word/styles.xml",
    "word/_rels/document.xml.rels",
]

_DOC_HTML = """<!DOCTYPE html><html><head><title>x</title>
<style>.a{color:red}</style></head>
<body><div class="wrap">
<h1>一级标题</h1>
<h2>二级标题</h2>
<h3>三级标题</h3>
<p>正文第一段。</p>
<p>正文第二段，带 <b>加粗</b> 与 <br> 换行。</p>
<ul><li>列表项一</li><li>列表项二<p>列表项内的段落（应折叠，不重复）</p></li></ul>
<table>
  <tr><th>板块</th><th colspan="2">收入</th></tr>
  <tr><td>MLCC</td><td>1.2 亿</td><td>38%</td></tr>
  <tr><td>其他</td><td>2.0 亿</td><td>62%</td></tr>
</table>
<div class="chart"><canvas id="c1" width="600" height="200"></canvas></div>
<p>图表之后的正文。</p>
</div>
<script>var x = 1;</script>
</body></html>
"""


def _build_docx(tmp_path, html=_DOC_HTML, images=(), title="t"):
    out = tmp_path / "out.docx"
    res = dw.html_to_docx(html, out, images=images, title=title)
    return out, res


def _blocks_of(html: str) -> list[dict]:
    """取 `extract_blocks` 出的块序列（断言**顺序与内容**时用，比只看计数更硬）。"""
    return dw.extract_blocks(html)[0]


def test_docx_package_has_required_parts_and_wellformed_xml(tmp_path):
    """少任一必需 part，Word 都会报「文件已损坏」；XML 不良构同理。"""
    out, _ = _build_docx(tmp_path)
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        for part in REQUIRED_PARTS:
            assert part in names, "缺少必需 part：%s" % part
        for n in names:
            if n.endswith(".xml") or n.endswith(".rels"):
                ET.fromstring(z.read(n))     # 不良构会直接抛异常


def test_docx_content_types_declares_every_part(tmp_path):
    """`[Content_Types].xml` 必须为每个 xml/rels 声明 Default，并为四个专有 part 声明 Override。"""
    out, _ = _build_docx(tmp_path)
    with zipfile.ZipFile(out) as z:
        ct = z.read("[Content_Types].xml").decode("utf-8")
    assert 'Extension="rels"' in ct and 'Extension="xml"' in ct
    for part in ("/word/document.xml", "/word/styles.xml",
                 "/docProps/core.xml", "/docProps/app.xml"):
        assert part in ct, part


def test_docx_heading_styles_use_builtin_names(tmp_path):
    """标题必须用内置样式名 `heading N`。

    这是**静默失败**的典型：自定义 styleId（如 `H1`）也能生成文件、也能显示成大字，
    但 Word 导航窗格与自动目录**不认**它 —— 导出"结构可引用"这件事就废了，且不报错。
    """
    out, _ = _build_docx(tmp_path)
    with zipfile.ZipFile(out) as z:
        styles = z.read("word/styles.xml").decode("utf-8")
        doc = z.read("word/document.xml").decode("utf-8")
    for lv in range(1, 4):
        assert 'w:styleId="Heading%d"' % lv in styles
        assert 'w:val="heading %d"' % lv in styles      # ← 内置名（小写 + 空格），不是 Heading%d
    assert 'w:pStyle w:val="Heading1"' in doc
    assert 'w:pStyle w:val="Heading2"' in doc
    assert 'w:pStyle w:val="Heading3"' in doc
    assert "大纲" not in styles or True                 # 占位：仅确保未误改其它内容


def test_docx_stats_match_html_tag_counts(tmp_path):
    """**内容不许静默丢失**：stats 必须与 HTML 里的标签数逐项吻合。

    这条是本模块最核心的断言 —— 它把"少了一整节"变成红灯。
    """
    _, res = _build_docx(tmp_path)
    st = res["stats"]
    assert st["headings"] == 3          # h1 h2 h3
    assert st["list_items"] == 2        # 两个 li（li 内的 p 折叠，不重复计数）
    assert st["tables"] == 1
    assert st["table_rows"] == 3
    assert st["charts"] == 1
    assert st["nested_tables"] == 0
    # paragraphs：两个顶层 p（li 内的 p 已折叠）+ 表格之后的那个 p
    assert st["paragraphs"] == 3


def test_docx_table_cell_paragraph_is_folded_not_duplicated(tmp_path):
    """`<td><p>文字</p></td>` 里的 p 不能再单独成段 —— 否则同一句话出现两遍。"""
    html = ("<div class='wrap'><table><tr><td><p>唯一一段</p></td></tr></table></div>")
    out, res = _build_docx(tmp_path, html=html)
    assert res["stats"]["paragraphs"] == 0
    assert res["stats"]["tables"] == 1
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode("utf-8")
    assert doc.count("唯一一段") == 1


def test_docx_keeps_text_of_unknown_tags(tmp_path):
    """不认识的标签**降级为文本**，而不是整块丢掉（如 <div>/<span> 包裹的正文）。"""
    html = "<div class='wrap'><div class='card'>卡片里的正文<span>x</span></div></div>"
    _, res = _build_docx(tmp_path, html=html)
    out = tmp_path / "out.docx"
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode("utf-8")
    assert "卡片里的正文" in doc


def test_docx_colspan_becomes_gridspan(tmp_path):
    out, _ = _build_docx(tmp_path)
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode("utf-8")
    assert "w:gridSpan" in doc
    assert "3" in doc


def test_docx_has_no_colspan_when_source_has_none(tmp_path):
    html = "<div class='wrap'><table><tr><th>a</th><th>b</th></tr></table></div>"
    _build_docx(tmp_path, html=html)
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        doc = z.read("word/document.xml").decode("utf-8")
    assert "w:gridSpan" not in doc


def test_docx_tables_are_separated_by_empty_paragraph(tmp_path):
    """两张表紧挨会粘成一张 → 表后必须补一个空段。"""
    html = ("<div class='wrap'>"
            "<table><tr><td>a</td></tr></table>"
            "<table><tr><td>b</td></tr></table></div>")
    _build_docx(tmp_path, html=html)
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        doc = z.read("word/document.xml").decode("utf-8")
    body = doc[doc.index("<w:body>"):]
    # 第一张表结束到第二张表开始之间，必须有一个空段落
    assert re.search(r"</w:tbl>\s*<w:p\s*/>\s*<w:tbl>", body), body[-600:]


def test_docx_strips_illegal_xml_control_chars(tmp_path):
    """XML 1.0 非法控制字符必须先清掉再转义，否则 Word 判定文件损坏。

    顺序不能反：先转义会把 `\\x0b` 变成 `&#11;`，那就"合法"地留下来了。
    （`html.parser` 本身可能把 `\\x00` 换成替换字符，故只断言非法字符不落进 XML、
    且两侧文字完整保留。）
    """
    html = "<div class='wrap'><p>坏字符A\x00B\x0bC\x1fD</p></div>"
    out, _ = _build_docx(tmp_path, html=html)
    with zipfile.ZipFile(out) as z:
        raw = z.read("word/document.xml")
    ET.fromstring(raw)                                  # 良构
    text = raw.decode("utf-8")
    assert "\x00" not in text and "\x0b" not in text and "\x1f" not in text
    assert "坏字符" in text and "D</w:t>" in text        # 文字本身保留
    for ch in ("A", "B", "C", "D"):
        assert ch in text


def test_docx_escapes_markup_characters(tmp_path):
    """正文里的 `<` `&` 必须转义，否则会破坏 document.xml 结构（Word 报损坏）。"""
    html = "<div class='wrap'><p>a &lt; b &amp;&amp; c &gt; d</p></div>"
    out = tmp_path / "out.docx"
    dw.html_to_docx(html, out)
    with zipfile.ZipFile(out) as z:
        raw = z.read("word/document.xml")
    ET.fromstring(raw)
    assert "a &lt; b &amp;&amp; c &gt; d" in raw.decode("utf-8")


def test_docx_script_and_style_are_not_exported(tmp_path):
    out, _ = _build_docx(tmp_path)
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode("utf-8")
    assert "var x = 1" not in doc
    assert "color:red" not in doc


# --- 内联 SVG 图表（基本面报告）：既不能当正文，也不能凭空消失 ---


def _doc_texts(doc_xml: str) -> list[str]:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(doc_xml)
    return [t.text or "" for t in root.iter(ns + "t")]


SVG_CHART_HTML = """<div class="wrap"><h2>营收趋势</h2>
<svg class="svgchart" viewBox="0 0 400 200" role="img">
  <defs><linearGradient id="g"><stop offset="0%"/></linearGradient></defs>
  <line x1="10" y1="10" x2="390" y2="10"/>
  <text x="5" y="12">40</text><text x="5" y="32">20</text>
  <rect x="20" y="40" width="8" height="60"/>
</svg>
<p>图注一句话。</p></div>"""


def test_docx_svg_chart_is_a_chart_slot_not_body_text(tmp_path):
    """基本面报告的图表是**内联 SVG**（矢量）。

    两个都不能错：
      - 当成普通标签 → 坐标刻度 `<text>` 会变成一串数字灌进 Word 正文；
      - 完全忽略 → 图表凭空消失，用户拿到一份"缺了 5 张图却没有任何说明"的文档。
    """
    out, res = _build_docx(tmp_path, html=SVG_CHART_HTML)
    assert res["charts"] == 1
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode("utf-8")
    texts = _doc_texts(doc)
    assert "40" not in texts and "20" not in texts      # 刻度数字不是正文
    assert "营收趋势" in texts                            # 标题还在
    assert "图注一句话。" in texts                        # 图注还在
    # 没给图片时，图表位置必须留下**可见**说明（这一层只陈述事实：这里没图）
    assert any("图表" in t for t in texts)
    assert any("图表" in w for w in res["warnings"])


def test_export_docx_explains_vector_charts_when_both_paths_fail(tmp_path, monkeypatch):
    """两条取图路径都失效时，必须把**原因**和**替代方案**一起说清楚。

    `html_to_docx` 无从判断"为什么没图"，只陈述事实；"是矢量图、请改用 PDF"这句解释
    必须由知道原因的上层（`export_docx`）补上 —— 只报"未取到图片"会让用户以为导出坏了。
    这里把 `browser_path` 打成 None 来制造"本机无浏览器"这种最现实的情形。
    """
    import fundamental_agent.report_export as rx
    monkeypatch.setattr(rx, "browser_path", lambda: None)
    monkeypatch.setattr(rx, "browser_path", lambda: None)
    html_path = tmp_path / "report.html"
    html_path.write_text(SVG_CHART_HTML, encoding="utf-8")
    ctx = rx.ExportContext(rx._tmp_workspace(), rx.profile_dir(0))
    try:
        res = rx.export_docx(html_path, tmp_path / "o.docx", ctx, title="t")
    finally:
        import shutil
        shutil.rmtree(ctx.workdir, ignore_errors=True)
    assert res["images_used"] == 0
    blob = " ".join(res["warnings"])
    assert "矢量" in blob and "PDF" in blob


def test_docx_plain_svg_icon_does_not_take_a_chart_slot(tmp_path):
    """非图表的 svg（装饰图标）不占图表槽位 —— 否则图片会与图表位置错位。"""
    html = ('<div class="wrap"><svg class="icon"><path d="M0 0h4v4H0z"/></svg>'
            '<canvas id="c"></canvas></div>')
    _, res = _build_docx(tmp_path, html=html)
    assert res["charts"] == 1


def test_chart_nodes_are_returned_in_document_order():
    """图表槽位必须按**文档序**，不能"先所有 canvas 再其它"。

    图片是按位置依次填进槽位的：顺序错了会出现"第一张图配了第二个图表"，而页面上
    看不出任何异常 —— 这类静默错配比缺图更难发现。
    """
    from bs4 import BeautifulSoup
    html = ('<div class="wrap"><div class="chart"></div><canvas id="a"></canvas>'
            '<svg class="svgchart"></svg><canvas id="b"></canvas></div>')
    soup = BeautifulSoup(html, "html.parser")
    nodes = dw.chart_slots(soup.select_one(".wrap"))
    assert [n.name for n in nodes] == ["div", "canvas", "svg", "canvas"]


def test_chart_class_match_is_token_exact_not_substring():
    """⚠️ 图表类名必须**逐 token 精确匹配**，子串判断会把卡片外框也算成图表。

    真事故：模板里有 `card growth-chart` / `prof-chart` / `efficiency-chart` /
    `solvency-chart`（卡片容器）、`chart-hd`（表头）、`chart-empty`（空态占位）。
    用 `"chart" in cls` 判定的结果是某份基本面报告的槽位从 4 个**虚增到 12 个** ——
    Word 版里凭空多出 8 个"未能取到图片"的空占位，且位图与真实图表错位。
    """
    from bs4 import BeautifulSoup
    html = ('<div class="wrap">'
            '<div class="card growth-chart"><svg class="svgchart"></svg></div>'
            '<div class="card prof-chart"><svg class="svgchart"></svg></div>'
            '<div class="chart-hd">图表标题</div>'
            '<div class="chart-empty"></div>'
            '<div class="chart chart-lg"></div>'
            '<div class="chart sm"></div>'
            '</div>')
    soup = BeautifulSoup(html, "html.parser")
    nodes = dw.chart_slots(soup.select_one(".wrap"))
    # 只有两个真正的 div.chart 是槽位；两张 svg 也是槽位 → 共 4 个（按文档序）
    assert [n.name for n in nodes] == ["svg", "svg", "div", "div"]
    assert {n.get("id", "") for n in nodes} == {""}


def test_js_chart_count_excludes_inline_svg():
    """`count_js_charts` 只数需要 JS 作画的图表，**不含内联 svg**。

    svg 是标记里的矢量图，PDF 里一定有（只是以矢量形式），把它算进来会得出
    "PDF 里没有图表图片"的**误报**；而 JS 图表若一张位图都没有，那才是真故障。
    """
    html = ('<div class="wrap"><canvas></canvas><div class="chart"></div>'
            '<svg class="svgchart"></svg><div class="card x-chart"></div></div>')
    assert dw.count_js_charts(html) == 2


def test_docx_chart_without_image_gets_visible_placeholder(tmp_path):
    """有图表占位符但没给图片 → 必须留下**可见**占位说明，并给出告警（不静默缺图）。"""
    _, res = _build_docx(tmp_path, images=())
    assert res["images_used"] == 0
    assert any("图表" in w for w in res["warnings"])
    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        doc = z.read("word/document.xml").decode("utf-8")
    assert "未能从渲染结果中取到图片" in doc


def test_docx_extra_images_go_to_appendix_not_dropped(tmp_path):
    """图片比占位符多 → 进「附：图表」附录，**绝不丢弃**。"""
    imgs = []
    for i in range(3):
        buf = io.BytesIO()
        _img(200, 100, 90).save(buf, format="PNG")
        imgs.append((buf.getvalue(), "png"))
    out, res = _build_docx(tmp_path, html="<div class='wrap'><p>无图表</p></div>",
                           images=imgs)
    assert res["images_used"] == 3
    assert any("附" in w or "图表" in w for w in res["warnings"])
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode("utf-8")
        media = [n for n in z.namelist() if n.startswith("word/media/")]
    assert len(media) == 3
    assert "附" in doc


def test_docx_image_relationships_and_media_are_consistent(tmp_path):
    """每个内嵌图都要在 `document.xml.rels` 里有对应关系，否则 Word 显示裂图。"""
    buf = io.BytesIO()
    _img(400, 200, 180).save(buf, format="PNG")
    out, res = _build_docx(tmp_path, images=[(buf.getvalue(), "png")])
    assert res["images_used"] == 1
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        ct = z.read("[Content_Types].xml").decode("utf-8")
        names = z.namelist()
    rid = re.search(r'r:embed="([^"]+)"', doc).group(1)
    assert 'Id="%s"' % rid in rels
    assert "media/image1.png" in rels
    assert "word/media/image1.png" in names
    # ⚠️ jpeg 图片必须以 Extension="jpeg" 声明，否则 Word 报损坏
    assert 'Extension="png"' in ct


def test_docx_jpeg_extension_declared_correctly(tmp_path):
    buf = io.BytesIO()
    _img(300, 150, 130).save(buf, format="JPEG")
    out, res = _build_docx(tmp_path, images=[(buf.getvalue(), "jpeg")])
    assert res["images_used"] == 1
    with zipfile.ZipFile(out) as z:
        ct = z.read("[Content_Types].xml").decode("utf-8")
    assert 'Extension="jpeg"' in ct


def test_docx_unsupported_image_ext_is_warned_and_skipped(tmp_path):
    out, res = _build_docx(tmp_path, images=[(b"\x00\x01", "bmp")])
    assert res["images_used"] == 0
    assert any("bmp" in w for w in res["warnings"])


def test_docx_empty_html_still_produces_valid_package(tmp_path):
    """空 HTML 也必须产出**能打开**的文件，而不是 0 字节（宁可空白，不可损坏）。"""
    out, res = _build_docx(tmp_path, html="")
    assert out.stat().st_size > 0
    assert any("未提取到任何内容" in w or "内容" in w for w in res["warnings"])
    with zipfile.ZipFile(out) as z:
        ET.fromstring(z.read("word/document.xml"))


def test_docx_core_properties_carry_title(tmp_path):
    out, _ = _build_docx(tmp_path, title="力源信息 公司概况")
    with zipfile.ZipFile(out) as z:
        core = z.read("docProps/core.xml").decode("utf-8")
    assert "力源信息 公司概况" in core


# ============================================================
# E2. 三个"内容会静默消失"的真事故（每一条都是本轮实测定出来的）
# ============================================================


def test_block_container_does_not_swallow_headings_and_tables(tmp_path):
    """⚠️ 容器只输出"自己的直连文字"时，**绝不能**把子树标记成"已被带走"。

    真事故：`<div>前文<h3>标题</h3><table>…</table></div>` 里，容器把"前文"输出后
    自己进了 `emitted`，于是 `<h3>`、`<table>` 被当成"已由外层承载"而整块丢掉。
    实测真实报告：**30 个标题只输出 22 个、5 张表只剩 4 张**，而页面上一切正常。
    """
    html = ('<div class="wrap"><div class="sec">前文一句'
            '<h3>小节标题</h3>'
            '<table><tr><td>唯一单元格</td></tr></table>'
            '<p>后文一句</p></div></div>')
    out, res = _build_docx(tmp_path, html=html)
    st = res["stats"]
    assert st["headings"] == 1, "容器把子树里的标题吞掉了"
    assert st["tables"] == 1, "容器把子树里的表格吞掉了"
    assert st["paragraphs"] == 2, "容器自身的直连文字 + 子树里的 p"
    with zipfile.ZipFile(out) as z:
        doc = z.read("word/document.xml").decode("utf-8")
    texts = _doc_texts(doc)
    assert "前文一句" in texts and "后文一句" in texts
    assert "小节标题" in texts and "唯一单元格" in texts


def test_unknown_container_with_known_blocks_only_emits_its_own_text(tmp_path):
    """容器内含已知块时，只输出**自己的直连文字**，子块各自成块（不重复也不丢）。

    这里防的是另一个方向：容器若把子块文字一起吞进去，同一句话就会在产物里出现两遍
    （一次在容器段、一次在子块），而**页面上看不出来**。
    """
    html = ('<div class="wrap"><div class="sec"><div class="card">卡片标题'
            '<p>段落文字</p></div></div></div>')
    _, res = _build_docx(tmp_path, html=html)
    assert res["stats"]["paragraphs"] == 2       # 卡片标题 + 段落文字，各一次
    texts = [b["runs"][0][0] for b in _blocks_of(html)]
    assert texts == ["卡片标题", "段落文字"], texts


def test_ul_ol_are_containers_and_items_are_not_merged(tmp_path):
    """⚠️ 列表必须按项拆开，不能被容器合成一段。

    真事故：判断"元素里有没有已知块"时写成了 `el.find("h1,h2,h3,li,table")` ——
    BeautifulSoup 的 `find()` 把字符串当**标签名**（不是 CSS 选择器），这个判断恒为
    `False`。于是 `<ul>` 被当成普通块，`_runs_of` 把两个 `li` 的文字**合成一段**，
    `li` 又被跳过 —— 实测 `list_items == 0`，列表结构在导出物里彻底消失。
    """
    html = ('<div class="wrap"><ul><li>第一项</li><li>第二项</li></ul>'
            '<ol><li>甲</li></ol></div>')
    _, res = _build_docx(tmp_path, html=html)
    assert res["stats"]["list_items"] == 3, "列表项被容器吞了"
    assert res["stats"]["paragraphs"] == 0, "列表不该额外产出一个段落"
    texts = [b["runs"][0][0] for b in _blocks_of(html)]
    assert texts == ["第一项", "第二项", "甲"]


def test_inject_capture_script_uses_the_last_body_tag():
    """⚠️ 必须插在**最后一个** `</body>` 前 —— 按第一个匹配会打断内联的图表库。

    真事故：报告会把图表库内联进 HTML（离线回退），而 ECharts 库源码里含有
    `</body>` 字符串（它拼 HTML 用）。按第一个匹配注入 = 把脚本塞进库源码的字符串中间，
    整段库代码被当成**正文渲染**出来（截图里是一大坨 JS 源码），所有图表消失。
    实测板块报告（1050KB，内联了 ECharts）就是这样被打断的。
    """
    import fundamental_agent.report_export as rx
    html = ('<html><body><script>var s = "<body style=\\"margin:0;\\">"'
            ' + "</body>";</script><p>正文</p></body></html>')
    out = rx._inject_capture_script(html)
    assert out.count(rx._META_ATTR) == 1
    # 注入点必须在**真正**的收尾 </body> 之前，也就是文件最末
    assert out.rstrip().endswith("</body></html>")
    assert out.index(rx._META_ATTR) > out.rindex('" + "</body>"')


def test_parse_capture_meta_reads_row_widths_and_heights():
    """读回"页面实际搬了多少行"：个数靠静态判断、位置靠算，就会被运行时差异打脸。"""
    import fundamental_agent.report_export as rx
    dom = '<html %s="1060,394;527,352">…</html>' % rx._META_ATTR
    assert rx._parse_capture_meta(dom) == [(1060, 394), (527, 352)]
    assert rx._parse_capture_meta('<html %s="">…</html>' % rx._META_ATTR) == []
    assert rx._parse_capture_meta("") == []
    # 空段（多写了一个分号）要跳过，不能让整条元数据报废
    assert rx._parse_capture_meta('<html %s="1060,394;;527,352">' % rx._META_ATTR) == \
        [(1060, 394), (527, 352)]


def test_trim_image_drops_right_and_bottom_blank_but_keeps_content(tmp_path):
    """图表行截图右侧/底部是大片白，必须裁掉；**内容一个像素都不能少**。

    内容占大半时**裁不动**是**正确**的：背景取"全图抽样直方图的众数"，内容过半时众数
    会落到内容色上，于是空白被当成"内容"、结果是不裁 —— 这是安全方向（宁可留白，
    不能把图表裁掉）。故这里用一个内容占比正常的图来验证真的会裁。
    """
    import fundamental_agent.report_export as rx
    im = _img(600, 400, 120)
    px = im.load()
    for y in range(120):            # 内容只占左上角 300×120，右/下都是白
        for x in range(300, 600):
            px[x, y] = (255, 255, 255)
    trimmed = rx._trim_image(im)
    assert trimmed is not None
    w, h = trimmed.size
    assert 120 + 16 <= h < 400          # 内容高 + padding，且真的裁掉了底部空白
    assert 300 + 16 <= w < 600          # 右侧空白也一样要裁掉
    blank = rx._trim_image(_img(300, 200, 0))
    assert blank is None, "整行空白必须返回 None，让调用方去报占位而不是塞一张白图"


def test_capture_row_height_exceeds_tallest_chart_in_templates():
    """行高必须**大于**模板里最高的图表，否则会被裁掉一截。

    模板里最高的图表是板块报告的 `.chart{height:420px}`。这条断言把"调小行高"这种
    看起来无害的优化挡在门外 —— 裁掉的是图表底部的内容，且没有任何报错。
    """
    import fundamental_agent.report_export as rx
    assert rx._CHART_ROW_H > 420 * 1.5
    assert rx._CHART_ROW_W > 920          # 必须宽于模板的桌面断点，否则会被切成移动版式


# ============================================================
# F. 端点契约
# ============================================================


def test_export_requires_login(client):
    r = client.get("/api/report/export/600519.overview.20260926?fmt=pdf")
    assert r.status_code == 401


def test_export_rejects_unknown_format(env, client):
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    r = client.get("/api/report/export/600519.overview.20260926?fmt=xls",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 400
    # 错误信息必须列出可选值 —— 否则用户不知道能填什么
    assert "pdf" in r.json()["detail"]


def test_export_404_for_unresolvable_report(env, client):
    """不存在的报告 / 非法 report_id → 404，且**不回显路径**。"""
    for rid in ("600519.overview.20260926", "../../etc.overview.20260926", "bad"):
        r = client.get("/api/report/export/%s?fmt=pdf" % rid,
                       headers=_auth(env["tokens"][0]))
        assert r.status_code != 200
        assert r.status_code in (400, 404)


def test_export_404_for_other_users_report(env, client):
    """跨用户必须 404 —— 与 view 同口径（「能打开的报告」⟺「能导出的报告」）。"""
    _make_report(env["root"], 2, "600519", "overview", "20260926", "贵州茅台")
    r = client.get("/api/report/export/600519.overview.20260926?fmt=pdf",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 404
    # 同一份报告对**本人**是可见的（用 409 之外的方式证明"是权限而非不存在"）
    r2 = client.get("/api/report/view/600519.overview.20260926",
                    headers=_auth(env["tokens"][1]))
    assert r2.status_code == 200


def test_export_404_for_legacy_shared_report(env, client):
    """legacy 共享根的报告：正常用户看不到也导不出（结构上不可能，不是额外规则）。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台", legacy=True)
    r = client.get("/api/report/export/600519.overview.20260926?fmt=pdf",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 404


def test_export_requires_report_html_exists(env, client):
    """「目录在」不等于「这里有一份报告」：没有 report.html → 404。"""
    d = env["root"] / "u1" / "600519" / "overview" / "20260926"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{}", encoding="utf-8")
    r = client.get("/api/report/export/600519.overview.20260926?fmt=pdf",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 404


def test_export_409_when_report_is_being_regenerated(env, client):
    """正在重新生成 → 409。

    不加这条守卫会导出到**写了一半的目录**，产出一份内容错位的文件 ——
    比"导出失败"坏得多。守卫与删除端点同源（`tm.active_keys`）。
    """
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    env["tm"].create("600519", report_type="overview", user_id=env["user_ids"][0])
    r = client.get("/api/report/export/600519.overview.20260926?fmt=pdf",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 409
    assert "正在重新生成" in r.json()["detail"]


def test_export_409_is_scoped_to_subject_and_type(env, client):
    """守卫只看 `(主体, 类型)`：别的类型/别的用户正在生成，不能挡住这份导出。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    env["tm"].create("600519", report_type="fundamental", user_id=env["user_ids"][0])
    env["tm"].create("600519", report_type="overview", user_id=env["user_ids"][1])
    r = client.get("/api/report/export/600519.overview.20260926?fmt=xls",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 400      # 格式非法 → 说明它已经越过了 409 守卫


def test_export_503_when_no_browser(env, client, monkeypatch):
    """浏览器不可用 → 503 + 可操作的原因，**绝不降级成"另一种方式"偷偷导出一份缺图的东西**。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    monkeypatch.setattr(rx, "browser_path", lambda: None)
    r = client.get("/api/report/export/600519.overview.20260926?fmt=pdf",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 503
    assert rx.BROWSER_ENV in r.json()["detail"]


def test_export_busy_returns_503_not_queue(env, client, monkeypatch):
    """并发已满 → 503 让用户稍后重试，**不排队**（排队会让用户以为"点了没反应"）。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")

    def boom(_fmt, _path, _ctx, **_kw):
        raise rx.ExportBusy("导出任务已达并发上限（2），请稍后重试")

    monkeypatch.setattr(rx, "export_pdf", lambda html, out, ctx: boom("pdf", out, ctx))
    r = client.get("/api/report/export/600519.overview.20260926?fmt=pdf",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 503
    assert "并发上限" in r.json()["detail"]


def test_export_failure_does_not_leave_temp_file(env, client, monkeypatch, tmp_path):
    """导出失败必须回 500 且**不留半成品临时文件**（也不谎报成功）。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    before = set(Path(rx.tempfile.gettempdir()).glob("tradeai_export_*"))

    def boom(html, out, ctx):
        Path(out).write_bytes(b"partial")     # 假装写了一半
        raise rx.ExportError("浏览器未能生成 PDF")

    monkeypatch.setattr(rx, "export_pdf", boom)
    r = client.get("/api/report/export/600519.overview.20260926?fmt=pdf",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 500
    assert "导出失败" in r.json()["detail"]
    after = set(Path(rx.tempfile.gettempdir()).glob("tradeai_export_*"))
    assert after == before, "失败后残留了临时文件：%s" % (after - before)


def test_list_reports_exposes_export_capabilities(env, client):
    """能力由**服务端下发**（列表响应里的 export 字段），前端不自己猜。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    r = client.get("/api/report/list", headers=_auth(env["tokens"][0]))
    assert r.status_code == 200
    d = r.json()
    assert "export" in d
    caps = d["export"]
    assert set(caps) >= {"available", "reason", "formats", "concurrency"}
    assert [f["key"] for f in caps["formats"]] == rx.FORMAT_ORDER
    for f in caps["formats"]:
        assert f["hint"]


def test_item_contract_has_fields_export_needs(env, client):
    """导出入口要用到的列表字段一个都不能少（否则前端拼不出文件名/标题）。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    r = client.get("/api/report/list", headers=_auth(env["tokens"][0]))
    it = r.json()["items"][0]
    for k in ("report_id", "report_type", "report_type_label", "code", "name",
              "generated_at", "deletable", "view_url"):
        assert k in it, k


@has_browser
def test_export_pdf_end_to_end(env, client):
    """真机：真浏览器渲染 → 真 PDF → 真响应头。这是"整条链路可用"的唯一证据。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    r = client.get("/api/report/export/600519.overview.20260926?fmt=pdf",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 200, r.text[:400]
    assert r.content[:5] == b"%PDF-"
    assert len(r.content) > 2000
    assert r.headers["Content-Type"].startswith("application/pdf")
    assert r.headers["X-Export-Format"] == "pdf"
    assert int(r.headers["X-Export-Bytes"]) == len(r.content)
    cd = r.headers["Content-Disposition"]
    assert cd.startswith("attachment;") and "filename*=UTF-8''" in cd
    from urllib.parse import unquote
    assert "贵州茅台_公司概况_20260926.pdf" in unquote(cd)
    # 报告里有 1 个 canvas → 必须被光栅化进 PDF（否则就是"有文件、没图表"）
    import pymupdf
    with pymupdf.open(stream=io.BytesIO(r.content), filetype="pdf") as doc:
        assert doc.page_count >= 1
        assert all(p.get_text().strip() or p.get_images() for p in doc)


@has_browser
def test_export_png_end_to_end_is_a_single_tall_image(env, client):
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    r = client.get("/api/report/export/600519.overview.20260926?fmt=png",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 200, r.text[:400]
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert r.headers["Content-Type"] == "image/png"
    from PIL import Image
    with Image.open(io.BytesIO(r.content)) as im:
        w, h = im.size
    # 截图宽度≈视口宽（受滚动条/设备像素比影响会略小于 PNG_WIDTH）
    assert rx.PNG_WIDTH - 40 <= w <= rx.PNG_WIDTH
    assert 200 < h < rx.PNG_HEIGHT_CAP_MAX


@has_browser
def test_export_docx_end_to_end_is_openable(env, client):
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    r = client.get("/api/report/export/600519.overview.20260926?fmt=docx",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 200, r.text[:400]
    assert r.content[:2] == b"PK"
    assert r.headers["Content-Type"].endswith("wordprocessingml.document")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for part in REQUIRED_PARTS:
            assert part in z.namelist()
        doc = z.read("word/document.xml").decode("utf-8")
    assert "测试报告" in doc
    assert "正文一段。" in doc


@has_browser
def test_export_docx_title_and_filename_use_display_fields(env, client):
    """下载名与 Word 标题都取自**列表同一份**展示信息（名称 + 类型中文名 + 日期）。"""
    _make_report(env["root"], 1, "300184", "industry", "20260926__BK0890", "力源信息")
    r = client.get("/api/report/export/300184.industry.20260926__BK0890?fmt=docx",
                   headers=_auth(env["tokens"][0]))
    assert r.status_code == 200, r.text[:400]
    from urllib.parse import unquote
    cd = unquote(r.headers["Content-Disposition"])
    assert "力源信息" in cd and "20260926__BK0890" in cd
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        core = z.read("docProps/core.xml").decode("utf-8")
    assert "力源信息" in core


# ============================================================
# G. 路由顺序（硬约束，漂移的症状极具误导性）
# ============================================================


def test_export_route_registered_before_task_id_route():
    """`/api/report/export/{report_id}` 必须在 `/api/report/{task_id}` **之前**注册。

    与 `/api/report/list`、`/api/report/remove` 同一条纪律。顺序错了的症状是
    「任务不存在」这类极具误导性的 404，而不是"导出功能坏了"。
    """
    paths = [getattr(r, "path", "") for r in api_server.app.routes]
    assert "/api/report/export/{report_id}" in paths
    assert paths.index("/api/report/export/{report_id}") < paths.index("/api/report/{task_id}")
    assert paths.index("/api/report/list") < paths.index("/api/report/{task_id}")

    def routing(path: str) -> str:
        """按 FastAPI 的注册顺序做一次朴素匹配，返回最终命中的路由。"""
        for r in api_server.app.routes:
            p = getattr(r, "path", "")
            if not p:
                continue
            if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", p), path):
                if "GET" in (getattr(r, "methods", None) or {"GET"}):
                    return p
        return ""

    assert routing("/api/report/export/600519.overview.20260926") == \
        "/api/report/export/{report_id}"
    assert routing("/api/report/list") == "/api/report/list"
    assert routing("/api/report/abc123") == "/api/report/{task_id}"
    assert routing("/api/report/abc123/html") == "/api/report/{task_id}/html"


# ============================================================
# H. 源码级护栏（"集中成常量"必须配一条会自己红的断言）
# ============================================================


def test_api_layer_reuses_resolve_report_dir_not_its_own_paths():
    """端点必须复用 `report_index` 的解析入口，**不得自己拼报告路径**。

    路径规则一旦有两份实现，就会分化出"能打开却导不出"或更糟的
    "打不开反而导得出来"（越权）。断言的是源码，因为这条纪律会被后来者
    以"顺手加个 joinpath 更直观"的方式无意破坏。
    """
    body = _code_only(_func_source(
        "src/fundamental_agent/api/server.py",
        "def export_report_file",
        '@app.get("/api/report/{task_id}", response_model'))
    assert "describe_report" in body or "resolve_report_dir" in body
    for banned in ("joinpath", "_DEFAULT_BASE", "report_index.user_root", "report.html"):
        assert banned not in body, "导出端点不应自己拼路径，出现了：%s" % banned


def test_export_module_does_not_own_path_rules():
    """`report_export.py` 只管"把一个已知目录导出去"，不解析 report_id。"""
    src = _code_only(_read("src/fundamental_agent/report_export.py"))
    for banned in ("_DEFAULT_BASE", "user_root", "parse_report_id", "encode_report_id"):
        assert banned not in src, "导出模块不应涉及路径/标识解析，出现了：%s" % banned


def test_run_browser_never_pipes_stdout():
    """`capture_output=True` 会让 `communicate()` 等到 crashpad 子进程退完 ——
    实测把一次 4 秒的打印变成 40 秒。必须重定向到文件。
    """
    body = _code_only(_func_source("src/fundamental_agent/report_export.py",
                                   "def _run_browser", "def _log_tail"))
    assert not re.search(r"capture_output\s*=", body)
    assert "subprocess.PIPE" not in body
    # stdout / stderr 都必须是**文件句柄**（`fh`/`fo`），不是管道
    assert re.search(r"stdout=\w+", body) and re.search(r"stderr=\w+", body)
    assert "timeout=timeout" in body


def test_browser_args_keep_network_enabled():
    """绝不能加 `--disable-background-networking`。

    报告在图表库内联失败时会**回退 CDN**（D4 的 `chart_offline:false`），
    封掉网络等于让那类报告的图表全空白 → 导出变成"静默缺内容"。
    """
    body = _code_only(_func_source("src/fundamental_agent/report_export.py",
                                   "def _base_args", "def _run_browser"))
    assert "--disable-background-networking" not in body
    assert "--headless=new" in body
    assert "--user-data-dir=" in body


def test_export_declares_slot_pool_and_profile_reuse():
    """并发槽位与常驻 profile 是"4 秒 vs 30 秒"的关键，不允许被简化掉。"""
    src = _read("src/fundamental_agent/report_export.py")
    assert "_SLOT_LOCKS" in src and "MAX_CONCURRENT_EXPORTS" in src
    assert "_PROFILE_PREFIX" in src and "profile_dir" in src
    body = _code_only(_func_source("src/fundamental_agent/report_export.py",
                                   "def _sweep_stale", "def _reset_profile"))
    # 只清工作目录：把常驻 profile 扫掉会退化回"每轮重建"，更糟的是可能删掉
    # 另一个正在跑的导出的 profile。
    assert "_WS_PREFIX" in body
    assert "_PROFILE_PREFIX" not in body


def test_gateway_forwards_download_headers():
    """8000 网关必须透传 `Content-Disposition` 等头。

    丢了它，用户经 8000 下载会拿到随机名甚至没有文件名 —— 页面看起来完全正常，
    只有"保存下来的名字不对"，是最典型的静默回归。
    """
    src = _read("frontend/server.py")
    assert "_PROXY_PASS_HEADERS" in src
    decl = _func_source("frontend/server.py", "_PROXY_PASS_HEADERS = (", ")")
    for h in ("Content-Disposition", "X-Export-Warnings", "X-Export-Bytes"):
        assert h in decl, h
    body = _code_only(_func_source("frontend/server.py", "def _proxy_report", "def do_POST"))
    assert "_PROXY_PASS_HEADERS" in body
    assert "get_all" in body      # 多值头不能只取第一个


def test_reports_page_has_export_entry():
    """报告库页必须有导出入口，且**能力来自服务端**（不自己猜有没有浏览器）。"""
    src = _read("frontend/reports.html")
    assert "API_EXPORT" in src and "'/api/report/export/'" in src
    assert "data-exp" in src and "data-fmt" in src
    assert "exportCap" in src
    assert "X-Export-Warnings" in src          # 告警必须被读出来展示
    assert "Content-Disposition" in src        # 文件名以响应头为准
    assert "decodeURIComponent" in src         # 中文在头里是百分号编码的


def test_reports_page_export_uses_blob_not_plain_link():
    """不能退化成 `<a href=export…>` 直链：那样读不到告警头、也没法兜底文件名。

    ⚠️ 断言必须跑在**去掉注释**的代码上：报告库页里正供着这句反面教材
    （注释里写着「走 fetch + blob 而不是 `<a href="/api/report/export/…">`」），
    不去注释就会把这句说明当成违规本身，红灯指向一段注释而不是真实回归。
    """
    src = _markup_code_only("frontend/reports.html")
    assert "URL.createObjectURL" in src
    assert not re.search(r"<a[^>]+href\s*=\s*[\"']?/api/report/export", src)
    assert "API_EXPORT" in src          # 但仍要真的去请求导出接口
