"""极简 DOCX（OOXML）写入器：把报告 HTML 转成**结构化**的 Word 文档（F-07）。

**为什么自己写而不是引三方库**：
  1. F-07 明确要求**零新增依赖**（`requirements.txt` 里没有 `python-docx`）；
  2. 报告 HTML 的语义集合很小（标题 / 段落 / 列表 / 表格 / 图表），真正需要的
     OOXML 子集不到 500 行，引一个通用库反而带来"它支持但报告没用"的维护面；
  3. 自己写才能把「**内容不许静默丢失**」变成可断言的事情 —— `html_to_docx()`
     返回 `stats`（各类块的条数 + 图表数），测试直接拿它跟 HTML 里的标签数比对。
     这正是本项目反复强调的那条纪律：**导出结果"能打开"不等于"内容还在"**。

**职责边界（写清楚，避免被误当成通用转换器）**：
  - 只处理报告模板实际用到的标签；不认识的标签**降级为文本**而不是整块丢掉；
  - **不保留视觉**（配色 / 圆角 / 徽标 / 卡片的版式）。Word 版的定位是
    "**可编辑、可引用文字与数据**"，要还原视觉请用 PDF 版（清单 F-07 三格式分工）；
  - 图表不作为矢量重绘，而是插入**同一份 PDF 里已光栅化的图片**（见 `report_export.py`）——
    这样不必再跑一次浏览器，且与 PDF 版看到的是同一张图。

**OOXML 最小必需包**（少任何一个 Word 都会报"文件已损坏"）：
```
[Content_Types].xml          每个 part 的 MIME（Override）+ 扩展名默认（Default）
_rels/.rels                  包级关系：document.xml / core.xml / app.xml
word/document.xml            正文
word/_rels/document.xml.rels 文档级关系：styles.xml + 每个 media 图片
word/styles.xml              样式（Heading1..4 必须用内置名 "heading N"，否则 Word 不认作标题）
word/media/*                 插图
docProps/core.xml / app.xml  文档属性（可缺，但补上后 Word 的"属性"面板才有内容）
```
"""

from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from bs4 import BeautifulSoup, NavigableString

# ===== 常量 =====

# A4（twips：1 inch = 1440 twips）
_PAGE_W_TW = 11906
_PAGE_H_TW = 16838
_MARGIN_TW = 1418  # 2.5cm，与中文公文/研报习惯一致
# 1 twip = 635 EMU（914400 EMU/inch ÷ 1440 twip/inch）
_CONTENT_W_EMU = (_PAGE_W_TW - 2 * _MARGIN_TW) * 635
_CONTENT_H_EMU = (_PAGE_H_TW - 2 * _MARGIN_TW) * 635

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"

_REL_DOC = _R_NS.replace("/relationships", "/relationships/officeDocument")
_REL_STYLES = _R_NS + "/styles"
_REL_IMAGE = _R_NS + "/image"
_REL_CORE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
_REL_APP = _R_NS + "/extended-properties"

# XML 1.0 不允许的控制字符：不清掉会让 Word 直接判定文件损坏
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")

# 会被当成"内容块"、按语义输出的标签（已知块）。注意 `li` 与 `table` 都在内：
# 报告里大量信息在表格与列表里，只抓 p 会**静默丢掉大半内容**。
#
# ⚠️ 它**不是**"处理的全部范围"：没有列在这里的标签（div / span / section …）里的文字
# 也要输出 —— 见 `extract_blocks` 里"不在已知块名单里"那一段。少写这一条，报告里凡是用
# `<div class="kv">标签值</div>` 这类容器装的文字就会整块消失，而**页面上一切正常**。
_KNOWN_BLOCK_NAMES = frozenset({"h1", "h2", "h3", "h4", "h5", "p", "li", "table", "canvas", "svg"})


