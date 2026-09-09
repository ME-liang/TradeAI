"""列表页分页状态保持 端到端验证（Playwright + 已缓存 chromium）

验证路径：
  A) 个股列表 stock.html:  第1页 → 翻到第3页 + 滚动 → 点击个股进详情 → 点「返回列表」 → 应回到第3页且滚动位置保持
  B) 同上，但用浏览器后退按钮返回
  C) 从首页菜单进入 stock.html → 应默认第1页（不受状态恢复影响）
  D) 板块列表 sector.html: 同样的翻页 → 详情 → 返回 → 第3页 + 滚动保持
  E) 板块成员列表 sector_detail.html → 个股详情 → 返回 → 页码保持
"""
import re
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
CHROME = r"C:\Users\Administrator\AppData\Local\ms-playwright\chromium-1208\chrome-win64\chrome.exe"
SCROLL_Y = 300

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ((" | " + str(extra)) if extra else ""))


def meta(page):
    try:
        return page.inner_text("#meta")
    except Exception:
        return ""


def cur_page(page):
    m = re.search(r"第\s*(\d+)\s*/", meta(page))
    return int(m.group(1)) if m else None


def goto_page(page, n):
    """通过「跳至页码」组件跳到第 n 页并等待 meta 更新
    （分页器在第 1 页时不一定渲染出第 n 页按钮，故统一用跳页组件）"""
    page.fill(".jump-input", str(n))
    page.click(".jump-go")
    page.wait_for_function(
        "n => { const m = document.getElementById('meta').textContent.match(/第\\s*(\\d+)\\s*\\//); return m && +m[1] === n; }",
        arg=n, timeout=20000)


