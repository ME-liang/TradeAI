"""个股详情页加载时间线测量（Playwright Performance API）

输出：各资源耗时（CDN echarts / /api/stock / /api/kline / 后续异步接口）、
指标区首渲染时刻、K线图表 canvas 出现时刻（图表可交互近似）。
"""
import json
import statistics
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000/stock_detail.html?code=600519"
CHROME = r"C:\Users\Administrator\AppData\Local\ms-playwright\chromium-1208\chrome-win64\chrome.exe"
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3

INIT = """
window.__marks = {};
function startMo() {
  const mo = new MutationObserver(() => {
    const name = document.getElementById('name');
    if (!window.__marks.metrics && name && name.textContent && name.textContent.trim() !== '—' && name.textContent.trim() !== '') {
      window.__marks.metrics = performance.now();
    }
    if (!window.__marks.chart && document.querySelector('#chart canvas')) {
      window.__marks.chart = performance.now();
    }
    if (window.__marks.metrics && window.__marks.chart) mo.disconnect();
  });
  mo.observe(document.documentElement, {childList: true, subtree: true, characterData: true});
}
if (document.documentElement) startMo();
else document.addEventListener('readystatechange', startMo, {once: true});
"""


def run(pw):
    browser = pw.chromium.launch(executable_path=CHROME, headless=True)
    rows = []
    for i in range(RUNS):
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.add_init_script(INIT)
        t0 = time.time()
        page.goto(BASE, wait_until="load", timeout=60000)
        page.wait_for_function(
            "window.__marks && window.__marks.metrics && window.__marks.chart", timeout=60000)
        wall = time.time() - t0
        marks = page.evaluate("window.__marks")
        res = page.evaluate("""
          performance.getEntriesByType('resource').filter(r =>
            /echarts|\\/api\\//.test(r.name)).map(r => ({
            name: r.name.replace(/^https?:\\/\\/[^/]+/, '').split('?')[0] || r.name.slice(0, 40),
            host: (r.name.match(/^https?:\\/\\/([^/]+)/) || [])[1] || '',
            dns: r.domainLookupEnd - r.domainLookupStart,
            tcp: r.connectEnd - r.connectStart,
            ttfb: r.responseStart - r.requestStart,
            dl: r.responseEnd - r.responseStart,
            size: r.transferSize,
            dur: r.responseEnd - r.startTime,
            start: r.startTime,
          }))
        """)
        nav = page.evaluate("""
          (() => { const n = performance.getEntriesByType('navigation')[0]; return {
            domInteractive: n.domInteractive, domComplete: n.domComplete, loadEvent: n.loadEventEnd }; })()
        """)
        rows.append({"run": i + 1, "wall": wall, "marks": marks, "res": res, "nav": nav})
        ctx.close()
    browser.close()
    return rows


def fmt_ms(x):
    return f"{x:7.0f}ms" if x is not None else "      —"


rows = run(sync_playwright().start())
for r in rows:
    print(f"\n===== 第 {r['run']} 次加载 (墙钟 {r['wall']:.2f}s, domComplete {fmt_ms(r['nav']['domComplete'])}) =====")
    m = r["marks"]
    print(f"  指标区首渲染: {fmt_ms(m.get('metrics'))}")
    print(f"  图表可交互  : {fmt_ms(m.get('chart'))}")
    print("  资源明细:")
    for e in sorted(r["res"], key=lambda x: x["start"]):
        label = (e["host"] + e["name"]) if e["host"] and "cdn" in e["host"] else e["name"]
        print(f"    +{fmt_ms(e['start'])} 总{fmt_ms(e['dur'])} (等待{fmt_ms(e['ttfb'])} 传输{fmt_ms(e['dl'])}) "
              f"{e['size']//1024}KB  {label[:60]}")
