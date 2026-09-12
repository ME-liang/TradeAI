# 公司概况报告 · HTML 排版规范（references）

本规范供 writer 产出 HTML 时遵循，已按项目模板 `report/template_overview.html`
校准（设计令牌、图表库、断点均以模板为准）。WorkBuddy 同名规范的方法论部分
（JS 自检、图表分工、质量细则）全部保留；图表库由 ECharts 替换为项目实际使用的
**Chart.js 4**，CDN 与模板一致用 cdnjs。

---

## 0. 交付前必做：JS 语法自检（最高优先级）

HTML 里手写的内联 `<script>`（尤其 Chart.js `options`）**极易出现括号 / 引号
配对错误**——多层嵌套对象、箭头函数、字符串字面量跨行，自己读一遍看不出来，
但只要有一处失配，整个 `<script>` 块就 `SyntaxError`，该页**所有图表全部不渲染**
（不是一个图空，是全空）。这是 HTML 报告最高频的致命缺陷。

系统会对产物执行 `node --check` 逐块校验，失败即带报错打回重写。写作时先自查，
高发错误形态（写之前先警惕）：

- `data:arr.map(s=>({value:s.a,itemStyle:{color:s.c}}))` —— `=>({` 配 `}))`，
  少一个 `)` 或多一个 `}` 都废
- `formatter:'{b}: {c}%'` —— 字符串别跨行写，跨行会断裂
- `datasets:[{...}}, {...}]` —— 嵌套对象结尾多一个 `}`
- `options:{scales:{y:{...,'新增'}}}` —— 对象里别混入游离裸值
- 数据数组里的 `null` 是合法值（断点），但别把字符串 `"null"` 混进数字数组

不要把「写出了 HTML 文件」当作完成——**图表能渲染才算完成**。

## 1. Chart.js 4 骨架（填 data，别从零手敲嵌套）

引库（放 `<head>`，与 template_overview.html 完全一致）：

```html
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
```

容器语义（模板通行写法）：

```html
<div class="chart"><canvas id="chartTrend"></canvas></div>
```

柱 / 折线（含双轴）：

```javascript
new Chart(document.getElementById('chartTrend'), {
  type: 'bar',
  data: {
    labels: ['2023', '2024', '2025'],
    datasets: [
      { label: '营收(亿)', data: [59.44, 78.21, 88.67], backgroundColor: '#185fa5' },
      { label: '归母净利(亿)', data: [0.66, 0.98, 1.72], type: 'line',
        yAxisID: 'y2', borderColor: '#d92d20' }
    ]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    scales: {
      y:  { type: 'linear', position: 'left' },
      y2: { type: 'linear', position: 'right', grid: { drawOnChartArea: false } }
    }
  }
});
```

占比（环形，替代 ECharts pie）：

```javascript
new Chart(document.getElementById('chartMix'), {
  type: 'doughnut',
  data: {
    labels: ['电子元器件', '结构模块器(模组)', '其他业务'],
    datasets: [{ data: [92.3, 3.92, 3.78] }]
  },
  options: { responsive: true, maintainAspectRatio: false,
             cutout: '55%', plugins: { legend: { position: 'right' } } }
});
```

要点：

- **量级差大的双序列必须双轴**（如营收数十亿 vs 净利个位数），第二轴
  `grid.drawOnChartArea:false` 避免网格线打架。
- **空值不入图**：完全没有数据的区间不要硬塞占位；序列中断点用 `null`
  并接受 spanGaps 行为，不得编数补齐。
- 取数多取一个完整周期消除前段空值（快照已按期提供，照用即可）。
- 图表 id 不得重名；数字密集的板块可做「图 / 表可切换」，别让图取代精确数字。

## 2. 设计令牌（与 template_overview.html 严格一致）

```css
:root{
  --ink:#1b2330; --ink2:#4a5464; --ink3:#7b8494;
  --line:#e4e7ec; --bg:#f6f7f9; --card:#ffffff;
  --brand:#185fa5; --brand-soft:#e6f1fb;
  --up:#d92d20; --down:#0f7b4f; --warn:#b54708; --warn-soft:#fef0e6;
}
```

- **浅底深字研报风**，卡片式布局（`.card` 圆角 12px、1px 描边），避免暗色
  仪表盘风或开篇大段文字。
- **首屏结论先行**：header 后紧跟结论卡（结论卡左侧 3px 品牌色描边）。
- **A 股语境红涨绿跌**：涨/风险高用 `--up`，跌/干净用 `--down`，与模板
  `.up/.down` 语义一致。
- 字体栈：`-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
  "Microsoft YaHei",sans-serif`；正文 14px / line-height 1.65。
- 布局：`.wrap{max-width:1040px}` 居中；移动端 `@media(max-width:640px)`
  断点缩小标题与图表高度。
- 表格：表头浅灰底 12px，高亮行 `.hl` 浅蓝底（`#f0f6fc`），数字右对齐 +
  `font-variant-numeric:tabular-nums`。

## 3. 图表分工

- 趋势 / 对比 / 占比 / 分布等**有数值轴的数据图用 Chart.js**，别堆一长串
  趋势型表格。
- 产业链图谱 / 股权结构等**关系拓扑用 HTML+CSS 盒子或 SVG**（规整结构用 CSS
  盒子、复杂拓扑用 SVG）——不依赖 JS，天然规避括号失配。
- 查阅 / 多维对照型数据仍用表格（同业对标、前十大股东、分红回购）。

## 4. 页脚（不可省略）

页脚依次包含：数据来源清单、口径说明、来源分级说明（一手 / 非一手·待核实）、
检索方式说明、固定免责声明文案（见 SKILL.md 第四节，禁止改写缩减）、
数据快照日期。
