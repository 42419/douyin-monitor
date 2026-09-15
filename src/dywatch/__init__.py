"""dywatch —— 基于 Douyin_TikTok_Download_API v5 的抖音账号视频监控工具。

本包对 DTK 是**纯只读**的 HTTP 客户端：不自带签名、不碰 Cookie、不碰身份池。
按职责分层的依赖方向（单向、无环）：

    cli → runtime → loop → pipeline → {dtk, diff, state, notifiers, alerts}
    diff / models / messages / render  —— 纯逻辑，不 import 上面任何一个
    dtk                                —— 唯一知道 HTTP 的地方
    state                              —— 唯一持久化出口
    notifiers                          —— 唯一知道第三方 payload 的地方
"""

__version__ = "0.1.0"
