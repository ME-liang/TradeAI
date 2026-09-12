"""validate_html 单元测试：JS 语法校验闭环（Step 3 / M4）。

运行：pytest tests/test_validate_html.py -q
不依赖真实 node：passed/failed 路径用假 node 脚本模拟，
skipped 路径用不存在的 node 路径模拟；另有一条真实 node 冒烟（有 node 才生效）。
"""

import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.agents.validate_html import (  # noqa: E402
    check_html_js,
    extract_inline_scripts,
)


GOOD_HTML = """
<html><body>
<script src="https://cdn.example.com/chart.js"></script>
<script>
  const x = [1, 2, 3];
  console.log(x.map(v => v * 2));
</script>
<script>
  function draw(canvas) { return canvas.getContext('2d'); }
</script>
</body></html>
"""

BAD_HTML = """
<html><body>
<script>
  const ok = 1;
</script>
<script>
  if (true) { console.log('missing brace');
</script>
</body></html>
"""


def test_extract_skips_external_scripts():
    blocks = extract_inline_scripts(GOOD_HTML)
    assert len(blocks) == 2  # 外链 CDN script 不计入
    assert "chart.js" not in blocks[0]


def test_extract_empty_for_no_scripts():
    assert extract_inline_scripts("<p>no js</p>") == []


def test_failed_on_bad_js():
    # 用真实 node（若环境无 node，跳过该断言由 skipped 测试覆盖逻辑）
    result = check_html_js(BAD_HTML, node_path="node")
    if result["status"] == "skipped":
        assert result["node"] is None
        return
    assert result["status"] == "failed"
    assert result["blocks"] == 2
    assert len(result["errors"]) == 1
    assert result["errors"][0]["block"] == 1
    assert "missing brace" in result["errors"][0]["message"] or "SyntaxError" in result["errors"][0]["message"]


def test_passed_on_good_js():
    result = check_html_js(GOOD_HTML, node_path="node")
    if result["status"] == "skipped":
        return
    assert result["status"] == "passed"
    assert result["errors"] == []


def test_skipped_when_node_missing():
    result = check_html_js(GOOD_HTML, node_path="Z:/no/such/node.exe")
    assert result["status"] == "skipped"
    assert result["blocks"] == 2  # 块仍被正确提取计数
    assert result["node"] is None


def _write_fake_node(tmp_path, exit_code, stderr):
    """生成一个模拟 node --check 行为的假可执行脚本（Windows 用 .bat）。"""
    if sys.platform == "win32":
        exe = tmp_path / "fake_node.bat"
        # 接收任意参数；按固定退出码与 stderr 返回
        exe.write_text('@echo off\r\necho ' + stderr + ' 1>&2\r\nexit /b ' + str(exit_code) + '\r\n', encoding="ascii")
    else:
        exe = tmp_path / "fake_node"
        exe.write_text(
            textwrap.dedent(
                f"""
                #!/bin/sh
                echo "{stderr}" >&2
                exit {exit_code}
                """
            ).lstrip(),
            encoding="ascii",
        )
        exe.chmod(0o755)
    return str(exe)


def test_failed_via_fake_node(tmp_path):
    fake = _write_fake_node(tmp_path, 1, "SyntaxError: unexpected token")
    result = check_html_js(GOOD_HTML, node_path=fake)
    assert result["status"] == "failed"
    assert result["errors"][0]["block"] == 0
    assert "SyntaxError" in result["errors"][0]["message"]


def test_passed_via_fake_node(tmp_path):
    fake = _write_fake_node(tmp_path, 0, "")
    result = check_html_js(GOOD_HTML, node_path=fake)
    assert result["status"] == "passed"