def _has_known_block_descendant(el) -> bool:
    """该元素内部是否含**已知内容块** —— 决定它是"容器"还是"块本身"。

    ⚠️ 必须自己遍历标签名判断，**不能用 `el.find("h1,h2,h3,p,…")`**：
    BeautifulSoup 的 `find()` 把字符串当**标签名**（不是 CSS 选择器），那个表达式
    永远匹配不到任何东西、恒为 `False`。后果是容器被当成一个普通块，
    `_runs_of` 把子块文字**全吞进同一段**，子块随后又被 `emitted` 跳过 ——
    实测 `<ul><li>甲</li><li>乙</li></ul>` 会变成一段"甲乙"，
    且 `stats["list_items"] == 0`。**页面上一切正常，只有导出物里列表结构没了。**
    """
    for d in el.descendants:
        name = getattr(d, "name", None)
        if name in _KNOWN_BLOCK_NAMES:
            return True
        if _chart_slot_kind(d):
            return True
    return False

_MAX_HEADING = 5


def _xesc(s: Any) -> str:
    """XML 文本转义（先清非法控制字符，再转义 —— 顺序不能反）。"""
    t = _BAD_XML.sub("", str(s if s is not None else ""))
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def _norm(s: Any) -> str:
    """HTML 空白折叠（与浏览器一致），并去掉首尾空白。"""
    return " ".join(str(s if s is not None else "").split())


# ===== HTML → 中间表示（块序列） =====


def _runs_of(node, bold: bool = False) -> list[tuple[str, bool]]:
    """把一个节点内的行内内容摊平成 [(文本, 是否加粗)]。

    只保留**加粗**这一个语义：报告里的强调几乎全用 `<strong>/<b>`，而配色是靠
    CSS class 表达的（内联样式里拿不到），硬猜会猜错。宁可少一种装饰，
    也不要制造"Word 里颜色和网页不一样"的错觉。

    换行用 `"\\n"` 标记，由 `_runs_xml` 转成 `<w:br/>` —— `<w:t>` 里的
    真实换行在 Word 中会被当成普通空白，看起来就是"少了个回车"。
    """
    out: list[tuple[str, bool]] = []
    for child in node.children:
        name = getattr(child, "name", None)
        if name is None:
            txt = _norm(child)
            if txt:
                out.append((txt, bold))
            continue
        if name in ("script", "style", "noscript", "svg"):
            # svg：基本面报告用内联 SVG 画图，里面的 `<text>` 是坐标刻度与数值标签，
            # 摊平成正文会得到一串"0 20 40 60 -1.2"式的垃圾。矢量图属于图表，
            # 由 `chart_slots` 处理，不从文本通道走。
            continue
        if name == "br":
            out.append(("\n", bold))
            continue
        out.extend(_runs_of(child, bold or name in ("strong", "b")))
    return out


