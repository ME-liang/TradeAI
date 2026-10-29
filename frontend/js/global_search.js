/* ===== F-02 首页全局搜索框 =====
 *
 * 一个输入框承载两类查询：股票（名称 / 代码）与板块（名称 / 代码 BKxxxx），
 * 输入后自动判定类型并跳转到对应详情页。
 *
 * 设计依据：docs/F-02-首页全局搜索框-设计方案.md
 *   §3 入参识别与匹配规则  → normalize / classify / levelOf
 *   §4 数据获取与性能预算  → schedule(300ms) + abort 单飞 + seq 丢弃 + 1.5s 慢提示
 *   §5 结果分组与歧义处理  → collectStocks / collectBoards / render
 *   §6 跳转映射与校验      → openOption
 *   §7 交互与状态设计      → S0~S7 状态机 / 键盘 / 无障碍 / 移动端
 *
 * 数据源复用既有接口（Phase 1 后端零改动）：
 *   /api/stocks?q=&page=&pageSize=&sort=&order=       → {stocks[],total,data_status,source_updated}
 *   /api/boards_list?type=&q=&page=&pageSize=        → {boards[],total,data_status,data_updated}
 *
 * 铁律（与 2026-09-23 两次误报事故同源）：前端只依据 data_status 分流，
 * 绝不依据 total===0 判断"没有这个东西"。
 */
