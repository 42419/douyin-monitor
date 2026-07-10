#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音多用户视频更新监听脚本
========================================
定期检查多个抖音账号是否发布了新视频或删除了旧视频，并通过钉钉群机器人推送通知。

用法：
    python3 douyin_monitor.py            # 常驻监控
    python3 douyin_monitor.py --once     # 只检测一轮后退出（便于调试/接入 cron）
    python3 douyin_monitor.py --status   # 查看最近一次状态快照

详细的安装、配置、部署说明见同目录下的 README.md。
核心逻辑已拆分为 douyin_monitor 包，本文件为兼容入口。
"""

from douyin_monitor.cli import main

if __name__ == "__main__":
    main()
