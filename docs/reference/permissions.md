# 权限 / API Key

## dywatch 需要的 scope

在 DTK 控制台的 **API keys → Create key** 创建 Key 时（措辞与上游
[Users and API keys](https://douyin.wtf/users-and-api-keys/) 一致），
**监控需要三个 scope**：

| scope             | 用途                                               |
| ----------------- | -------------------------------------------------- |
| `douyin:read`     | 读作者作品列表（主抓手）、识别分享链接、读任务结果 |
| `archive:read`    | 读本地归档，做删除交叉确认（零身份成本）           |
| `identity:manage` | 给读请求带 `identity=`，钉在 `DTK_IDENTITY` 上     |

**创建**这把 Key 需要用 `operator` 或 `admin` 权限的账号登录控制台——`viewer`
账号既进不了创建页，`identity:manage` 本身也要求 operator 及以上（DTK 的
`require_credential_access` 同时校验 scope 与角色）。

`identity:manage` 是本工具唯一的高权限项：它能解密查看任意身份的 cookie 明文，
换来的是"始终看到登录视角"——游客视角拿不到作者的最新作品（见
[判定规则 · 单身份读取](/guide/detection-rules#单身份读取)）。这把 Key 不要给别的工具用。

如果打算开启[归档下载](/guide/archive-download)，额外申请 `media:write`（写
权限），**不建议在第一次部署时就开**，等确认监控本身跑稳了再考虑。

## Key 的形态

API Key 的格式是 `dtk_<12位十六进制>_<32位base64url>`，共 **49 个字符**。
**完整值只在创建时显示一次**，请立刻保存下来。

复制不全是最容易踩的坑，表现为 `401 UNAUTHENTICATED`（这是"Key 被拒"，不是
"权限不足"——权限不足是 `403 FORBIDDEN_SCOPE`）。`dywatch doctor` 会在启动自检
时验证 Key 有效性与 scope 是否齐备，建议每次改动 `.env` 后都先跑一遍。

## 两个不需要任何 scope 的接口

dywatch 用到的两个接口不需要任何 scope 鉴权，只要凭据本身有效即可：实例健康状态
查询（面板展示上游健康用）、`dywatch doctor` 自检时用来验证 Key 是否有效的接口。
具体细节见仓库里的
[`DESIGN.md`](https://github.com/42419/douyin-monitor/blob/main/DESIGN.md#25-权限与-api-key-配置逐端点核实的精确清单)
第 2.5 节，那里有逐端点核实过的精确清单。

## 为什么不多要权限

dywatch 对 DTK 的定位是"默认纯只读客户端"——不提交采集任务、不改 DTK 的设置、
不注册 watchlist。只申请用得到的 scope 有两个好处：即便 dywatch 的凭据泄露，
攻击面也被限制在"读"；也让你在审计 DTK 的 API Key 列表时，一眼就能确认这把
Key 具体能做什么。

唯一的例外是 `identity:manage`：它确实超出"读"的范畴。但没有它就只能走游客视角，
而抖音不给游客看最新作品——这是"能监控"与"监控不到"的区别，不是可选的优化。
详见[这是什么 · dywatch 与 DTK 的分工](/guide/what-is-dywatch#dywatch-与-dtk-的分工)。
