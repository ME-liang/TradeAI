#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""认证模块（方案《云端部署 + APP + 账号密码登录》阶段 1）。

组成:
  db.py      SQLite 三表(users/sessions/sms_codes) + 连接封装 + 建表/预置管理员脚本
  service.py 密码 scrypt 哈希校验、会话创建/校验/注销、登录验证
"""