def _merge(runs: Sequence[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """合并相邻同格式 run，并裁掉整体首尾空白（避免 Word 里出现孤立空格）。"""
    merged: list[tuple[str, bool]] = []
    for txt, bold in runs:
        if not txt:
            continue
        if merged and merged[-1][1] == bold:
            merged[-1] = (merged[-1][0] + txt, bold)
        else:
            merged.append((txt, bold))
    while merged and not merged[0][0].strip():
        merged.pop(0)
    while merged and not merged[-1][0].strip():
        merged.pop()
    return merged


def _top_rows(tbl) -> list:
    """表格的顶层级 `<tr>`（排除嵌套表格里的行，否则会串行）。"""
    rows = []
    for tr in tbl.find_all("tr"):
        anc = tr.find_parent("table")
        if anc is tbl or anc is None:
            rows.append(tr)
        # anc 是别的 table → 属于嵌套表格，跳过
    return rows


def _table_data(tbl) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for tr in _top_rows(tbl):
        cells = []
        for c in tr.find_all(["th", "td"], recursive=False):
            try:
                span = int(c.get("colspan") or 1)
            except (TypeError, ValueError):
                span = 1
            cells.append({
                "text": _norm(c.get_text(" ", strip=True)),
                "head": c.name == "th",
                "span": max(1, min(span, 64)),
            })
        if cells:
            rows.append(cells)
    return rows


def _chart_slot_kind(el) -> str | None:
    """元素是不是"图表"：返回 `"canvas"` / `"svg"` / `"div"`，否则 `None`。

    ⚠️ 类名必须**逐 token 精确匹配**，绝不能用 `"chart" in cls` 这种子串判断：
    模板里除真图表外还有 `card growth-chart` / `prof-chart` / `efficiency-chart` /
    `solvency-chart`（卡片外框）、`chart-hd`（表头）、`chart-empty`（空态占位）。
    子串判断会把它们统统当成图表槽位 —— 实测某份基本面报告的槽位从 **4 个虚增到 12 个**，
    后果是 Word 版凭空多出 8 个"未能取到图片"的空占位（**凭空多出来的内容**），
    而且图片与槽位还会错位。

    三种形态（缺一种，对应报告的图表就会整块消失）：
      - `canvas`：Chart.js（概况报告）；
      - `div.chart`：ECharts 运行时才往里塞 canvas（板块报告）—— 静态 HTML 里它是空的；
      - `svg.svgchart`：基本面报告的服务端矢量图（标记里就有，不需要 JS）。
    """
    name = getattr(el, "name", None)
    if name is None:       # 遍历 `descendants` 时会碰到 NavigableString（纯文本节点），跳过
        return None
    cls = set(el.get("class") or [])
    if name == "canvas":
        return "canvas"
    if name == "svg" and ("svgchart" in cls or "chart" in cls):
        return "svg"
    if name == "div" and "chart" in cls and not el.find("canvas"):
        return "div"
    return None


def _soup_of(html: str):
    """解析报告 HTML 并去掉不产出内容的节点（`script` / `style` / `noscript` / `template`）。"""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    return soup


def _root_of(soup):
    """报告主容器：模板只有这两种（fundamental/overview/industry: `.page`，board: `.wrap`）。"""
    return soup.select_one(".page") or soup.select_one(".wrap") or soup.body or soup


def chart_slots(root) -> list:
    """图表节点，**按文档序**返回（Word 版按这个顺序把位图依次填进槽位）。

    ⚠️ 顺序错了就会出现"第一张图和第二个图对不上"，而且看起来一切正常 ——
    图片是按位置塞进占位里的，错位没有任何外部信号。

    嵌套去重：`div.chart` 里若还嵌着 `canvas`/`svg`，只算**外层**那一个
    （`_chart_slot_kind` 已对"含 canvas 的 div.chart"返回 None，这里再兜一层）。
    """
    out = []
    for el in root.find_all(True):
        if not _chart_slot_kind(el):
            continue
        if any(_chart_slot_kind(a) for a in el.parents):
            continue
        out.append(el)
    return out


def chart_slot_kinds(html: str) -> list[str]:
    """图表槽位的**形态序列**（文档序）：`"canvas"` / `"svg"` / `"div"`。

    `report_export` 用它判断每个槽位该填什么：`canvas`/`div` 的图能从打印结果里
    抽出位图；`svg` 抽不出来（PDF 里是矢量），必须单独栅格化。
    """
    root = _root_of(_soup_of(html))
    return [k for k in (_chart_slot_kind(el) for el in chart_slots(root)) if k]


def count_js_charts(html: str) -> int:
    """数**需要浏览器 JS 在运行时作画**的图表（`canvas` + 不含 canvas 的 `div.chart`）。

    ⚠️ 故意**不数内联 `<svg>`**：SVG 是标记里就有的矢量图，它必然出现在 PDF 里
    （只是以矢量而非位图形式），把它算进来会得出"PDF 里没有图表图片"的**误报**。
    这个数只用于回答一个问题："JS 图表库到底有没有加载成功"。
    """
    return sum(1 for el in _soup_of(html).find_all(True)
               if _chart_slot_kind(el) in ("canvas", "div"))


def _has_block_ancestor(el) -> bool:
    """该元素是否已由某个"外层块"承载（避免重复输出，也避免把图表的零件当正文）。

    - 表格单元格里的 `<p>`：由表格块负责，跳过；
    - 列表项里的 `<p>` / 嵌套 `<li>`：文本已由外层 `li` 的 `_runs_of` 递归收集，
      再单独成段就是**重复内容**。折叠会丢掉子列表的缩进层级，但不丢文字 ——
      宁可少一层缩进，也不要同一句话出现两遍。
    - 嵌套表格：由外层表格的单元格文本承载，内层跳过；
    - `svg` 之内：矢量图的 `<text>` 是坐标刻度/数值标签，不是正文
      （基本面报告的内联图表就长这样，漏了这条会往 Word 里灌进一串数字）。
    """
    for anc in el.parents:
        name = getattr(anc, "name", None)
        if name in ("td", "th", "p", "li", "table", "svg"):
            return True
        if name in ("body", "html"):
            break
    return False


def _direct_runs(node, bold: bool = False) -> list[tuple[str, bool]]:
    """只取该元素的**直接文本节点**（不进入子元素），并保留 `<br>` 造成的换行。

    用途：元素内含已知块（例如 `<div>前文<table>…</table>后文</div>`）时，
    子块自己会成块输出，这里只负责把**属于本元素自己的**那段文字捡回来 ——
    否则「前文 / 后文」会随着父容器被整块跳过而消失。
    """
    out: list[tuple[str, bool]] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append((str(child), bold))
        elif getattr(child, "name", None) == "br":
            out.append(("\n", bold))
    return out


def extract_blocks(html: str) -> tuple[list[dict], dict]:
    """把报告 HTML 摊平成块序列，并给出**计数**（供测试断言"内容没丢"）。

    返回 `(blocks, stats)`；`stats` 含 `headings / paragraphs / list_items / tables /
    table_rows / charts / nested_tables`。

    两条防止"内容静默丢失"的规则：
      1. 按**文档序**遍历全部元素（不只是已知块标签）—— 不认识的结构降级为段落，
         而不是整块丢掉；
      2. 祖先已产出块时，后代不再重复输出（`_covered`）—— 表格单元格里的 `<p>`、
         列表项里的 `<p>`、嵌套表格，文字都已由外层承载。
    折叠会丢掉子列表的缩进层级，但**不丢文字** —— 宁可少一层缩进，也不要同一句话两遍。
    """
    soup = _soup_of(html)
    root = _root_of(soup)

    chart_ids = {id(n) for n in chart_slots(root)}

    stats = {"headings": 0, "paragraphs": 0, "list_items": 0, "tables": 0,
             "table_rows": 0, "charts": 0, "nested_tables": 0}
    blocks: list[dict] = []
    emitted: set[int] = set()

    def _covered(el) -> bool:
        """祖先里已有**覆盖整棵子树**的产出块 → 本元素的文字已被带走，不再重复输出。

        ⚠️ 只有"整棵子树都被这一次输出包含"时才算覆盖（`emitted` 只在那种情况下登记）。
        容器**只输出自身直连文字**时绝不能登记 —— 否则 `<div>前文<h3>标题</h3></div>`
        里的 `<h3>` 会被当成"已被容器带走"而**整块丢掉标题**。实测真实报告：
        30 个标题只输出 22 个、5 张表只剩 4 张，而**页面上一切正常**。
        """
        for anc in el.parents:
            if id(anc) in emitted:
                return True
            if getattr(anc, "name", None) in ("body", "html"):
                break
        return False

    for el in root.find_all(True):      # 文档序：祖先必然先于后代被访问
        if id(el) in chart_ids:         # canvas / svg.svgchart / 不含 canvas 的 div.chart
            stats["charts"] += 1
            blocks.append({"kind": "chart"})
            emitted.add(id(el))
            continue
        name = el.name
        if name in ("canvas", "svg") or (name == "div" and "chart" in (el.get("class") or [])):
            continue                    # 非图表的 canvas/svg、含 canvas 的 div.chart：都不出文本
        if _covered(el) or _has_block_ancestor(el):
            continue

        # 已知块（h/p/li/table）：它们的输出**天然覆盖自己整棵子树**（`_runs_of` 递归、
        # `_table_data` 收全单元格），故输出后登记 `covers_subtree = True`。
        covers_subtree = True
        if name in ("h1", "h2", "h3", "h4", "h5"):
            lvl = max(1, min(_MAX_HEADING, int(name[1])))
            runs = _merge(_runs_of(el))
            if not runs:
                continue
            stats["headings"] += 1
            blocks.append({"kind": "h", "level": lvl, "runs": runs})
        elif name == "li":
            runs = _merge(_runs_of(el))
            if not runs:
                continue
            stats["list_items"] += 1
            blocks.append({"kind": "li", "runs": runs})
        elif name == "table":
            data = _table_data(el)
            if not data:
                continue
            stats["tables"] += 1
            stats["table_rows"] += len(data)
            blocks.append({"kind": "table", "rows": data})
        elif name == "p":
            runs = _merge(_runs_of(el))
            if not runs:
                continue
            stats["paragraphs"] += 1
            blocks.append({"kind": "p", "runs": runs})
        else:
            # 不在已知块名单里的标签（div / span / section / dl …）：文字绝不消失。
            #   ① 内含已知块（如 div 里包着一张表）→ 只输出它**自己的直接文本**
            #      （`covers_subtree = False`：子块还要各自成块，不能被登记成"已覆盖"）；
            #   ② 否则整体作为一段输出，子节点随后由 `_covered` 跳过。
            if _has_known_block_descendant(el):
                covers_subtree = False
                runs = _merge(_direct_runs(el))
            else:
                runs = _merge(_runs_of(el))
            if not runs:
                continue
            stats["paragraphs"] += 1
            blocks.append({"kind": "p", "runs": runs})
        if covers_subtree:
            emitted.add(id(el))     # 只有覆盖整棵子树时才登记，见 `_covered` 的说明

    # 嵌套表格单独统计：它们被父表格的单元格文本承载，不再单独成块，
    # 但要在 stats 里可见（否则"表格数少了"这件事会无人发现）。
    stats["nested_tables"] = sum(1 for t in root.find_all("table") if t.find_parent("table"))
    return blocks, stats


# ===== 中间表示 → OOXML =====


def _runs_xml(runs: Sequence[tuple[str, bool]], base_bold: bool = False) -> str:
    out = []
    for txt, bold in runs:
        parts = txt.split("\n")
        for i, seg in enumerate(parts):
            if i:  # 换行用 <w:br/>，不能把 "\n" 直接写进 <w:t>（Word 会当普通字符）
                out.append("<w:r><w:br/></w:r>")
            if not seg:
                continue
            rpr = "<w:rPr><w:b/></w:rPr>" if (bold or base_bold) else ""
            out.append('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, _xesc(seg)))
    return "".join(out)


def _para(runs: Sequence[tuple[str, bool]], style: str = "", base_bold: bool = False) -> str:
    ppr = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
    return "<w:p>%s%s</w:p>" % (ppr, _runs_xml(runs, base_bold))


def _image_para(rel_id: str, name: str, w_emu: int, h_emu: int, idx: int) -> str:
    return (
        "<w:p><w:pPr><w:jc w:val=\"center\"/></w:pPr><w:r><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        '<wp:extent cx="%d" cy="%d"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        '<wp:docPr id="%d" name="%s"/>'
        '<a:graphic><a:graphicData uri="%s/picture">'
        '<pic:pic><pic:nvPicPr><pic:cNvPr id="%d" name="%s"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
        % (w_emu, h_emu, idx, _xesc(name), _PIC_NS, idx, _xesc(name), rel_id, w_emu, h_emu)
    )


def _table_xml(rows: Sequence[Sequence[dict]]) -> str:
    """表格：边框内联（不依赖 styles.xml 里的表样式，少一个失败点）+ 表头底纹加粗。"""
    borders = (
        '<w:tblBorders>'
        '<w:top w:val="single" w:sz="4" w:color="D0D5DD"/>'
        '<w:left w:val="single" w:sz="4" w:color="D0D5DD"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="D0D5DD"/>'
        '<w:right w:val="single" w:sz="4" w:color="D0D5DD"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="E4E7EC"/>'
        '<w:insideV w:val="single" w:sz="4" w:color="E4E7EC"/>'
        "</w:tblBorders>"
    )
    cols = max((len(r) for r in rows), default=0) or 1
    grid = "".join('<w:gridCol w:w="%d"/>' % (_CONTENT_W_EMU // 635 // cols) for _ in range(cols))
    out = ['<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>%s</w:tblPr><w:tblGrid>%s</w:tblGrid>'
           % (borders, grid)]
    for r in rows:
        out.append("<w:tr>")
        for c in r:
            tcpr = ['<w:tcPr><w:tcW w:w="0" w:type="auto"/>']
            if c.get("head"):
                tcpr.append('<w:shd w:val="clear" w:color="auto" w:fill="F2F4F7"/>')
            if int(c.get("span") or 1) > 1:
                tcpr.append('<w:gridSpan w:val="%d"/>' % int(c["span"]))
            tcpr.append("</w:tcPr>")
            body = c.get("text") or ""
            out.append("<w:tc>%s%s</w:tc>" % ("".join(tcpr), _para([(body, False)], base_bold=bool(c.get("head")))))
        out.append("</w:tr>")
    out.append("</w:tbl>")
    # 表格后补一个空段：Word 里两张表紧挨会粘成一张
    out.append("<w:p/>")
    return "".join(out)


def _heading_style(level: int) -> str:
    return "Heading%d" % max(1, min(_MAX_HEADING, level))


def _styles_xml() -> str:
    sizes = {1: 36, 2: 28, 3: 24, 4: 22, 5: 21}  # half-points
    colors = {1: "1B2330", 2: "185FA5", 3: "1B2330", 4: "4A5464", 5: "4A5464"}
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:styles xmlns:w="%s">' % _W_NS,
        "<w:docDefaults>"
        "<w:rPrDefault><w:rPr>"
        '<w:rFonts w:ascii="Segoe UI" w:hAnsi="Segoe UI" w:eastAsia="Microsoft YaHei" w:cs="Segoe UI"/>'
        '<w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr></w:rPrDefault>'
        "<w:pPrDefault><w:pPr>"
        '<w:spacing w:before="0" w:after="120" w:line="300" w:lineRule="auto"/>'
        "</w:pPr></w:pPrDefault>"
        "</w:docDefaults>",
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:qFormat/></w:style>',
    ]
    for lv in range(1, _MAX_HEADING + 1):
        parts.append(
            '<w:style w:type="paragraph" w:styleId="Heading%d">'
            # ⚠️ 名字必须是内置名 "heading N"，否则 Word 的导航窗格 / 目录不认。
            # 这里有两个 %d 都用 lv：styleId 与 name 必须一致，否则样式找不到定义，
            # Word 会退化成"所有标题都长得跟正文一样"（而且不报错）。
            '<w:name w:val="heading %d"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
            "<w:qFormat/>"
            '<w:pPr><w:keepNext/><w:outlineLvl w:val="%d"/>'
            '<w:spacing w:before="%d" w:after="%d"/></w:pPr>'
            "<w:rPr><w:b/><w:sz w:val=\"%d\"/><w:color w:val=\"%s\"/></w:rPr>"
            "</w:style>" % (lv, lv, lv - 1,
                            280 if lv <= 2 else 200, 140 if lv <= 2 else 100,
                            sizes[lv], colors[lv])
        )
    parts.append("</w:styles>")
    return "".join(parts)


def _document_xml(blocks: Sequence[dict], image_meta: Sequence[dict | None]) -> str:
    """按块序列生成正文 XML。

    `image_meta` 与**图表块**一一对应，元素为图片元信息或 `None`（= 这个位置没有图）。
    `None` 一律写成**可见的占位说明**，绝不留空 —— 静默留空会让用户以为"报告本来就没图"。
    """
    body: list[str] = []
    img_i = 0
    for b in blocks:
        k = b["kind"]
        if k == "h":
            body.append(_para(b["runs"], style=_heading_style(b["level"])))
        elif k == "p":
            body.append(_para(b["runs"]))
        elif k == "li":
            body.append(_para([("· ", False)] + list(b["runs"])))
        elif k == "table":
            body.append(_table_xml(b["rows"]))
        elif k == "chart":
            m = image_meta[img_i] if img_i < len(image_meta) else None
            img_i += 1
            if m:
                body.append(_image_para(m["rel_id"], m["name"], m["w_emu"], m["h_emu"], img_i))
            else:
                body.append(_para([("［图表：未能从渲染结果中取到图片］", False)]))
    # 多出来的图片（例如报告里没有图表占位符、但打印结果里确实有图）→ 追加"图表"附录，绝不丢弃
    extras = [m for m in image_meta[img_i:] if m]
    if extras:
        body.append(_para([("附：图表", False)], style=_heading_style(2)))
        for n, m in enumerate(extras):
            body.append(_image_para(m["rel_id"], m["name"], m["w_emu"], m["h_emu"], n + 1))
    body.append(
        "<w:sectPr>"
        '<w:pgSz w:w="%d" w:h="%d"/>'
        '<w:pgMar w:top="%d" w:right="%d" w:bottom="%d" w:left="%d" w:header="851" w:footer="992" w:gutter="0"/>'
        "</w:sectPr>" % (_PAGE_W_TW, _PAGE_H_TW, _MARGIN_TW, _MARGIN_TW, _MARGIN_TW, _MARGIN_TW)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="%s" xmlns:r="%s" xmlns:wp="%s" xmlns:a="%s" xmlns:pic="%s">'
        "<w:body>%s</w:body></w:document>"
        % (_W_NS, _R_NS, _WP_NS, _A_NS, _PIC_NS, "".join(body))
    )


def _content_types(has_png: bool, has_jpeg: bool) -> str:
    defaults = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                '<Default Extension="xml" ContentType="application/xml"/>']
    if has_png:
        defaults.append('<Default Extension="png" ContentType="image/png"/>')
    if has_jpeg:
        defaults.append('<Default Extension="jpeg" ContentType="image/jpeg"/>')
        defaults.append('<Default Extension="jpg" ContentType="image/jpeg"/>')
    overrides = [
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>',
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
    ]
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">%s%s</Types>'
            % ("".join(defaults), "".join(overrides)))


def _root_rels() -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="%s" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="%s" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="%s" Target="docProps/app.xml"/>'
            "</Relationships>" % (_REL_DOC, _REL_CORE, _REL_APP))


def _doc_rels(image_names: Sequence[str]) -> str:
    rels = ['<Relationship Id="rIdStyles" Type="%s" Target="styles.xml"/>' % _REL_STYLES]
    for i, nm in enumerate(image_names, start=1):
        rels.append('<Relationship Id="rIdImg%d" Type="%s" Target="media/%s"/>'
                    % (i, _REL_IMAGE, nm))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>'
            % "".join(rels))


