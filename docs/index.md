---
layout: home

hero:
  name: dywatch
  text: 抖音账号视频监控
  tagline: 监控多个抖音账号，检测新作品与作品消失，通过钉钉 / 企业微信 / Bark / Server 酱 / Telegram / 通用 webhook 推送通知。建立在 Douyin_TikTok_Download_API v5 之上，默认只读。
  image:
    src: /logo.svg
    alt: dywatch
  actions:
    - theme: brand
      text: 快速开始
      link: /guide/dtk-setup
    - theme: alt
      text: 这是什么
      link: /guide/what-is-dywatch
    - theme: alt
      text: GitHub
      link: https://github.com/42419/douyin-monitor

features:
  - icon: 🔒
    title: 默认只读
    details: 不自带签名、不碰 Cookie、不碰身份池、不直接访问抖音。对上游 DTK v5 只需要 douyin:read 与 archive:read 两个 scope，不提交任务、不改设置。
  - icon: 🛎️
    title: 六种推送渠道
    details: 钉钉、企业微信、Bark、Server 酱、Telegram、通用 webhook，可同时开启多个渠道，新作品优先送达。
  - icon: 🧭
    title: 分级确认，减少误报
    details: 新作品即时推送；作品消失、全部消失、疑似漏检各自有独立的确认轮数与抑制窗口，避免抖音接口抖动带来的误报。
  - icon: 📊
    title: 只读面板
    details: WEB_ENABLED=true 打开一个不消耗身份、无需鉴权的状态面板，一屏看完每个账号的最新状态与事件历史。
  - icon: ⚙️
    title: systemd 原生部署
    details: 不提供容器镜像。一个进程 + 一个 SQLite 文件 + 一份配置，systemd 管好开机自启、崩溃重启、日志与权限隔离。
  - icon: 📦
    title: 可选归档下载
    details: 默认关闭。打开 ARCHIVE_DOWNLOAD_ENABLED 后，新作品会顺手让 DTK 存一份媒体，作为作品下架前的证据留存。
---
