# 前置：DTK v5 与 API Key

dywatch 不直接访问抖音，它需要一个可达的
[Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
（DTK）**v5** 实例，以及一把带有正确 scope 的 API Key。

**DTK 本身的部署、账号、身份池都以上游文档为准，本页不复制那些步骤**——抄一份过来，
上游一变我们就得跟着改，还容易改错。下面只列 dywatch 用得到的三件事。

| 想知道                                                    | 去上游                                                                 |
| --------------------------------------------------------- | ---------------------------------------------------------------------- |
| 从零把 DTK 跑起来（`.env`、启动、初始化向导、第一把 Key） | [官方 Quick start](https://douyin.wtf/quickstart/)                     |
| 部署形态、反向代理、备份、升级、扩 worker                 | [Installation and deployment](https://douyin.wtf/installation/)        |
| 身份池怎么补、代理怎么配                                  | [Identities and proxies](https://douyin.wtf/identities-and-proxies/)   |
| 角色与 scope 的完整定义                                   | [Users and API keys](https://douyin.wtf/users-and-api-keys/)           |
| 源码、镜像、问题反馈                                      | [GitHub 仓库](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) |

已经有一个可用的 DTK v5 实例和 API Key？直接去[安装 dywatch](/guide/quick-start)。

## 一、先导入一个登录态身份

dywatch 的每条读请求都会**点名一个身份**（`DTK_IDENTITY`），所以 DTK 里必须有一份
**登录态 cookie**。控制台 **Identities → Import cookies**，导入浏览器里那份（带
`sessionid` 即可），再把它的 **UUID** 复制下来。

导入后点开这个身份，把「轮换范围」设为「**只服务点名它的请求**」：日常流量不会和别的
工具抢身份，也不占身份池水位——dywatch 每次读都点名它。

::: tip 身份池可以是空的
dywatch 不走身份池，池子里有没有游客都不影响它；但那个登录态身份必须存在且 `active`。
:::

## 二、一把 Key，三个 scope

控制台 **API keys → Create key**。dywatch 需要三个：

| scope             | 用途                                               |
| ----------------- | -------------------------------------------------- |
| `douyin:read`     | 读作者作品列表（主抓手）、识别分享链接、读任务结果 |
| `archive:read`    | 读本地归档，做删除交叉确认（零身份成本）           |
| `identity:manage` | 把读请求钉在 `DTK_IDENTITY` 指定的身份上           |

::: tip 建 Key 需要 operator 及以上
`identity:manage` 走的是 DTK 的凭据访问校验：**既看 scope、也看角色**（operator/admin）。
viewer 建的 Key 即使勾了 scope 也会被拒。这个 scope 能解密任意身份的 cookie 明文，
所以这把 Key 只给 dywatch 用（详见[权限 / API Key](/reference/permissions)）。
:::

后续要开[归档下载](/guide/archive-download)再加 `media:write`，但**不建议第一次部署就开**。
上游的完整 scope 表在
[Step 8](https://douyin.wtf/quickstart/#step-8--create-an-api-key)。

## 三、Key 只显示一次

格式是 `dtk_<12位十六进制>_<32位base64url>`，共 **49 个字符**，**完整值只在创建时显示一次**。
复制不全是这里最常见的坑，会直接得到 `401 UNAUTHENTICATED`（见[排障](/operations/troubleshooting)）。

## 确认这一步做对了

```bash
./.venv/bin/python -m dywatch doctor
```

它把"配错了"和"上游坏了"分开：实例可达性、Key 是否有效、scope 是否齐、账号读不读得出来，
身份池没补的 `503` 也会在这里暴露。

下一步：[安装 dywatch](/guide/quick-start)。
