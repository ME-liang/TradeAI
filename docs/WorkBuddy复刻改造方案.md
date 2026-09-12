# WorkBuddy 方式复刻改造方案 · AI 生成公司概况报告

> 状态：方案（未实施）　日期：2026-09-12
> 前置评估：已通读现状实现（见本文第二节基线），红线不变——不触碰 `report/render.py` 的 `render()`、`report/template.html` 与基本面 pipeline 任何路径。

---

## 一、目标与复刻机制

将概况报告生成方式改造为与 WorkBuddy 会话内实现对等的形态，复刻其四个核心机制：

| # | WorkBuddy 机制 | 本项目等价物 |
|---|---|---|
| M1 | skill 框架 + 排版规范作为上下文注入 | 本地 `skills/company-profile/` 目录，运行时读入拼 system prompt |
| M2 | agentic_search 多步自主检索（定性信息来源） | `agents/search_agent.py`：LLM 规划 → 调用检索工具 → 综合带来源摘要 |
| M3 | LLM 一次直写完整 HTML（含 Chart.js） | `agents/writer_agent.py`：skill + 检索摘要 + 六层数据快照 → 整段成稿 |
| M4 | 生成后校验（node --check）+ 交付 | 渲染产物 JS 语法校验、失败带错重试 ≤2；落盘路径不变 |

**改造后链路**：前端按钮（不变）→ 8002 API（不变）→ 取数层（不变，产出六层 JSON 契约）→ search_agent 定性补检 → writer_agent 直写 HTML → JS 校验 + 数字回查 → 落盘 `output/reports/{code}/overview/{date}/report.html`（不变）。

## 二、现状基线（改造起点）

- 触发：`frontend/stock_detail.html` `#aiOverview` 按钮 → POST `/api/report {report_type:'overview'}` → 轮询 + iframe 预览（**此层全部保留不动**）。
- API：`api/server.py` TaskManager 异步 + 当日产物复用（措施4）+ meta/profile_json 分离落盘（**保留**）。
- 取数：`main.py run_pipeline` → cninfo / 东财 / akshare + PyMuPDF 年报抽取 → `pipeline/company_profile.py build_profile` 六层 JSON 契约 + `build_financial_summary` 事实核对表（**保留，作为 writer 的 grounding 输入**）。
- 成稿（将被替换的分支）：`report_types/overview.py generate_overview_content` LLM 出 40 字段 JSON → `build_overview_data` 兜底合并 → Jinja `template_overview.html` 渲染。
- 护栏资产：`overview.py` 中的事实核对表、持股硬错误拦截、客户/合作方白名单——**不删除**，其中可在 writer 模式下复用的部分（数字回查、事实核对表）迁移到新校验器。

## 三、改动清单

### 新增文件（纯新增，零耦合）

| 文件 | 职责 |
|---|---|
| `skills/company-profile/SKILL.md` | 六层报告框架 + 各层内容要求 + 输出规范（writer 的 system prompt 主体）。以现 `overview.md` 契约 + WorkBuddy a-share-company-profile 框架融合改写 |
| `skills/company-profile/references/html-report-style.md` | 排版规范：蓝底投研风、配色、来源标注、口径说明、免责声明（从 WorkBuddy 同名文件移植并按 template_overview.html 校准） |
| `src/fundamental_agent/llm/skill_loader.py` | 读 skill 目录：`load_skill("company-profile")` 返回 `{system, references[]}` |
| `src/fundamental_agent/agents/__init__.py` | agents 包 |
| `src/fundamental_agent/agents/search_agent.py` | M2：检索代理（两档实现，见 5.2） |
| `src/fundamental_agent/llm/prompts/search_plan.md` | 检索规划提示词（选项 B 用） |
| `src/fundamental_agent/agents/writer_agent.py` | M3：整段成稿 + 结构化日志 |
| `src/fundamental_agent/agents/validate_html.py` | M4：JS 语法校验（node --check）+ 数字回查 + 重试编排 |
| `tests/test_search_agent.py`、`tests/test_writer_agent.py`、`tests/test_validate_html.py` | 单测 |

### 修改文件（小改，均有开关）

| 文件 | 改动 |
|---|---|
| `config.yaml` | 新增 `overview_writer:` 段：`mode: contract|direct`（direct=WorkBuddy 方式）、`search.enabled/provider/api_key_env`、`js_check.enabled`、`max_retries` |
| `main.py` | overview 分支：按 `mode` 路由到 writer_agent 或原 `generate_overview_content`；检索调用插在 build_profile 之后（并行于引擎后段，见 5.2 性能设计） |
| `.env.example` | 补 `SEARCH_API_KEY`（及选项 B 所需密钥说明） |
| `llm/prompts/overview.md` | 不动（contract 模式继续使用）；不迁移删除 |

### 依赖与前端

- 新依赖：web search API 的 HTTP 客户端（用已有 `requests/httpx`，不引 SDK）；`node --check` 需部署机有 Node.js（无则降级跳过 + warning 日志）。
- Chart.js / ECharts 资源本地化（writer 产出的 HTML 引用 `frontend/js/` 或 `report/assets/` 本地路径，禁 CDN）。
- 前端按钮、轮询、iframe **零改动**。

## 四、writer_agent 设计要点（M3，风险最高件）

**输入（user 消息，四段式）**：
1. 公司标识 + 任务说明；
2. 六层 JSON 契约快照（`build_profile` 产物，全部精确数字的唯一来源）；
3. 关键财务事实核对表（复用 `_key_facts_brief` 逻辑，真值前置钉死）；
4. search_agent 综合摘要（仅定性层：合作关系、行业格局、动态事件，强制带来源与日期，标注"非一手来源·待核实"）。

