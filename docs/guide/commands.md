# 命令行

```bash
dywatch                # 常驻监控（systemd 用这个）
dywatch once           # 只跑一轮后退出：上线前确认配置是否正确
dywatch doctor         # 自检：实例可达 / Key 有效 / scope 够用 / 账号读得出来
dywatch status         # 打印最近一轮的状态快照
dywatch config-check   # 打印全部生效配置与每一项的来源
dywatch add "<主页链接或 sec_user_id>" [昵称]
dywatch test-notify    # 给每个渠道发一条测试消息
```

::: tip 简写
命令的完整前缀是 `./.venv/bin/python -m dywatch`，这样写是为了"在安装目录里、
不进虚拟环境也能直接抄着跑"（systemd 用的也是这一条）。执行过
`source .venv/bin/activate` 之后可以直接写 `dywatch doctor`——`install.sh`
装出来的 `.venv/bin/dywatch` 与 `python -m dywatch` 是同一个入口。本站其余
页面一律用 `dywatch <子命令>` 这种简写。
:::

所有命令都接受 `--env /path/to/.env` 指定配置文件（默认 `$MONITOR_HOME/.env`，
其次 `./.env`）；`--version` 打印版本号。

## `dywatch doctor`：先跑这个

`doctor` 把"配错了"和"上游坏了"分成两类——这两类的处理方式完全不同，输出示例：

```
dywatch 0.1.0 —— 自检
上游实例: http://192.168.20.4:8000
✓ 凭据有效：username=yunfei role=admin via=api_key
  scopes: archive:read, douyin:read, media:read, media:write
✓ 必需的 scope 齐备
✓ 归档下载可用：已用 6.9MB / 上限 2048MB（0%），已 pin 0 条，下载器 在线   ← 开了归档下载才有这行
✓ 实例 5.1.0：身份池 douyin active=3 cooling=0 degraded=0
检查 1 个账号（每个消耗 1 个身份）…
  ✓ 示例账号: 18 条（非置顶 15 / 置顶 3，count=15） 2640ms has_more=True
✓ 自检通过
```

`doctor` 与 `once` 每个账号会消耗 1 个 DTK 身份，属正常开销；两者不打网络也能
给出有用的结论（配置校验、Key 的 scope、账号 ID 是否有效）。

## `dywatch add`：把主页链接转成 sec_user_id

```bash
dywatch add "https://www.douyin.com/user/MS4wLjABAAAA..." "可选昵称"
```

内部调用 DTK 的 `/api/v1/tools/parse-url` 把链接转成 `sec_user_id`，**零成本、
不消耗身份**。短链（`v.douyin.com/...`）必须先跳转才能知道目标，dywatch 会提示
你改粘主页链接，而不是替你花一个身份去解析。

## `dywatch config-check`：确认配置有没有生效

打印每一项配置的**来源**（默认值 / `.env` / 环境变量），布尔值写错（比如手滑写成
`ture`）时会在来源列标成`非法,已回退默认`，而不是悄悄当成 `false` 放过去。

## `dywatch test-notify`：验证推送渠道

给 `NOTIFY_CHANNELS` 里配置的每个渠道各发一条测试消息，用来在上线前确认凭据
（token、webhook key……）是否正确。
