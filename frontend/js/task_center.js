/* ============================================================
 * 报告任务中心（F-04）
 * ------------------------------------------------------------
 * 用途：报告生成是**长任务**（实测单份报告数十秒到数分钟），而它此前的进度只活在
 * 发起它的那一页的 `setInterval` 里 —— 切页即失联，用户看不到"我刚才那份跑完了没"。
 * 本脚本把「跑中任务」提升为**全站可见**的一块 UI。
 *
 * 设计要点（每条都对应本项目已发生过的一次事故）：
 *
 * 1. **数据来自 `GET /api/tasks`（服务端按 user_id 硬过滤）**，不在前端过滤。
 *    前端过滤 = 先把别人的任务下发到浏览器再藏起来，等于泄露。
 *
 * 2. **「进行中」与「最近完成」是同一次请求的两段视图**，不是两个接口。
 *    F-04 管跑中（内存瞬态）、F-06 管已生成产物（磁盘持久）；任务完成时服务端
 *    先落盘再改状态，所以「SUCCESS 出现在这里」⟹「报告库已能查到」。这就是
 *    「生成完成后自动进入报告库」的实现，**不需要**任何搬运动作。
 *
 * 3. **打开报告一律用服务端下发的 `view_url`（= `/api/report/view/{report_id}`），
 *    绝不拼 `/api/report/{task_id}/html`**。后者是内存凭证，8002 一重启就失效 ——
 *    那正是 F-06 交付时修掉的缺陷，不能在任务中心里重新引入。
 *
 * 4. **空态与不可用态必须分开显示**：请求失败时不允许说"没有正在生成的报告"，
 *    必须说"任务列表加载失败"。本项目在行情侧已两次栽在"失败伪装成不存在"上。
 *
 * 5. **类型中文名 / 主体链接由服务端下发**（`report_type_label` / `subject_url`，
 *    源头是 `report_index.REPORT_TYPES`）。前端不自建类型映射表，否则将来新增
 *    报告类型时会出现"报告库认得它、任务中心不认识"。
 *
 * 6. **删除只对终态开放，而且删的是「任务记录」不是「报告」**：失败任务不落盘，只活在
 *    任务中心里（`TaskManager._tasks`），跑几次参数错误的生成（例如不存在的板块）
 *    就会把「需要处理」区堆满同一行字 —— 删除是它们唯一的出口。三条边界：
 *    ① **活跃任务绝不渲染删除按钮**，服务端另有 `skipped_active` 拒删 —— 删掉运行中的
 *       任务会让该 worker 的 `update()` 静默失效、进度条**永久卡死**（与「容量淘汰只动
 *       终态」是同一条约束的两个入口，只守一个等于没守）；
 *    ② 入口**只放在失败 / 定位不到产物的行**上：SUCCESS 行的产物仍在报告库（报告库扫
 *       磁盘，不看 `_tasks`），把删除放在「已完成」行会让人以为在删报告；
 *    ③ 删除成功后必须**按服务端回执**（`deleted` / `skipped_active`）更新本地列表，
 *       不能无条件把点过的 id 都抹掉 —— 否则"服务端拒删"会表现为"本地已删、刷新又回来"。
 *
 * 两种挂载形态（同一份渲染逻辑）：
 *   - 页面里存在 `#taskCenter` 容器 → **内联区块**（报告库页用它，即"报告任务中心"本尊）；
 *   - 否则 → 右下角悬浮胶囊 + 抽屉面板（其余页面用它，解决"切页即失联"）。
 * ============================================================ */
