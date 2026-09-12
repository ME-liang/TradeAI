"""一键生成公司概况报告的 Web API 层。

提供三个端点（详见 server.py）：
- POST /api/report           创建生成任务，立即返回 task_id（异步，不阻塞）
- GET  /api/report/{id}      轮询任务状态 / 进度 / 阶段
- GET  /api/report/{id}/html 任务成功后取回报告 HTML
"""