def run(pw):
    browser = pw.chromium.launch(executable_path=CHROME, headless=True)
    ctx = browser.new_context(viewport={"width": 1100, "height": 520})
    page = ctx.new_page()

    # ---------- A) 个股列表: 翻页 → 详情 → 返回链接 ----------
    print("\n=== A) 个股列表 stock.html：翻到第3页 → 进详情 → 点返回列表 ===")
    page.goto(BASE + "/stock.html")
    page.wait_for_selector(".row", timeout=30000)
    check("A1 初始为第 1 页", cur_page(page) == 1, meta(page))
    goto_page(page, 3)
    check("A2 已翻到第 3 页", cur_page(page) == 3, meta(page))
    page.evaluate(f"window.scrollTo(0, {SCROLL_Y})")
    page.wait_for_timeout(200)
    y_before = page.evaluate("window.scrollY")
    check("A3 已滚动到指定位置", y_before > 100, f"scrollY={y_before}")
    page.click(".row")
    page.wait_for_url("**/stock_detail.html**", timeout=20000)
    check("A4 已进入个股详情页", "stock_detail.html" in page.url, page.url.split("/")[-1][:40])
    page.click(".crumb a")
    page.wait_for_url("**/stock.html**", timeout=20000)
    page.wait_for_selector(".row", timeout=30000)
    page.wait_for_timeout(600)
    check("A5 返回后仍在第 3 页", cur_page(page) == 3, meta(page))
    y_after = page.evaluate("window.scrollY")
    check("A6 返回后滚动位置保持", abs(y_after - y_before) <= 60 and y_after > 100,
          f"离开前={y_before} 返回后={y_after}")

    # ---------- B) 浏览器后退按钮 ----------
    print("\n=== B) 个股列表：用浏览器后退按钮返回 ===")
    page.click(".row")
    page.wait_for_url("**/stock_detail.html**", timeout=20000)
    page.go_back()
    page.wait_for_url("**/stock.html**", timeout=20000)
    page.wait_for_timeout(800)
    check("B1 后退后仍在第 3 页", cur_page(page) == 3, meta(page))

    # ---------- C) 其它入口进入列表 → 默认第 1 页 ----------
    print("\n=== C) 从首页菜单进入个股列表：应默认第 1 页 ===")
    page.goto(BASE + "/index.html")
    page.wait_for_timeout(500)
    link = page.query_selector("a[href*='stock.html']")
    if link:
        link.click()
        page.wait_for_url("**/stock.html**", timeout=20000)
        page.wait_for_selector(".row", timeout=30000)
        page.wait_for_timeout(400)
        check("C1 菜单进入为第 1 页(不恢复)", cur_page(page) == 1, meta(page))
    else:
        check("C1 菜单进入为第 1 页(不恢复)", False, "首页未找到 stock.html 链接")

    # 新标签页直接打开(非返回导航)也应为第 1 页
    p2 = ctx.new_page()
    p2.goto(BASE + "/stock.html")
    p2.wait_for_selector(".row", timeout=30000)
    check("C2 新标签直接打开为第 1 页", cur_page(p2) == 1, meta(p2))
    p2.close()

    # ---------- D) 板块列表 ----------
    print("\n=== D) 板块列表 sector.html：翻到第3页 → 进板块详情 → 返回 ===")
    page.goto(BASE + "/sector.html")
    page.wait_for_selector(".row", timeout=30000)
    check("D1 初始为第 1 页", cur_page(page) == 1, meta(page))
    goto_page(page, 3)
    check("D2 已翻到第 3 页", cur_page(page) == 3, meta(page))
    page.evaluate(f"window.scrollTo(0, {SCROLL_Y})")
    page.wait_for_timeout(200)
    y_before = page.evaluate("window.scrollY")
    check("D3 已滚动到指定位置", y_before > 100, f"scrollY={y_before}")
    page.click(".row")
    page.wait_for_url("**/sector_detail.html**", timeout=20000)
    check("D4 已进入板块详情页", "sector_detail.html" in page.url, page.url.split("/")[-1][:40])
    page.click("a.breadcrumb")
    page.wait_for_url("**/sector.html**", timeout=20000)
    page.wait_for_selector(".row", timeout=30000)
    page.wait_for_timeout(600)
    check("D5 返回后仍在第 3 页", cur_page(page) == 3, meta(page))
    y_after = page.evaluate("window.scrollY")
    check("D6 返回后滚动位置保持", abs(y_after - y_before) <= 60 and y_after > 100,
          f"离开前={y_before} 返回后={y_after}")

    # ---------- E) 板块成员列表 → 个股详情 → 返回板块 ----------
    print("\n=== E) 板块成员列表(银行 42 只) → 个股详情 → 返回板块：页码保持 ===")
    page.goto(BASE + "/sector_detail.html?code=BK0475&name=" + "%E9%93%B6%E8%A1%8C")
    page.wait_for_selector(".row", timeout=30000)
    # 成员数少的板块可能不足 3 页, 按实际总页数取目标页
    total_pages = int(re.search(r"/\s*(\d+)\s*页", meta(page)).group(1))
    target = min(3, total_pages)
    goto_page(page, target)
    check(f"E1 板块成员列表已到第 {target} 页", cur_page(page) == target, meta(page))
    page.click(".row")
    page.wait_for_url("**/stock_detail.html**", timeout=20000)
    page.click(".crumb a")
    page.wait_for_url("**/sector_detail.html**", timeout=20000)
    page.wait_for_selector(".row", timeout=30000)
    page.wait_for_timeout(600)
    check(f"E2 返回板块后仍在第 {target} 页", cur_page(page) == target, meta(page))

    # ---------- F) 筛选 + 排序 + 翻页 组合恢复 ----------
    print("\n=== F) 个股列表：关键词 + 排序 + 第2页 → 进详情 → 返回：全部条件保持 ===")
    page.goto(BASE + "/stock.html")
    page.wait_for_selector(".row", timeout=30000)
    page.fill("#q", "银行")
    page.wait_for_timeout(1200)                       # 等待接口返回过滤结果
    page.select_option("#sortSel", "chg_desc")        # 排序改为「涨跌幅（高→低）」
    page.wait_for_timeout(1200)
    total_pages = int(re.search(r"/\s*(\d+)\s*页", meta(page)).group(1))
    target = min(2, total_pages)
    if target > 1:
        goto_page(page, target)
    filtered_meta = meta(page)
    print("    离开前:", filtered_meta)
    check("F1 关键词已生效并翻页", cur_page(page) == target, filtered_meta)
    page.click(".row")
    page.wait_for_url("**/stock_detail.html**", timeout=20000)
    page.click(".crumb a")
    page.wait_for_url("**/stock.html**", timeout=20000)
    page.wait_for_selector(".row", timeout=30000)
    page.wait_for_timeout(800)
    check("F2 返回后关键词保持", page.input_value("#q") == "银行", page.input_value("#q"))
    check("F3 返回后排序保持", page.input_value("#sortSel") == "chg_desc", page.input_value("#sortSel"))
    check("F4 返回后页码保持", cur_page(page) == target, meta(page))

    browser.close()


with sync_playwright() as pw:
    run(pw)

ok = sum(1 for _, c in results if c)
print(f"\n===== 结果: {ok}/{len(results)} 项通过 =====")
sys.exit(0 if ok == len(results) else 1)
