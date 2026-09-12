"""数据契约包：六层报告的数据结构与溯源元信息。

阶段 C/D 起，所有进入报告的数字都应携带 SourceRef（来源 / 时点 / 一手或二手），
以满足金融红线「每个数字可溯源、缺失即标未获取」。六层 dataclass（Identity/Business/
Governance/Industry/Financials/Dynamics）是二期 JSON 落盘契约的目标结构，当前仅定义，
由阶段 G 的各引擎逐步填充。
"""