def _core_xml(title: str, author: str = "TradeAI") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
            ' xmlns:dcterms="http://purl.org/dc/terms/"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            "<dc:title>%s</dc:title><dc:creator>%s</dc:creator><cp:lastModifiedBy>%s</cp:lastModifiedBy>"
            '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
            "</cp:coreProperties>" % (_xesc(title), _xesc(author), _xesc(author), ts, ts))


def _app_xml() -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"'
            ' xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            "<Application>TradeAI</Application></Properties>")


# ===== 对外入口 =====


def html_to_docx(
    html: str,
    out_path: str | Path,
    *,
    images: Iterable[tuple[bytes, str] | None] = (),
    title: str = "",
) -> dict:
    """把报告 HTML 写成 `.docx`。

    `images` 按文档序对应各图表占位符，元素为 `(图片字节, 扩展名)` 或 **`None`**
    （`None` = 这个位置确实没有图，留可见占位说明）。
    多出来的图片进"附：图表"附录（**绝不丢弃**），不足的位置留可见占位说明。

    返回 `{"blocks":…, "tables":…, "charts":…, "images_used":…, "stats":…, "warnings":[…]}`。
    """
    blocks, stats = extract_blocks(html)

    warnings: list[str] = []
    # 图片尺寸：按内容宽度等比缩放，过高的图（长图式图表）再按内容高度收一次
    media: list[dict | None] = []      # 与 images 等长，位置即"第几个图表槽位"
    built: list[dict] = []             # 真正写进包的媒体（编号从这里取，不受空洞影响）
    seen_ext: set[str] = set()
    for i, item in enumerate(images, start=1):
        if item is None:               # 该槽位没有图（如矢量图未能栅格化）→ 留占位
            media.append(None)
            continue
        blob, ext = item
        ext = (ext or "png").lower().lstrip(".")
        if ext == "jpg":
            ext = "jpeg"
        if ext not in ("png", "jpeg"):
            warnings.append("第 %d 张图片扩展名 %s 不受支持，已跳过" % (i, ext))
            media.append(None)
            continue
        w_px, h_px = _image_size(blob) or (1400, 500)
        w_emu = _CONTENT_W_EMU
        h_emu = int(w_emu * h_px / w_px) if w_px else int(w_emu * 0.36)
        if h_emu > _CONTENT_H_EMU:
            h_emu = _CONTENT_H_EMU
            w_emu = int(h_emu * w_px / h_px) if h_px else w_emu
        name = "image%d.%s" % (len(built) + 1, ext)
        seen_ext.add(ext)
        meta = {"blob": blob, "ext": ext, "name": name,
                "rel_id": "rIdImg%d" % (len(built) + 1),
                "w_emu": w_emu, "h_emu": h_emu}
        built.append(meta)
        media.append(meta)

    chart_slots = stats["charts"]
    in_slots = sum(1 for m in media[:chart_slots] if m)
    extras = sum(1 for m in media[chart_slots:] if m)
    if chart_slots and not in_slots:
        # 只陈述**确凿的事实**：这个位置没拿到图。至于"为什么"（矢量图 / 图表库没加载），
        # 本模块无从判断 —— 那句解释由知道原因的上层（`report_export.export_docx`）补，
        # 在这里替它猜一个原因，就会在另一种情形下变成误导性文案（N-18 的教训）。
        warnings.append("报告有 %d 个图表位置，但未取到任何图表图片" % chart_slots)
    elif chart_slots > in_slots:
        warnings.append("报告有 %d 个图表位置，但只取到 %d 张图（可能图表库未加载、"
                        "或该图无数据）" % (chart_slots, in_slots))
    if extras:
        warnings.append("多出的 %d 张图未对应到图表位置，已放入「附：图表」" % extras)

    if not blocks:
        warnings.append("未从报告 HTML 中解析出任何内容块")

    out_path = Path(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _content_types("png" in seen_ext, "jpeg" in seen_ext))
        z.writestr("_rels/.rels", _root_rels())
        z.writestr("docProps/core.xml", _core_xml(title or "分析报告"))
        z.writestr("docProps/app.xml", _app_xml())
        z.writestr("word/document.xml", _document_xml(blocks, media))
        z.writestr("word/styles.xml", _styles_xml())
        z.writestr("word/_rels/document.xml.rels", _doc_rels([m["name"] for m in built]))
        for m in built:
            z.writestr("word/media/%s" % m["name"], m["blob"])

    return {
        "blocks": len(blocks),
        "tables": stats["tables"],
        "charts": chart_slots,
        "images_used": sum(1 for m in media if m),
        "stats": stats,
        "warnings": warnings,
    }


def _image_size(blob: bytes) -> tuple[int, int] | None:
    """读图片像素尺寸（只认 PNG / JPEG 头，不引依赖）。

    拿不到尺寸时返回 None，调用方按 1400×500 的常见图表比例兜底 ——
    宁可比例略差，也不要让整张图因为读不出尺寸而消失。
    """
    try:
        if blob[:8] == b"\x89PNG\r\n\x1a\n" and len(blob) >= 24:
            w = int.from_bytes(blob[16:20], "big")
            h = int.from_bytes(blob[20:24], "big")
            return (w, h) if w and h else None
        if blob[:2] == b"\xff\xd8":
            i = 2
            n = len(blob)
            while i + 9 < n:
                if blob[i] != 0xFF:
                    i += 1
                    continue
                marker = blob[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = int.from_bytes(blob[i + 2:i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h = int.from_bytes(blob[i + 5:i + 7], "big")
                    w = int.from_bytes(blob[i + 7:i + 9], "big")
                    return (w, h) if w and h else None
                i += 2 + seg_len
    except Exception:  # noqa: BLE001 — 尺寸读不出不该让导出失败
        return None
    return None
