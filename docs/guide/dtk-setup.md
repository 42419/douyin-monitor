# 前置：DTK v5 与 API Key

dywatch 不直接访问抖音，它需要一个可达的
[Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
（DTK）**v5** 实例，以及一把带有正确 scope 的 API Key。

**DTK 本身的部署、账号、身份池都以上游文档为准，本页不复制那些步骤**——抄一份过来，
上游一变我们就得跟着改，还容易改错。下面只列 dywatch 用得到的三件事。

| 想知道                                                        | 去上游                                                              |
| ------------------------------------------------------------- | ------------------------------------------------------------------- |
| 从零把 DTK 跑起来（`.env`、启动、初始化向导、第一把 Key）      | [官方 Quick start](https://douyin.wtf/quickstart/)                   |
| 部署形态、反向代理、备份、升级、扩 worker                     | [Installation and deployment](https://douyin.wtf/installation/)       |
| 身份池怎么补、代理怎么配                                      | [Identities and proxies](https://douyin.wtf/identities-and-proxies/)  |
| 角色与 scope 的完整定义                                       | [Users and API keys](https://douyin.wtf/users-and-api-keys/)          |
| 源码、镜像、问题反馈                                          | [GitHub 仓库](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) |

已经有一个可用的 DTK v5 实例和 API Key？直接去[安装 dywatch](/guide/quick-start)。

## 一、身份池不能是空的

DTK 的**身份池默认是空的**，而 dywatch 每次抓取都要从池子里拿一个身份——池子空着时所有请求
会一直报 `503 IDENTITY_POOL_EXHAUSTED`，**跟 Key、scope 都无关**。这一步最容易漏，因为部署
本身不会报错。

两条路二选一（官方
[Step 3](https://douyin.wtf/quickstart/#step-3--decide-about-the-browser-container) 讲得很细）：
加 `--profile browser` 让浏览器容器自动铸造，或在控制台 **Identities → Import cookies**
手动导入一份登录态 Cookie。装完先确认 **Identities** 页面至少有一个 `active` 身份，
再去装 dywatch。

## 二、一把 Key，只要两个 scope

控制台 **API keys → Create key**。dywatch 的监控本身只需要两个：

| scope          | 用途                                               |
| -------------- | -------------------------------------------------- |
| `douyin:read`  | 读作者作品列表（主抓手）、识别分享链接、读任务结果 |
| `archive:read` | 读本地归档，做删除交叉确认（零身份成本）           |

::: tip 建 Key 需要 operator/admin，但 Key 只按自己的 scope 鉴权
`viewer` 账号看不到 Create key 的入口，用初始化向导建的管理员账号来建就行。Key 建好后，
`douyin:read` / `archive:read` 这些端点**只认 Key 自身的 scope、不看创建者的角色**——
所以只勾这两个，就算是管理员建的 Key 也碰不到别的东西
（详见[权限 / API Key](/reference/permissions)）。
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