**输出约束**：完整 HTML 单文件；图表数据直接内嵌 JSON（数值只能取自第 2/3 段）；缺失字段显示"未获取"；页脚含数据快照日期与免责声明。

**与 contract 模式的关键差异**：数字仍全部来自结构化快照（writer 不允许"凭检索材料写精确财务数字"），检索只负责定性叙述——这是 WorkBuddy 原实现中"多源交叉验证后成稿"角色的本地降级替代，也是护栏能继续生效的前提。

## 五、分步实施计划（顺序即依赖序，每步独立 commit）

### Step 0 基线固化（半天）
- 建分支 `feat/wb-replication`；跑 300184 overview 存 baseline HTML 与护栏命中计数。
- **验证**：baseline 与 golden-B/C 对比留档。**回滚**：切回分支即可。

### Step 1 skill 移植 + loader（1 天，纯新增）
- 写 `skills/company-profile/`（SKILL.md 融合 overview.md 契约与六层框架；references 排版规范按 template_overview.html 校准）；写 `skill_loader.py` 与单测。
- **验证**：单测断言 loader 产出包含六层框架关键词与 references 全文；文件缺失时报 FileNotFoundError（沿用 loader 既有约定）。
- **回滚**：删目录。

### Step 2 search_agent（1–2 天）
- 先做**选项 A**：web search API（博查/Tavily 等国内可用者）单调用 → LLM 综合为带来源摘要；失败/超时返回 None（走"未获取"降级）。选项 B（function calling 自建多步循环）作为二期。
- **验证**：mock 单测；真实跑 300184 三类定性查询（上下游伙伴、行业格局、近期动态），人工核对来源真实性；断言超时 < 60s。
- **回滚**：`search.enabled: false` + 删文件。

### Step 3 writer_agent + 校验闭环（2–3 天，核心）
- `writer_agent.py`：按第四节四段式拼 prompt → router 单次直写 HTML（max_tokens 需放宽，见风险 R3）。
- `validate_html.py`：① 提取内联 script → `node --check` 逐块校验，失败把报错原文回传 writer 重试 ≤2；② 数字回查：正则抽取正文数值（含千分位/亿/％），逐一比对快照与核对表，未命中即打回；③ 复用 `_WRONG_HOLDING_PATTERNS` 等存量硬错误拦截。
- **验证**：坏 JS / 假数字 / 示例回显三类注入单测全绿；真实跑 300184，护栏计数 ≥ contract 模式基线；HTML 在 8000 iframe 正常渲染、图表可交互。
- **回滚**：`mode: contract` 一键切回，writer 文件保留。

### Step 4 API 接线 + 双模式开关（1 天）
- `main.py` overview 分支按 `mode` 路由；meta.json 增记 `writer_mode` 与 `search_used`，便于产物溯源与 A/B 对比；当日复用逻辑不变（按 mode 区分产物目录或 meta 校验）。
- **验证**：两种模式各跑一份，确认互不污染、复用命中正确；前端全流程（按钮→进度→iframe）走通。
- **回滚**：改回 `mode: contract`，前端无感。

### Step 5 灰度切换（观察期 1–2 周）
- 默认 `mode: direct`（WorkBuddy 方式）跑真实股票 ≥10 只，人工抽检定性章节与数字回查拦截率；护栏误杀率明显高于基线时回退 contract。
- **验收标准**：① 数字回查零放行；② JS 校验 PASS 率 ≥95%（重试后）；③ 定性章节带来源率 100%；④ 单份端到端时长可接受（见 R4）。

## 六、风险与对策

| # | 风险 | 对策 |
|---|---|---|
| R1 | 直写 HTML 绕过逐字段护栏，幻觉回归 | 数字只允许来自快照 + 数字回查强制打回；存量硬错误正则全量保留；writer prompt 明示"无来源精确数字=生成失败" |
| R2 | 检索内容引入不可控素材 | search_agent 输出强制带来源+日期，writer 侧标注"非一手来源·待核实"；`search.enabled` 开关可整体关闭 |
| R3 | 长输出截断（契约快照+检索摘要+整份 HTML） | 快照只注入 writer 用到的层（六层全量 JSON 经 `_compact` 瘦身）；max_tokens 上调并实测；输出超限时重试并压缩检索摘要 |
| R4 | 端到端时长变长（检索 + 长成稿 + 重试） | 检索与引擎后段并行；TaskManager 进度语义不变；meta 记录各阶段耗时，超阈值告警 |
| R5 | 部署机无 Node.js | js_check 降级为跳过 + warning；备选 Python 括号/JSON 结构粗校验兜底 |
| R6 | 双模式产物混淆 | 产物目录仍按 report_type 隔离，meta.json 写 `writer_mode`；复用判定加 mode 比对 |

## 七、测试与回滚策略（汇总）

- **测试**：pytest 单测（mock 检索/LLM）→ 300184 真实端到端 → golden 基线对比 → 灰度 ≥10 只股票抽检；护栏命中计数与误杀率作为核心回归指标，不得低于 contract 模式基线。
- **回滚**：三层——① 每步独立 commit 可单独 revert；② `overview_writer.mode` / `search.enabled` / `js_check.enabled` 三开关运行时切换；③ search_agent / writer_agent 纯新增，任何失败返回 None 走既有"未获取"降级，最坏情况等价现状。
