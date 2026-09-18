# 监控列表 users.conf

```
# <sec_user_id>|<昵称>
MS4wLjABAAAA4MjTvxSsNOjHfi9kfyRdu0KMKRHA1dPNv1WQQwW0OKY|示例账号
```

`sec_user_id` 以 `MS4wLjABAAAA` 开头，是抖音账号的稳定 ID。

## 不知道 ID 怎么写？直接粘主页链接

```bash
dywatch add "https://www.douyin.com/user/MS4wLjABAAAA..."
```

见 [命令行 · dywatch add](/guide/commands#dywatch-add-把主页链接转成-sec-user-id)。

## 格式说明

- 一行一个账号，格式为 `<sec_user_id>|<昵称>`
- 昵称只用于通知展示，可以随便写，也可以与别的账号重复
- `#` 开头为注释，空行忽略

## 改完不用重启

`users.conf` 按文件的 mtime **热加载**，运行中的实例下一轮自动生效——只有改
`.env` 才需要 `systemctl restart dywatch`。

## "作者 ID 写错了"怎么发现

DTK 对**形态合法但不存在**的 `sec_user_id` 会返回 `200 + items: []`，上游永远
不会主动报错。所以 dywatch 区分两种"空列表"：

- 从未见过作品的账号连续 `EMPTY_ROUNDS_ALERT`（默认 3）轮为空 → 告警
  **"该账号始终无作品，请核实 ID"**
- 曾经有作品的账号突然变空 → 走[判定规则](/guide/detection-rules)里的
  "全部消失"分级确认

排障时如果发现某个账号一直"无作品"，大概率是 ID 写错了：把 `users.conf` 里对应
的行换成主页链接，重新 `dywatch add` 一遍即可。
