# 公司基本面分析 Agent（脚手架）

面向上市公司的自动化基本面分析 Agent：输入公司名称或股票代码，自动完成
数据获取、指标计算、分析与结论生成，并输出一份自包含、响应式的 HTML 报告。

> 当前状态：**脚手架（scaffold）** —— 目录、接口、数据契约已就位，但六大
> 分析模块的具体指标计算逻辑尚未实现（引擎返回空结果）。可作为后续填充的基线。

## 目录结构

```
fundamental_agent/
├── config.yaml            # 数据源/行业映射/报告输出配置
├── requirements.txt       # Python 依赖
├── README.md
├── src/fundamental_agent/
│   ├── main.py            # 编排入口：解析参数 → 取数 → 六引擎 → 渲染
│   ├── models.py          # 共享数据结构（Metric / ModuleResult / AnalysisResult）
│   ├── entity.py          # 公司名/代码 → 标准实体（TODO）
│   ├── data/
│   │   ├── client.py      # DataClient 抽象接口（关键契约）
│   │   ├── tushare.py     # Tushare 具体实现（TODO）
│   │   └── dummy.py       # 空数据客户端，用于无 token 时跑通流程
│   ├── engine/
│   │   ├── base.py        # 引擎统一基类
│   │   ├── financials.py      # ① 财报解析
│   │   ├── profitability.py   # ② 盈利能力
│   │   ├── growth.py          # ③ 成长性
│   │   ├── solvency.py        # ④ 偿债能力
│   │   ├── efficiency.py      # ⑤ 运营效率
│   │   └── benchmark.py       # ⑥ 行业对标
│   └── report/
│       ├── template.html  # 响应式 Jinja2 模板（占位块）
│       └── render.py      # 结果字典 → 单文件 HTML
└── tests/test_smoke.py    # 冒烟测试：空壳也能生成 HTML
```

## 运行方式

```bash
pip install -r requirements.txt

# 使用 dummy 客户端跑通空壳（无需 token）
cd src && python -m fundamental_agent.main --code 000001 --name 测试 --dummy

# 接入真实数据（填入 config.yaml 的 tushare_token 后）
cd src && python -m fundamental_agent.main --code 000001 --name 平安银行
```

## 待实现清单（按模块）

- [ ] entity.py：名称/代码 → 标准实体解析与防重名
- [ ] data/tushare.py：三表与行业可比公司取数
- [ ] engine/financials：三表结构化 + 勾稽校验
- [ ] engine/profitability：毛利率/净利率/ROE/ROA + 杜邦
- [ ] engine/growth：YoY / CAGR / 增长质量
- [ ] engine/solvency：流动/速动比率、资产负债率、利息保障倍数
- [ ] engine/efficiency：存货/应收/总资产周转率、营业周期
- [ ] engine/benchmark：可比公司筛选 + 分位定位 + 雷达图
- [ ] report/template.html：补充图表与结论区块

> 免责声明：本工具输出仅供研究与学习参考，不构成任何投资建议。