(function () {
  'use strict';

  var API = '/api/tasks';
  var API_REMOVE = '/api/tasks/remove';   // 删除用 POST 表达：8000 侧无 do_DELETE（与 F-03 同路线）
  var POLL_ACTIVE = 1500;   // 有任务在跑：跟详情页轮询同频
  var POLL_IDLE = 15000;    // 没有任务在跑：只为了能发现"别处提交/完成"的任务
  var DONE_KEEP_MS = 60000; // 任务完成后胶囊再停留一会儿，给用户一个入口
  var TOAST_MS = 9000;
  var RECENT_MAX = 6;       // 「最近完成」区最多展示条数

  var ACTIVE = { PENDING: 1, RUNNING: 1 };

  var state = {
    items: [],
    active: 0,
    err: '',            // 非空 = 不可用态（**不得**渲染成空态）
    loading: false,
    busy: {},           // task_id -> 1：删除请求进行中（按钮置灰，防重复点击）
    doneAt: 0,          // 最近一次任务完成时刻（控制胶囊停留）
    seen: {},           // task_id -> 上次状态（用于检测迁移）
    open: false
  };

  var elFab = null, elMask = null, elPanel = null, elBody = null,
      elInline = null, elToast = null, timer = null,
      bound = {};         // 容器级委托只绑一次的标记（见 bindActions）

  /* ===== 样式（自注入：本脚本要在样式体系各异的多个页面复用） ===== */
  var CSS = [
    '.tc-fab{position:fixed;right:24px;bottom:24px;z-index:9000;display:inline-flex;align-items:center;gap:10px;',
    'height:52px;padding:0 20px;border:none;border-radius:26px;background:#0071E3;color:#fff;font-size:14px;font-weight:600;',
    'font-family:inherit;cursor:pointer;box-shadow:0 8px 24px rgba(0,113,227,.32);transition:transform .15s ease,background .15s ease;}',
    '.tc-fab:hover{background:#0062C4;transform:translateY(-1px);}',
    '.tc-fab[hidden]{display:none;}',
    '.tc-fab.done{background:#0E8A6E;box-shadow:0 8px 24px rgba(14,138,110,.30);}',
    '.tc-fab.done:hover{background:#0B7359;}',
    '.tc-fab-ring{position:relative;width:22px;height:22px;flex:0 0 auto;}',
    '.tc-fab-ring svg{transform:rotate(-90deg);display:block;}',
    '.tc-fab-ring .bg{stroke:rgba(255,255,255,.32);}',
    '.tc-fab-ring .fg{stroke:#fff;transition:stroke-dashoffset .4s ease;}',
    '.tc-fab-pct{font-variant-numeric:tabular-nums;opacity:.92;font-weight:500;}',

    '.tc-mask{position:fixed;inset:0;background:rgba(0,0,0,.28);z-index:9001;}',
    '.tc-mask[hidden]{display:none;}',
    '.tc-panel{position:fixed;right:0;top:0;bottom:0;width:min(440px,100vw);background:#fff;z-index:9002;',
    'display:flex;flex-direction:column;box-shadow:-12px 0 40px rgba(0,0,0,.16);}',
    '.tc-panel[hidden]{display:none;}',
    '.tc-head{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;padding:20px 22px;border-bottom:1px solid #E2E2E7;}',
    '.tc-head .t{font-size:18px;font-weight:700;color:#1D1D1F;}',
    '.tc-head .x{border:none;background:transparent;font-size:26px;line-height:1;color:#86868B;cursor:pointer;font-family:inherit;padding:0 4px;}',
    '.tc-head .x:hover{color:#1D1D1F;}',
    '.tc-scroll{flex:1 1 auto;overflow-y:auto;padding:8px 22px 22px;}',
    '.tc-foot{flex:0 0 auto;border-top:1px solid #E2E2E7;padding:14px 22px;}',
    '.tc-foot a{color:#0071E3;font-size:14px;font-weight:600;text-decoration:none;}',
    '.tc-foot a:hover{text-decoration:underline;}',

    '.tc-sec{margin-top:18px;}',
    '.tc-sec-h{display:flex;align-items:baseline;gap:8px;font-size:13px;font-weight:700;color:#6E6E73;letter-spacing:.4px;}',
    '.tc-sec-h .n{font-weight:600;color:#86868B;}',
    '.tc-list{display:flex;flex-direction:column;gap:10px;margin-top:10px;}',

    '.tc-row{background:#F5F5F7;border-radius:14px;padding:14px 16px;}',
    '.tc-r1{display:flex;align-items:center;gap:10px;}',
    '.tc-badge{flex:0 0 auto;font-size:11px;font-weight:700;padding:3px 9px;border-radius:9px;background:#EFEFF2;color:#6E6E73;white-space:nowrap;}',
    '.tc-badge.b-fundamental{background:#EAF2FC;color:#0071E3;}',
    '.tc-badge.b-overview{background:#E6F4F0;color:#0E8A6E;}',
    '.tc-badge.b-industry{background:#F1ECFE;color:#7A5AF8;}',
    '.tc-badge.b-board{background:#FFF4E0;color:#B26A00;}',
    '.tc-subj{flex:1 1 auto;min-width:0;font-size:15px;font-weight:700;color:#1D1D1F;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.tc-subj .c{font-size:12px;color:#86868B;font-weight:500;margin-left:6px;}',
    '.tc-st{flex:0 0 auto;font-size:12px;font-weight:700;color:#86868B;font-variant-numeric:tabular-nums;}',
    '.tc-st.ok{color:#0E8A6E;}',
    '.tc-st.bad{color:#C0392B;}',

    '.tc-bar{height:6px;border-radius:3px;background:#E2E2E7;overflow:hidden;margin-top:10px;}',
    '.tc-bar i{display:block;height:100%;border-radius:3px;background:#0071E3;transition:width .4s ease;}',
    '.tc-r2{display:flex;align-items:center;gap:10px;margin-top:8px;font-size:13px;color:#86868B;}',
    '.tc-r2 .stage{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.tc-r2 .pct{font-weight:700;color:#0071E3;font-variant-numeric:tabular-nums;}',
    '.tc-err{margin-top:8px;font-size:13px;color:#C0392B;line-height:1.6;word-break:break-word;}',
    '.tc-acts{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap;}',
    '.tc-acts a{display:inline-flex;align-items:center;height:34px;padding:0 14px;border-radius:17px;font-size:13px;font-weight:600;text-decoration:none;white-space:nowrap;}',
    '.tc-acts a.p{background:#0071E3;color:#fff;}',
    '.tc-acts a.p:hover{background:#0062C4;}',
    '.tc-acts a.g{border:1px solid #E2E2E7;background:#fff;color:#1D1D1F;}',
    '.tc-acts a.g:hover{border-color:#0071E3;color:#0071E3;}',

    '.tc-empty{padding:34px 10px;text-align:center;color:#86868B;font-size:14px;line-height:1.9;}',
    '.tc-empty .big{display:block;font-size:15px;font-weight:700;color:#1D1D1F;margin-bottom:6px;}',
    '.tc-empty.bad .big{color:#C0392B;}',
    '.tc-empty button{margin-top:14px;height:38px;padding:0 18px;border-radius:19px;border:1px solid #E2E2E7;background:#fff;',
    'font-size:14px;font-weight:600;color:#1D1D1F;cursor:pointer;font-family:inherit;}',
    '.tc-empty button:hover{border-color:#0071E3;color:#0071E3;}',

    /* 删除（仅失败 / 异常行）：克制的中性按钮，hover 才转成警示色 ——
       常驻红色会让一整列失败记录看起来比实际更严重。 */
    '.tc-del{flex:0 0 auto;height:26px;padding:0 12px;border-radius:13px;border:1px solid #E2E2E7;background:#fff;',
    'color:#86868B;font-size:12px;font-weight:600;font-family:inherit;cursor:pointer;',
    'transition:border-color .15s ease,color .15s ease,background .15s ease;}',
    '.tc-del:hover{border-color:#C0392B;color:#C0392B;background:#FFF5F3;}',
    '.tc-del[disabled]{opacity:.55;cursor:default;}',
    '.tc-del[disabled]:hover{border-color:#E2E2E7;color:#86868B;background:#fff;}',
    '.tc-clr{margin-left:auto;border:none;background:transparent;color:#0071E3;font-size:12px;',
    'font-weight:600;font-family:inherit;cursor:pointer;padding:2px 0;}',
    '.tc-clr:hover{text-decoration:underline;}',
    '.tc-clr[disabled]{opacity:.55;cursor:default;text-decoration:none;}',

    '.tc-toast-wrap{position:fixed;right:24px;bottom:90px;z-index:9003;display:flex;flex-direction:column;gap:10px;align-items:flex-end;}',
    '.tc-toast{max-width:380px;background:#1D1D1F;color:#fff;border-radius:14px;padding:14px 18px;font-size:14px;line-height:1.6;',
    'box-shadow:0 10px 30px rgba(0,0,0,.24);animation:tcIn .22s ease;}',
    '.tc-toast.bad{background:#8E2A20;}',
    '.tc-toast a{color:#7CB8FF;font-weight:700;text-decoration:none;margin-left:8px;}',
    '.tc-toast a:hover{text-decoration:underline;}',
    '@keyframes tcIn{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}',

    /* 内联形态（报告库页）：外框由宿主页给出，这里只管内容排版。
       空态收紧为一行提示 —— 报告库页绝大多数时候没有跑中任务，
       一个 34px 内边距的大空块会把下方的报告列表挤到屏幕外。 */
    '.tc-inline .tc-sec{margin-top:0;}',
    '.tc-inline .tc-scroll{padding:0;overflow:visible;}',
    '.tc-inline .tc-row{background:#F5F5F7;}',
    '.tc-inline .tc-empty{padding:16px 0;text-align:left;}',
    '.tc-inline .tc-sec-h{margin-top:4px;}',

    '@media (max-width:600px){',
    '.tc-fab{right:16px;bottom:16px;height:48px;padding:0 16px;font-size:13px;}',
    '.tc-toast-wrap{right:16px;left:16px;bottom:80px;align-items:stretch;}',
    '.tc-toast{max-width:none;}',
    '}'
  ].join('');

  function injectStyle() {
    if (document.getElementById('tc-css')) return;
    var s = document.createElement('style');
    s.id = 'tc-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  /* ===== 工具 ===== */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  function isActive(it) { return !!ACTIVE[it && it.status]; }

  /** 终态时间显示：今天只给 HH:MM，跨天补 MM-DD（避免"10:52"读不出是哪天）。 */
  function fmtWhen(iso) {
    var t = Date.parse(iso || '');
    if (isNaN(t)) return '';
    var d = new Date(t), now = new Date();
    var hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
    var sameDay = d.getFullYear() === now.getFullYear()
      && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
    return sameDay ? hm : (pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + hm);
  }

  /** 已用时长（进行中任务），秒级起步，到分钟/小时。 */
  function fmtElapsed(iso) {
    var t0 = Date.parse(iso || '');
    if (isNaN(t0)) return '';
    var s = Math.max(0, Math.round((Date.now() - t0) / 1000));
    if (s < 60) return s + ' 秒';
    var m = Math.floor(s / 60);
    if (m < 60) return m + ' 分' + (s % 60 ? (s % 60) + ' 秒' : '');
    return Math.floor(m / 60) + ' 小时' + (m % 60) + ' 分';
  }

  /** 标的名与代号。**名称与代号相同时不重复显示**：跑中任务往往还没拿到中文名
   *  （`name` 由 worker 在 SUCCESS 时回填），若直接 "名称 + 代码" 拼两份，
   *  界面上会出现「600519 600519」。拿不到名称就只显示代码，**绝不编造公司名**。 */
  function subjectBits(it) {
    var code = it.bk || it.code || '';
    var name = it.name || it.bk_name || '';
    if (!name || name === code) return { main: code || '—', sub: '' };
    return { main: name, sub: code };
  }

  /** 删除按钮。**只在异常终态行调用** —— 调用点本身就是护栏：活跃行压根没有这段 HTML，
   *  所以"误删运行中任务"在 UI 层不可达（服务端另有 `skipped_active` 兜底）。 */
  function delBtn(it) {
    if (!it || !it.task_id) return '';
    var busy = !!state.busy[it.task_id];
    return '<button type="button" class="tc-del" data-tc-del="' + esc(it.task_id) + '"' +
      (busy ? ' disabled' : '') + '>' + (busy ? '删除中…' : '删除') + '</button>';
  }

  /** 「需要处理」区里可删除的 task_id（= split().abnormal，不含活跃任务）。 */
  function abnormalIds() {
    return split().abnormal.map(function (it) { return it.task_id; })
      .filter(function (id) { return !!id; });
  }

  /* ===== 渲染：一份 HTML 供内联与抽屉共用 ===== */
  function rowHtml(it) {
    var b = subjectBits(it);
    var typeCls = 'b-' + String(it.report_type || '').replace(/[^A-Za-z0-9_]/g, '');
    var head = '<div class="tc-r1">' +
      '<span class="tc-badge ' + typeCls + '">' + esc(it.report_type_label || it.report_type) + '</span>' +
      '<span class="tc-subj">' + esc(b.main) +
        (b.sub ? '<span class="c">' + esc(b.sub) + '</span>' : '') + '</span>';

    if (isActive(it)) {
      var pct = Math.max(0, Math.min(100, Number(it.progress) || 0));
      head += '<span class="tc-st">' + pct + '%</span></div>' +
        '<div class="tc-bar"><i style="width:' + pct + '%"></i></div>' +
        '<div class="tc-r2"><span class="stage">' + esc(it.stage || '生成中') + '</span>' +
        '<span class="pct">已用 ' + esc(fmtElapsed(it.created_at)) + '</span></div>';
      return '<div class="tc-row">' + head + '</div>';
    }

    if (it.status === 'SUCCESS') {
      head += '<span class="tc-st ok">已完成</span></div>';
      var acts = '';
      if (it.view_url) {
        acts += '<a class="p" href="' + esc(it.view_url) + '" target="_blank" rel="noopener">打开报告</a>';
      }
      if (it.subject_url) {
        acts += '<a class="g" href="' + esc(it.subject_url) + '">' +
                (it.report_type === 'board' ? '前往板块页' : '查看个股详情') + '</a>';
      }
      var r2 = '<div class="tc-r2"><span class="stage">' +
        (it.view_url ? '已进入报告库' : '报告已生成') +
        '</span><span>' + esc(fmtWhen(it.updated_at || it.created_at)) + '</span></div>';
      if (!it.view_url) {
        // 极少数情况：任务标记成功但 report_id 反推失败（目录名异常）→ 明确告知，
        // 而不是给一个点开就 404 的链接（N-21 的教训）。这类项报告库里也没有对应入口，
        // 所以同样给删除（只清这条任务记录，已落盘的产物不受影响）。
        r2 = '<div class="tc-r2"><span class="stage">报告已生成</span><span>' +
             esc(fmtWhen(it.updated_at || it.created_at)) + '</span>' + delBtn(it) + '</div>' +
             '<div class="tc-err">这份产物未能在报告库中定位，请到报告库查看或重新生成。</div>';
      }
      return '<div class="tc-row">' + head + r2 +
             (acts ? '<div class="tc-acts">' + acts + '</div>' : '') + '</div>';
    }

    // FAILED：失败**不落盘**，报告库里永远看不到它 —— 删除是清掉这条记录的唯一出口
    head += '<span class="tc-st bad">失败</span></div>';
    return '<div class="tc-row">' + head +
      '<div class="tc-err">' + esc(it.error || '生成失败（原因未知）') + '</div>' +
      '<div class="tc-r2"><span class="stage">' + esc(fmtWhen(it.updated_at || it.created_at)) +
      '</span>' + delBtn(it) + '</div></div>';
  }

  /** 把 items 切成三段：进行中 / 异常终态 / 正常完成。
   *
   *  「异常终态」= FAILED，或「标了 SUCCESS 但 `view_url` 为空」（report_id 反推失败）。
   *  这两类**报告库里看不到**（失败不落盘、定位不到就没有列表项），所以必须由任务中心兜住。
   */
  function split() {
    var active = [], done = [], abnormal = [];
    state.items.forEach(function (it) {
      if (isActive(it)) { active.push(it); return; }
      done.push(it);
      if (it.status !== 'SUCCESS' || !it.view_url) abnormal.push(it);
    });
    return { active: active, done: done, abnormal: abnormal };
  }

  function bodyHtml(inline) {
    if (state.err) {
      // **不可用态 ≠ 空态**（本项目铁律：失败不许伪装成"没有"）
      return '<div class="tc-empty bad"><span class="big">任务列表加载失败</span>' +
        esc(state.err) + '<br>这不是「你没有正在生成的报告」。' +
        '<br><button type="button" data-tc-retry>重新加载</button></div>';
    }
    var g = split();
    if (!state.items.length) {
      return '<div class="tc-empty"><span class="big">当前没有正在生成的报告</span>' +
        '在个股详情页或板块详情页点击「AI生成…报告」后，<br>进度会出现在这里；完成后自动进入报告库。</div>';
    }

    var html = '';
    if (g.active.length) {
      html += '<div class="tc-sec"><div class="tc-sec-h">进行中<span class="n">' + g.active.length +
              '</span></div><div class="tc-list">' +
              g.active.map(rowHtml).join('') + '</div></div>';
    }

    if (inline) {
      // 内联形态（报告库页）：**已完成且已进库的报告不在这里重复列**——那是下方报告库
      // 的职责，重复列只会制造两份互相竞争的事实。这里只补报告库**看不到**的两类：
      // 失败（不落盘）与「成功但定位不到产物」。
      if (g.abnormal.length) {
        // 「全部删除」只在多于一条时出现：一条时行内的「删除」就是最短路径，
        // 再给一个区级按钮只是多一个要理解的元素。
        var clr = g.abnormal.length > 1
          ? '<button type="button" class="tc-clr" data-tc-clr>全部删除</button>' : '';
        html += '<div class="tc-sec"><div class="tc-sec-h">需要处理<span class="n">' +
                g.abnormal.length + '</span>' + clr + '</div><div class="tc-list">' +
                g.abnormal.map(rowHtml).join('') + '</div></div>';
      }
      if (!html) {
        html = '<div class="tc-empty">当前没有正在生成的报告，也没有失败的生成任务。</div>';
      }
      return html;
    }

    // 抽屉形态：进行中 + 最近完成（这里列完成项是有意义的——抽屉是"任务"视角，
    // 带失败原因与耗时，而报告库是"产物"视角）
    if (!g.active.length) {
      html += '<div class="tc-sec"><div class="tc-empty">当前没有正在生成的报告。</div></div>';
    }
    if (g.done.length) {
      html += '<div class="tc-sec"><div class="tc-sec-h">最近完成</div><div class="tc-list">' +
              g.done.slice(0, RECENT_MAX).map(rowHtml).join('') + '</div></div>';
    }
    return html;
  }

  /** 容器级事件委托：**每个容器只绑一次**。
   *
   *  为什么不用逐节点绑定：`render()` 每次都重设 `innerHTML`。逐节点绑定要求"节点被替换"
   *  这件事永远成立，一旦将来改成 append / 局部更新，监听器就会随渲染次数累积
   *  （点一次删除键发 N 个请求）。委托挂在**不会被替换的容器**上，从结构上规避这一类。
   */
  function bindActions(root, key) {
    if (!root || bound[key]) return;
    bound[key] = true;
    root.addEventListener('click', function (e) {
      var t = e.target;
      if (!t || t.nodeType !== 1 || typeof t.closest !== 'function') return;
      var del = t.closest('[data-tc-del]');
      if (del) { e.preventDefault(); removeTasks([del.getAttribute('data-tc-del')]); return; }
      var clr = t.closest('[data-tc-clr]');
      if (clr) { e.preventDefault(); removeTasks(abnormalIds()); return; }
      if (t.closest('[data-tc-retry]')) { e.preventDefault(); poll(); }
    });
  }

  /* ===== 删除任务记录（服务端只允许终态）===== */
  function removeTasks(ids) {
    ids = (ids || []).filter(function (id) { return !!id && !state.busy[id]; });
    if (!ids.length) return;
    ids.forEach(function (id) { state.busy[id] = 1; });
    render();                              // 立刻置灰，避免连点发出第二个请求
    fetch(API_REMOVE, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_ids: ids })
    })
      .then(function (r) {
        if (r.status === 401) throw new Error('会话已过期，请重新登录');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        // ⚠️ 按服务端回执更新本地列表，**不能**无条件删掉点过的 id：服务端会拒删活跃任务
        // （`skipped_active`），不看回执就会出现"本地消失了、下次轮询又回来"的幽灵现象
        // —— 比直接报错更难排查。同一条纪律见 F-06（部分成功必须逐条回报）。
        var skipped = (d && d.skipped_active) || [];
        var keep = {};
        skipped.forEach(function (id) { keep[id] = 1; });
        var gone = {};
        ids.forEach(function (id) {
          delete state.busy[id];
          if (!keep[id]) { gone[id] = 1; delete state.seen[id]; }
        });
        state.items = state.items.filter(function (it) { return !gone[it.task_id]; });
        state.active = state.items.filter(isActive).length;
        render();
        if (skipped.length) {
          toast('有 ' + skipped.length + ' 份报告正在生成中，不能删除。', true);
        } else {
          toast('已删除 ' + ((d && d.deleted) || 0) + ' 条任务记录。', false);
        }
      })
      .catch(function (e) {
        ids.forEach(function (id) { delete state.busy[id]; });
        render();
        toast('删除失败：' + esc(e && e.message ? e.message : '网络异常'), true);
      });
  }

  function renderFab() {
    if (!elFab) return;
    var active = state.active;
    var recentlyDone = state.doneAt && (Date.now() - state.doneAt < DONE_KEEP_MS);
    if (!active && !recentlyDone) { elFab.hidden = true; return; }
    elFab.hidden = false;
    var first = null;
    for (var i = 0; i < state.items.length; i++) {
      if (isActive(state.items[i])) { first = state.items[i]; break; }
    }
    if (active && first) {
      var pct = Math.max(0, Math.min(100, Number(first.progress) || 0));
      var C = 2 * Math.PI * 9;
      elFab.className = 'tc-fab';
      elFab.setAttribute('aria-label', active + ' 份报告生成中，当前 ' + pct + '%');
      elFab.innerHTML =
        '<span class="tc-fab-ring"><svg width="22" height="22" viewBox="0 0 22 22">' +
        '<circle class="bg" cx="11" cy="11" r="9" fill="none" stroke-width="3"/>' +
        '<circle class="fg" cx="11" cy="11" r="9" fill="none" stroke-width="3" ' +
        'stroke-linecap="round" stroke-dasharray="' + C.toFixed(1) + '" ' +
        'stroke-dashoffset="' + (C * (1 - pct / 100)).toFixed(1) + '"/></svg></span>' +
        '<span>' + (active > 1 ? active + ' 份报告生成中' : '报告生成中') + '</span>' +
        '<span class="tc-fab-pct">' + pct + '%</span>';
    } else {
      elFab.className = 'tc-fab done';
      elFab.setAttribute('aria-label', '有报告已完成，查看任务');
      elFab.innerHTML = '<span>报告已生成</span>';
    }
  }

  function renderHint() {
    var el = document.getElementById('tcHint');
    if (!el) return;
    var g = split();
    if (state.err) { el.textContent = '任务列表暂时读不到（见下方提示）'; return; }
    if (g.active.length) {
      el.textContent = '进行中 ' + g.active.length + ' 份 · 完成后自动进入下方报告库';
    } else if (g.abnormal.length) {
      // 提示里点出"可删除"：失败记录右侧那个按钮很克制，不说一句用户不知道有出口
      el.textContent = '没有正在生成的报告 · 有 ' + g.abnormal.length + ' 份生成失败（可删除）';
    } else {
      el.textContent = '没有正在生成的报告 · 完成后的报告会自动出现在下方';
    }
  }

  function render() {
    if (elInline) { elInline.innerHTML = bodyHtml(true); bindActions(elInline, 'inline'); }
    if (elBody) { elBody.innerHTML = bodyHtml(false); bindActions(elBody, 'body'); }
    renderHint();
    renderFab();
  }

  /* ===== 完成通知（toast + 事件） ===== */
  function toast(html, bad) {
    if (!elToast) {
      elToast = document.createElement('div');
      elToast.className = 'tc-toast-wrap';
      elToast.setAttribute('aria-live', 'polite');
      document.body.appendChild(elToast);
    }
    var d = document.createElement('div');
    d.className = 'tc-toast' + (bad ? ' bad' : '');
    d.innerHTML = html;
    elToast.appendChild(d);
    setTimeout(function () {
      if (d.parentNode) d.parentNode.removeChild(d);
    }, TOAST_MS);
  }

  function fire(name, it) {
    try {
      document.dispatchEvent(new CustomEvent(name, { detail: it }));
    } catch (e) {
      // 老浏览器无 CustomEvent 构造函数 → 退化为不派发（本脚本不依赖监听方）
    }
  }

  function onDone(it) {
    var label = subjectBits(it).main + ' 的' +
                (it.report_type_label || it.report_type || '') + '报告';
    state.doneAt = Date.now();
    var link = it.view_url
      ? '<a href="' + esc(it.view_url) + '" target="_blank" rel="noopener">打开报告</a>'
      : '<a href="reports.html">前往报告库</a>';
    toast(esc(label) + '已生成，已进入报告库。' + link, false);
    fire('tradeai:task-done', it);
  }

  function onFailed(it) {
    var label = subjectBits(it).main + ' 的' +
                (it.report_type_label || it.report_type || '') + '报告';
    toast(esc(label) + '生成失败：' + esc(it.error || '原因未知'), true);
    fire('tradeai:task-failed', it);
  }

  /** 检测状态迁移。**首帧只记录不提示**：否则每次刷新页面都会把历史任务"播报"一遍。 */
  function detectTransitions() {
    state.items.forEach(function (it) {
      var prev = state.seen[it.task_id];
      state.seen[it.task_id] = it.status;
      if (prev === undefined || prev === it.status) return;
      if (it.status === 'SUCCESS') onDone(it);
      else if (it.status === 'FAILED') onFailed(it);
    });
    var live = {};
    state.items.forEach(function (it) { live[it.task_id] = 1; });
    Object.keys(state.seen).forEach(function (k) {
      if (!live[k]) delete state.seen[k];   // 被服务端容量淘汰 → 同步清理，避免无限增长
    });
  }

  /* ===== 轮询 ===== */
  function schedule() {
    if (timer) clearTimeout(timer);
    if (document.hidden) return;             // 页面不可见时停轮询，回到前台立刻补一次
    timer = setTimeout(poll, state.active > 0 ? POLL_ACTIVE : POLL_IDLE);
  }

  function poll() {
    if (state.loading) return;
    state.loading = true;
    fetch(API + '?limit=50', { credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) return null;   // 未登录：静默停（页面会被门禁跳转）
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        state.loading = false;
        if (!d) { state.items = []; state.active = 0; state.err = ''; render(); schedule(); return; }
        state.err = '';
        state.items = d.items || [];
        state.active = d.active || 0;
        detectTransitions();
        render();
        schedule();
      })
      .catch(function (e) {
        state.loading = false;
        state.err = '无法读取任务列表（' + (e && e.message ? e.message : '网络异常') + '）。';
        render();
        schedule();
      });
  }

  /* ===== 挂载 ===== */
  function mountFab() {
    elFab = document.createElement('button');
    elFab.type = 'button';
    elFab.className = 'tc-fab';
    elFab.hidden = true;
    elFab.addEventListener('click', openPanel);
    document.body.appendChild(elFab);

    elMask = document.createElement('div');
    elMask.className = 'tc-mask';
    elMask.hidden = true;
    elMask.addEventListener('click', closePanel);
    document.body.appendChild(elMask);

    elPanel = document.createElement('aside');
    elPanel.className = 'tc-panel';
    elPanel.hidden = true;
    elPanel.setAttribute('role', 'dialog');
    elPanel.setAttribute('aria-label', '报告任务中心');
    elPanel.innerHTML =
      '<div class="tc-head"><span class="t">报告任务中心</span>' +
      '<button class="x" type="button" aria-label="关闭">×</button></div>' +
      '<div class="tc-scroll"></div>' +
      '<div class="tc-foot"><a href="reports.html">前往报告库 →</a></div>';
    document.body.appendChild(elPanel);
    elBody = elPanel.querySelector('.tc-scroll');
    elPanel.querySelector('.tc-head .x').addEventListener('click', closePanel);

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && state.open) closePanel();
    });
  }

  function openPanel() {
    if (!elPanel) return;
    state.open = true;
    elMask.hidden = false;
    elPanel.hidden = false;
    render();
  }

  function closePanel() {
    if (!elPanel) return;
    state.open = false;
    elMask.hidden = true;
    elPanel.hidden = true;
  }

  function boot() {
    injectStyle();
    var slot = document.getElementById('taskCenter');
    if (slot) {
      elInline = slot;
      slot.classList.add('tc-inline');
    } else {
      mountFab();
    }
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) poll();
    });
    // 报告库删掉了报告（F-06 删除）→ 服务端同时清掉了指向它的**成功**任务记录，
    // 因为那条记录挂在「最近完成」里、链接已经 404（N-21 的形状）。
    // 这里立刻重拉一次，让它马上消失，而不是等下一个轮询周期（空闲时是 15s）。
    document.addEventListener('tradeai:report-removed', function () { poll(); });
    poll();
  }

  // 对外只暴露「立刻刷新」——页面在别处刚提交完任务时可就地催一次，
  // 不必等下一个轮询周期（提交后 1.5s 内就能看到进度开始动）。
  window.TaskCenter = {
    refresh: poll,
    open: openPanel,
    remove: removeTasks,
    state: state
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