(function () {
  'use strict';

  var container = document.getElementById('gsearch');
  if (!container) return;
  var input = document.getElementById('gsearchInput');
  var clearBtn = document.getElementById('gsearchClear');
  var cancelBtn = document.getElementById('gsearchCancel');
  var panel = document.getElementById('gsearchPanel');
  var live = document.getElementById('gsearchLive');
  if (!input || !panel) return;

  // ===== 常量（与设计方案 §3/§4/§5 对应）=====
  var API_STOCKS = '/api/stocks';
  var API_BOARDS = '/api/boards_list';
  var DEBOUNCE_MS = 300;        // §4.1 与 sector.html 既有防抖一致
  var SLOW_HINT_MS = 1500;      // §4.1 超时后进入"仍在加载"降级态（不 abort）
  var MAX_STOCKS = 8;           // §5.1 股票分组上限
  var MAX_BOARDS = 6;           // §5.1 板块分组上限
  var BOARD_PER_KIND = 4;       // 单类板块请求条数（行业/概念各一）
  var MIN_DIGITS = 3;           // §3.2 少于 3 位数字不触发
  var MAX_QUERY_LEN = 50;       // §5.2 A8 关键词上限

  // ===== 运行时状态 =====
  var seq = 0;                  // 请求批次序号（丢弃乱序/过期响应）
  var ctrl = null;              // AbortController（单飞）
  var debTimer = null;
  var slowTimer = null;
  var slowNotice = false;
  var composing = false;        // 输入法组合中（拼音中间态不发请求）
  var warmDone = false;         // 首焦预热只做一次
  var isOpen = false;
  var options = [];             // 扁平可选项（跨分组），供键盘导航
  var active = -1;
  var cur = { c: null, acc: null };
  var lastQuery = '';
  var _pfCode = '';             // 详情页预取去重

  // ===== 基础工具 =====
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function closestEl(node, sel) {
    while (node && node !== panel) {
      if (node.nodeType === 1 && node.matches && node.matches(sel)) return node;
      node = node.parentNode;
    }
    return null;
  }

  function buildQuery(obj) {
    var sp = new URLSearchParams();
    Object.keys(obj).forEach(function (k) {
      if (obj[k] !== null && obj[k] !== undefined) sp.set(k, String(obj[k]));
    });
    return sp.toString();
  }

  /** §3.1 归一化：去空白与分隔符 → 全角转半角 → 大写 */
  function normalize(raw) {
    return String(raw == null ? '' : raw)
      .replace(/[\s\u3000·。\-_.,、]/g, '')
      .replace(/[\uFF01-\uFF5E]/g, function (c) {
        return String.fromCharCode(c.charCodeAt(0) - 0xFEE0);   // 全角 → 半角
      })
      .toUpperCase();
  }

  /** §3.1 剥离市场前后缀：600519.SH / sh600519 → 600519 */
  function codeCore(n) {
    return n.replace(/^(SH|SZ|BJ)/, '').replace(/(SH|SZ|BJ)$/, '');
  }

  /** §3.1 步骤 5：仅用于比对，剥离 ST / *ST / XD / DR / N 修饰符（不改变展示） */
  function stripMark(name) {
    var s = String(name || '');
    var t = s.replace(/^\*?(ST|XD|DR|N)\s*/, '');
    return t || s;
  }

  // ===== §3.2 形态分流 =====
  function classify(raw) {
    var n = normalize(raw);
    if (!n) return { form: 'empty', n: '' };
    if (n.length > MAX_QUERY_LEN) return { form: 'too-long', n: n };
    if (/^BK\d{4}$/.test(n)) return { form: 'board-code', n: n };
    var core = codeCore(n);
    if (/^\d{6}$/.test(core)) return { form: 'stock-code', n: core };
    if (/^\d{4}$/.test(core)) return { form: 'weak-board', n: core };
    if (/^\d{1,3}$/.test(core)) return { form: 'too-short', n: n };
    return { form: 'text', n: n };
  }

  // ===== §3.3 匹配分级：0 精确 / 1 前缀 / 2 子串 / 3 仅行业字段命中 =====
  function levelOf(name, code, ind, c) {
    var q = c.n;
    var nm = normalize(stripMark(name));
    var cd = normalize(code);
    if (cd === q) return 0;
    if (c.form === 'weak-board' && cd === 'BK' + q) return 0;   // 4 位数字 → 补 BK 视作精确
    if (nm === q) return 0;
    if (nm.indexOf(q) === 0) return 1;
    if (nm.indexOf(q) > 0) return 2;
    if (cd.indexOf(q) >= 0) return 2;
    if (ind && normalize(ind).indexOf(q) >= 0) return 3;        // L3：折叠为"按行业查看"
    return 9;
  }

  // ===== §4 请求编排 =====
  function getJSON(url, signal) {
    return fetch(url, { signal: signal, headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
  }

  function runSearch(raw) {
    var c = classify(raw);
    lastQuery = c.n;
    cur = { c: c, acc: {} };

    // 不触发查询的三种形态：只渲染提示行（方案 §7.1 S6）
    if (c.form === 'empty') { render(); return; }
    if (c.form === 'too-short') { render(); return; }
    if (c.form === 'too-long') { render(); return; }

    // 分流决定查哪几路：6 位码只查股票、BK 码只查板块（方案 §3.2 "不再走文本模糊"）
    c.requestStocks = (c.form !== 'board-code');
    c.requestBoards = (c.form === 'board-code' || c.form === 'weak-board' || c.form === 'text');

    // 单飞：取消上一批在途请求，再递增序号（旧响应归来时 seq 已变 → 丢弃）
    if (ctrl) { try { ctrl.abort(); } catch (e) { /* 忽略 */ } }
    ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var signal = ctrl ? ctrl.signal : undefined;
    var mySeq = ++seq;

    var acc = cur.acc;
    acc.stocks = null;
    acc.boards = { industry: null, concept: null };
    var pending = 0;

    function settled() {
      pending--;
      if (mySeq !== seq) return;        // 过期批次（已被新请求 abort/取代），丢弃
      if (pending <= 0) clearSlow();
      render();                         // 谁先回谁先渲染（方案 §4.3-1，不做 Promise.all 强等待）
    }

    if (c.requestStocks) {
      pending++;
      getJSON(API_STOCKS + '?' + buildQuery({
        q: c.n, page: 1, pageSize: MAX_STOCKS, sort: 'mcap', order: 'desc'
      }), signal).then(function (d) {
        acc.stocks = {
          items: d.stocks || [],
          total: d.total || 0,
          status: d.data_status || 'ok',
          updated: d.source_updated || d.updated || null,
          // status=unavailable 时即使 HTTP 200 也按"取数失败"处理
          failed: d.data_status === 'unavailable'
        };
      }).catch(function (e) {
        if (mySeq !== seq) return;
        acc.stocks = { error: (e && e.name === 'AbortError') ? 'abort' : 'error' };
      }).then(settled);
    }

    if (c.requestBoards) {
      ['industry', 'concept'].forEach(function (k) {
        pending++;
        getJSON(API_BOARDS + '?' + buildQuery({
          type: k, q: c.n, page: 1, pageSize: BOARD_PER_KIND, sort: 'mcap', order: 'desc'
        }), signal).then(function (d) {
          acc.boards[k] = {
            items: d.boards || [],
            total: d.total || 0,
            status: d.data_status || 'ok',
            updated: d.data_updated || null,
            failed: d.data_status === 'unavailable'
          };
        }).catch(function (e) {
          if (mySeq !== seq) return;
          acc.boards[k] = { error: (e && e.name === 'AbortError') ? 'abort' : 'error' };
        }).then(settled);
      });
    }

    // 先出骨架屏（S3），再启动慢响应提示
    slowNotice = false;
    render();
    openPanel();
    if (pending > 0) startSlow(mySeq);
  }

  function startSlow(mySeq) {
    clearSlow();
    slowTimer = setTimeout(function () {
      if (mySeq !== seq) return;      // 已换批次
      slowNotice = true;
      render();
    }, SLOW_HINT_MS);
  }

  function clearSlow() {
    if (slowTimer) { clearTimeout(slowTimer); slowTimer = null; }
  }

  function schedule(raw) {
    if (debTimer) { clearTimeout(debTimer); debTimer = null; }
    var c = classify(raw);
    // 过短 / 超长 / 空：不发请求、不等防抖，立即反馈（方案 §7.1 S6）
    if (!c.n || c.form === 'too-short' || c.form === 'too-long') {
      lastQuery = c.n;
      cur = { c: c, acc: {} };
      render();
      openPanel();
      return;
    }
    debTimer = setTimeout(function () {
      debTimer = null;
      runSearch(raw);
    }, DEBOUNCE_MS);
  }

  // ===== §5 结果归集 =====
  function collectStocks(acc, c) {
    var out = { hits: [], industry: null, total: 0, status: null, updated: null };
    var s = acc.stocks;
    if (!s || s.error || s.failed) return out;
    out.total = s.total || 0;
    out.status = s.status;
    out.updated = s.updated;
    (s.items || []).forEach(function (r) {
      var lv = levelOf(r.name, r.code, r.ind, c);
      if (lv <= 2) out.hits.push({ row: r, lv: lv });
      else if (lv === 3 && !out.industry) out.industry = r.ind;
    });
    out.hits.sort(function (a, b) {
      if (a.lv !== b.lv) return a.lv - b.lv;
      return (b.row.mcap || 0) - (a.row.mcap || 0);      // 档内市值降序（与列表页心智一致）
    });
    return out;
  }

  function collectBoards(acc, c) {
    var res = { industry: [], concept: [], total: 0, status: null, updated: null, failedKinds: [] };
    ['industry', 'concept'].forEach(function (k) {
      var b = acc.boards[k];
      if (!b || b.error || b.failed) {
        if (b) res.failedKinds.push(k);
        return;
      }
      if (res.status === null) res.status = b.status;
      if (!res.updated && b.updated) res.updated = b.updated;
      res.total += (b.total || 0);
      var arr = (b.items || []).map(function (r) {
        return { row: r, kind: k, lv: levelOf(r.name, r.code, null, c) };
      });
      // 档位 → 代码升序：同一天内下拉顺序稳定，避免顺序随行情跳动导致 ↑↓ 选错
      arr.sort(function (x, y) {
        if (x.lv !== y.lv) return x.lv - y.lv;
        var cx = String(x.row.code), cy = String(y.row.code);
        return cx < cy ? -1 : (cx > cy ? 1 : 0);
      });
      res[k] = arr;
    });
    return res;
  }

  // ===== §7 渲染 =====
  function noteHtml(html) {
    return '<div class="gs-note">' + html + '</div>';
  }

  function actionsHtml(btns) {
    return '<div class="gs-note-actions">' + btns.map(function (b) {
      return '<button type="button" class="gs-btn" data-act="' + b.act + '">' + esc(b.label) + '</button>';
    }).join('') + '</div>';
  }

  function skelHtml() {
    return '<div class="gs-skel"></div><div class="gs-skel"></div><div class="gs-skel"></div>';
  }

  function groupWrap(title, inner) {
    return '<div class="gs-group" role="group" aria-label="' + esc(title) + '">'
      + '<div class="gs-group-head" aria-hidden="true"><span>' + esc(title) + '</span></div>'
      + inner + '</div>';
  }

  /** 单个可选项（既是 DOM 也是键盘导航项），idx 与 options 一一对应 */
  function optionHtml(o, nameHtml, subHtml, sideHtml) {
    var idx = options.length;
    options.push(o);
    return '<div class="gs-item" role="option" id="gs-opt-' + idx + '" data-idx="' + idx + '" aria-selected="false">'
      + '<div class="gs-item-main"><div class="gs-item-name">' + nameHtml + '</div>'
      + (subHtml ? '<div class="gs-item-sub">' + subHtml + '</div>' : '')
      + '</div>' + (sideHtml ? '<div class="gs-item-side">' + sideHtml + '</div>' : '')
      + '</div>';
  }

  function moreHtml(o, label) {
    var idx = options.length;
    options.push(o);
    return '<button type="button" class="gs-more" role="option" id="gs-opt-' + idx + '" data-idx="' + idx + '" aria-selected="false">'
      + esc(label) + '</button>';
  }

  function chgSide(rec) {
    var cls = rec.dir === 'up' ? 'up' : (rec.dir === 'down' ? 'down' : 'flat');
    return '<span class="gs-val">' + esc(rec.price || '--') + '</span>'
      + '<span class="gs-chg ' + cls + '">' + esc(rec.chg || '--') + '</span>';
  }

  function render() {
    var c = cur.c, acc = cur.acc || {};
    if (!c) return;
    options = [];
    active = -1;

    // S1 聚焦提示 / S6 需澄清
    if (c.form === 'empty') {
      panel.innerHTML = noteHtml('输入<b>股票名称、代码</b>，或<b>板块名称、代码</b>（如 600519、力源信息、半导体、BK1325）');
      announce('');
      return;
    }
    if (c.form === 'too-short') {
      panel.innerHTML = noteHtml('请至少输入 ' + MIN_DIGITS + ' 位数字，或输入完整名称');
      announce('输入过短');
      return;
    }
    if (c.form === 'too-long') {
      panel.innerHTML = noteHtml('关键词过长，请精简后重试');
      announce('关键词过长');
      return;
    }

    var sg = collectStocks(acc, c);
    var bg = collectBoards(acc, c);
    var stockState = !c.requestStocks ? 'absent'
      : (!acc.stocks ? 'loading'
        : ((acc.stocks.error || acc.stocks.failed) ? 'failed' : 'ok'));
    var boardKinds = ['industry', 'concept'];
    var landed = boardKinds.filter(function (k) { return !!acc.boards[k]; });
    var boardState = !c.requestBoards ? 'absent'
      : (landed.length < boardKinds.length ? 'loading'
        : (bg.failedKinds.length === boardKinds.length ? 'failed' : 'ok'));
    var boardPartial = (boardState === 'ok' && bg.failedKinds.length > 0);

    // 两侧全失败 → S7 整体降级
    if ((stockState === 'failed' || stockState === 'absent')
      && (boardState === 'failed' || boardState === 'absent')
      && (stockState === 'failed' || boardState === 'failed')) {
      panel.innerHTML = noteHtml('搜索服务暂时不可用，请稍后重试')
        + actionsHtml([{ act: 'retry', label: '重试' }]);
      announce('搜索服务暂时不可用');
      return;
    }

    var boardAll = bg.industry.concat(bg.concept);
    var boardLv0 = boardAll.some(function (x) { return x.lv === 0; });
    // 默认分组顺序 股票 → 板块 → 按行业查看个股；
    // 例外：4 位弱板块且板块精确命中时，板块置顶（方案 §3.2）
    var order = (c.form === 'weak-board' && boardLv0) ? ['boards', 'stocks'] : ['stocks', 'boards'];
    var parts = [];

    function boardsBlock() {
      if (boardState === 'absent') return '';
      if (boardState === 'failed') {
        // 与 S5「没有这个板块」严格区分（方案 §4.3 / §7.1）
        return groupWrap('板块', noteHtml('板块结果暂时不可用（数据源故障，<strong>不代表没有这个板块</strong>）')
          + actionsHtml([{ act: 'goto-sector', label: '去板块页查看 →' }]));
      }
      if (boardState === 'loading') return groupWrap('板块', skelHtml());
      var shown = boardAll.slice(0, MAX_BOARDS);
      if (!shown.length && !boardPartial) return '';
      var html = shown.map(function (x) {
        var badge = '<span class="gs-badge ' + x.kind + '">' + (x.kind === 'industry' ? '行业' : '概念') + '</span>';
        var sub = '板块代码 ' + esc(x.row.code) + ' · 领涨 ' + esc(x.row.leader || '--');
        return optionHtml(
          { type: 'board', code: String(x.row.code) },
          esc(x.row.name) + badge, sub, chgSide(x.row));
      }).join('');
      if (shown.length && bg.total > shown.length) {
        var kind = (bg.concept.length > bg.industry.length) ? 'concept' : 'industry';
        html += moreHtml({ type: 'more-boards', q: lastQuery, kind: kind }, '查看全部 ' + bg.total + ' 个板块 →');
      }
      if (!html && boardPartial) html = noteHtml('部分板块数据源不可用，结果可能不完整');
      return groupWrap('板块', html);
    }

    function stocksBlock() {
      if (stockState === 'absent') return '';
      if (stockState === 'failed') {
        return groupWrap('股票', noteHtml('个股结果暂时不可用（数据源故障，<strong>不代表没有这只股票</strong>）')
          + actionsHtml([{ act: 'goto-stocks-all', label: '去个股页查看 →' }]));
      }
      if (stockState === 'loading') return groupWrap('股票', skelHtml());
      var shown = sg.hits.slice(0, MAX_STOCKS);
      // 空分组不占位：个股全部因"仅行业字段命中"被折叠时，这里不留孤立的组标题
      if (!shown.length) return '';
      var html = shown.map(function (x) {
        var sub = '股票代码 ' + esc(x.row.code) + (x.row.ind ? ' · ' + esc(x.row.ind) : '');
        return optionHtml(
          { type: 'stock', code: String(x.row.code) },
          esc(x.row.name), sub, chgSide(x.row));
      }).join('');
      // 仅在确有名称/代码命中时才给"查看全部"；全被折叠进行业入口时不给（避免只剩一行按钮）
      if (shown.length && sg.total > shown.length) {
        html += moreHtml({ type: 'more-stocks', q: lastQuery }, '查看全部 ' + sg.total + ' 只股票 →');
      }
      return groupWrap('股票', html);
    }

    function foldBlock() {
      // §3.5：仅靠行业字段命中的个股不铺进列表（否则"输入半导体"会被数百只个股淹没），
      // 折叠成一条行业入口，把"输入行业名"从噪声变成导航。
      if (stockState !== 'ok' || c.form !== 'text' || !sg.industry) return '';
      return '<div class="gs-group" role="group" aria-label="按行业查看个股">'
        + '<div class="gs-group-head" aria-hidden="true"><span>按行业查看个股</span></div>'
        + optionHtml({ type: 'fold-industry', q: String(sg.industry) },
          '行业：' + esc(sg.industry), '跳转板块页查看该行业下的全部个股', '')
        + '</div>';
    }

    order.forEach(function (k) {
      parts.push(k === 'boards' ? boardsBlock() : stocksBlock());
    });
    parts.push(foldBlock());

    var totalHits = sg.hits.length + boardAll.length;
    var stillLoading = (stockState === 'loading' || boardState === 'loading');

    // S5 无结果：取数成功但命中为 0（与 S7 严格区分）
    if (!stillLoading && totalHits === 0 && !sg.industry) {
      var msg;
      if (c.form === 'stock-code') msg = '未找到代码 ' + esc(c.n) + ' 对应的股票';
      else if (c.form === 'board-code') msg = '未找到板块 ' + esc(c.n);
      else if (c.form === 'weak-board') msg = '未找到匹配「' + esc(c.n) + '」的股票或板块';
      else msg = '未找到匹配「' + esc(c.n) + '」的股票或板块';
      panel.innerHTML = noteHtml(msg)
        + actionsHtml([{ act: 'goto-stocks-all', label: '在全部个股中搜索 →' }]);
      announce(msg.replace(/<[^>]+>/g, ''));
      return;
    }

    if (!parts.join('')) {
      panel.innerHTML = noteHtml(slowNotice ? '数据源响应较慢，仍在加载…' : '加载中…');
      return;
    }

    // 底部提示条：键盘操作 + 非实时数据来源标注（用数据自身时间，不用响应时间）
    var src = [];
    if (sg.status && sg.status !== 'ok' && sg.updated) src.push('个股数据 ' + sg.updated);
    if (bg.status && bg.status !== 'ok' && bg.updated) src.push('板块数据 ' + bg.updated);
    var hint = '<div class="gs-hint"><span>↑↓ 切换 · Enter 打开 · Esc 关闭</span>'
      + '<span class="gs-source">' + (src.length ? esc(src.join(' · ')) : (slowNotice ? '数据源响应较慢…' : '')) + '</span></div>';

    panel.innerHTML = parts.join('') + (stillLoading && slowNotice ? noteHtml('部分结果仍在加载…') : '') + hint;
    announce('找到 ' + sg.hits.length + ' 只股票、' + boardAll.length + ' 个板块');
  }

  // ===== §7 面板开关 =====
  function openPanel() {
    if (panel.hidden) panel.hidden = false;
    if (isOpen) return;
    isOpen = true;
    container.classList.add('open');
    input.setAttribute('aria-expanded', 'true');
  }

  function closePanel() {
    if (panel.hidden) return;
    panel.hidden = true;
    isOpen = false;
    container.classList.remove('open');
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    active = -1;
  }

  function announce(txt) {
    if (live) live.textContent = txt || '';
  }

  // ===== §6 跳转与校验 =====
  function prefetchDetail(code) {
    if (!code || _pfCode === code) return;
    _pfCode = code;
    // 与 stock.html 的悬停预取一致：命中服务端缓存后详情页近乎秒开
    fetch('/api/stock?code=' + encodeURIComponent(code)).catch(function () {});
    fetch('/api/kline?code=' + encodeURIComponent(code) + '&klt=101&n=600').catch(function () {});
  }

  function openOption(o) {
    if (!o) return false;
    if (o.type === 'stock') {
      // 必须来自结果集且为 6 位数字；否则宁可不跳转（stock_detail.html 缺省会落到 600519）
      if (!/^\d{6}$/.test(o.code || '')) return false;
      prefetchDetail(o.code);
      location.href = 'stock_detail.html?code=' + encodeURIComponent(o.code);
      return true;
    }
    if (o.type === 'board') {
      if (!/^BK\d{4}$/.test(o.code || '')) return false;
      location.href = 'sector_detail.html?code=' + encodeURIComponent(o.code);
      return true;
    }
    if (o.type === 'fold-industry') {
      location.href = 'sector.html?q=' + encodeURIComponent(o.q) + '&type=industry';
      return true;
    }
    if (o.type === 'more-stocks') {
      location.href = 'stock.html?q=' + encodeURIComponent(o.q);
      return true;
    }
    if (o.type === 'more-boards') {
      location.href = 'sector.html?q=' + encodeURIComponent(o.q) + '&type=' + encodeURIComponent(o.kind);
      return true;
    }
    return false;
  }

  function handleAction(act) {
    if (act === 'retry') { runSearch(input.value); input.focus(); return; }
    if (act === 'goto-stocks-all') {
      location.href = 'stock.html' + (lastQuery ? '?q=' + encodeURIComponent(lastQuery) : '');
      return;
    }
    if (act === 'goto-sector') { location.href = 'sector.html'; }
  }

  // ===== 高亮同步 =====
  function nodes() {
    return panel.querySelectorAll('.gs-item, .gs-more');
  }

  function syncActive() {
    var list = nodes();
    for (var i = 0; i < list.length; i++) {
      var on = (i === active);
      list[i].classList.toggle('on', on);
      list[i].setAttribute('aria-selected', on ? 'true' : 'false');
    }
    var el = list[active];
    if (el) {
      el.scrollIntoView({ block: 'nearest' });
      input.setAttribute('aria-activedescendant', el.id);
    } else {
      input.removeAttribute('aria-activedescendant');
    }
  }

  function move(delta) {
    if (!options.length) return;
    if (active < 0) active = (delta > 0) ? 0 : options.length - 1;
    else active = (active + delta + options.length) % options.length;
    syncActive();
  }

  // ===== 首焦预热：仅一次，且不在首页加载时打板块接口（方案 §4.3-4）=====
  function warm() {
    if (warmDone) return;
    warmDone = true;
    ['industry', 'concept'].forEach(function (k) {
      fetch(API_BOARDS + '?' + buildQuery({ type: k, page: 1, pageSize: 1 })).catch(function () {});
    });
  }

  // ===== 事件绑定 =====
  function toggleClear() {
    if (clearBtn) clearBtn.hidden = !input.value;
  }

  input.addEventListener('compositionstart', function () { composing = true; });

  input.addEventListener('compositionend', function () {
    composing = false;
    toggleClear();
    schedule(input.value);      // 拼音上屏后才发请求
  });

  input.addEventListener('input', function () {
    toggleClear();
    if (composing) return;      // 输入法组合中：不触发查询
    schedule(input.value);
  });

  input.addEventListener('focus', function () {
    warm();
    openPanel();
    if (!input.value.trim()) {
      cur = { c: classify(''), acc: {} };
      render();
    } else if (!options.length && !debTimer) {
      schedule(input.value);
    }
  });

  input.addEventListener('blur', function () {
    // 延迟关闭，给面板内点击留出时间（面板 mousedown 已 preventDefault 保焦点）
    setTimeout(function () {
      if (document.activeElement === input) return;
      closePanel();
    }, 120);
  });

  input.addEventListener('keydown', function (e) {
    var key = e.key;
    if (key === 'ArrowDown') { e.preventDefault(); openPanel(); move(1); return; }
    if (key === 'ArrowUp') { e.preventDefault(); move(-1); return; }
    if (key === 'Enter') {
      if (active >= 0 && options[active]) { e.preventDefault(); openOption(options[active]); return; }
      if (options.length) { e.preventDefault(); openOption(options[0]); }
      return;   // 无结果/加载中：不跳转，停在原地给提示（方案 §5.3）
    }
    if (key === 'Escape') {
      // 两段式：先清空，再关闭
      if (input.value) { input.value = ''; toggleClear(); schedule(''); }
      else closePanel();
      return;
    }
    if (key === 'Tab') closePanel();
  });

  if (clearBtn) {
    clearBtn.addEventListener('click', function () {
      input.value = '';
      toggleClear();
      schedule('');
      input.focus();
    });
  }

  if (cancelBtn) {
    cancelBtn.addEventListener('click', function () {
      input.value = '';
      toggleClear();
      closePanel();
      input.blur();
    });
  }

  // 保持焦点：面板内按下鼠标不夺焦（否则 blur 会先关掉面板）
  panel.addEventListener('mousedown', function (e) { e.preventDefault(); });

  panel.addEventListener('click', function (e) {
    var item = closestEl(e.target, '.gs-item, .gs-more');
    if (item && item.getAttribute('data-idx') !== null) {
      openOption(options[Number(item.getAttribute('data-idx'))]);
      return;
    }
    var act = closestEl(e.target, '[data-act]');
    if (act) handleAction(act.getAttribute('data-act'));
  });

  panel.addEventListener('mousemove', function (e) {
    var item = closestEl(e.target, '.gs-item, .gs-more');
    if (!item) return;
    var idx = Number(item.getAttribute('data-idx'));
    if (idx !== active && !isNaN(idx)) { active = idx; syncActive(); }
  });

  // 点击面板之外 → 收起
  document.addEventListener('click', function (e) {
    if (!container.contains(e.target)) closePanel();
  });

  toggleClear();
})();
