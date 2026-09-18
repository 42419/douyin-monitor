# 前置：部署 DTK v5

dywatch 不直接访问抖音，它需要一个可达的
[Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
（DTK）**v5** 实例，以及一把带有正确 scope 的 API Key。这一步与 dywatch 本身无关，
按 DTK 官方文档部署即可；这里只梳理 dywatch 用得到的那部分。

如果你已经有可用的 DTK v5 实例和 API Key，可以直接跳到
[安装 dywatch](/guide/quick-start)。

## 用 Docker Compose 部署 DTK v5

DTK 官方推荐的部署方式是 Docker Compose，镜像同时发布 `linux/amd64` 与
`linux/arm64`（树莓派等 ARM 设备也能直接拉镜像，不用本地编译）。

```bash
git clone https://github.com/Evil0ctal/Douyin_TikTok_Download_API.git
cd Douyin_TikTok_Download_API
```

DTK 不内置任何默认密码或密钥，需要先写 `.env`：

```bash
POSTGRES_PASSWORD=$(openssl rand -hex 24)
REDIS_PASSWORD=$(openssl rand -hex 24)
cat > .env <<EOF
DTK_SECRET_KEY=$(openssl rand -base64 48)
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
REDIS_PASSWORD=${REDIS_PASSWORD}
DTK_DATABASE_URL=postgresql+asyncpg://dtk:${POSTGRES_PASSWORD}@postgres:5432/dtk
DTK_REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
EOF
```

```bash
docker compose -p dtk -f docker/compose.yml up -d --wait --wait-timeout 300
docker compose -p dtk -f docker/compose.yml logs api   # 日志里会打印初始化链接
```

`up` 尚未创建任何账号时，日志里会打印一条横幅，给出一个带一次性 token 的链接，
形如 `http://127.0.0.1:8000/setup?token=Xf3k...`（token 24 小时内有效）。**打开
这个完整链接**（不是裸的根地址），走完四步安装向导创建第一个管理员账号。

::: tip 跳过本地编译
默认第一次 `up` 会在本机编译镜像，耗时几分钟。想直接拉官方发布的镜像可以改用：

```bash
export DTK_IMAGE=evil0ctal/douyin_tiktok_download_api
export DTK_IMAGE_TAG=latest
docker compose -p dtk -f docker/compose.yml pull
docker compose -p dtk -f docker/compose.yml up -d
```
:::

::: details 不用 Docker？
DTK 官方也支持裸机部署（这也是项目自身的开发方式），需要自备 PostgreSQL 17
（**必须带 TimescaleDB 扩展**，普通 `postgres:17` 不够）、Redis 8、Python 3.12 + uv，
如果要编译控制台还需要 Node 22。具体步骤见 DTK 仓库的
[Installation 文档](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/main/documents/en/02-installation.md#running-without-docker)。
:::

::: warning 身份池不会自己有数据，dywatch 会一直 503 直到你处理这一步
安装向导走完之后，DTK 的"身份池"默认是**空的**。dywatch 的每一次抓取都要从身份池
拿一个身份，池子空着的话所有请求会一直报 `503 IDENTITY_POOL_EXHAUSTED`，跟 Key、
scope 都无关——这一步很容易被漏掉，因为部署本身不会报错。

DTK 提供两条路，二选一：

- **浏览器容器自动铸造身份**（部署 DTK 时额外加 `--profile browser`，见 DTK 官方
  [快速开始](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/main/documents/en/01-quickstart.md)
  的 Step 3 / Step 4）：多占用约 1.7GB 镜像、峰值 2.5GB+ 内存，池子会在几分钟内
  自动补到目标值
- **手动导入 Cookie**：不需要浏览器容器，在控制台的 **Identities → Import
  cookies** 页面粘贴一份登录态的 Cookie（浏览器扩展导出的 JSON、Netscape
  `cookies.txt`、或一行一个 `key=value` 都可以），配上同一个浏览器会话的
  User-Agent

装完 DTK 之后，先确认控制台的 **Identities** 页面至少有一个 `active` 状态的身份，
再去[安装 dywatch](/guide/quick-start)；`dywatch doctor` 的自检也会在这一步没做
好时把 `503` 暴露出来。
:::

## 创建 API Key：只需要两个 scope

在 DTK 控制台左侧 **Access → API keys** 页面点 **Create key** 创建一把新 Key。
**dywatch 的监控本身只需要两个 scope**：

| scope          | 用途                                                 |
| -------------- | ---------------------------------------------------- |
| `douyin:read`  | 读作者作品列表（主抓手）、识别分享链接、读任务结果   |
| `archive:read` | 读本地归档，做删除交叉确认（零身份成本）             |

::: tip 创建 Key 需要 operator/admin，但 Key 本身按 scope 鉴权，跟账号角色无关
DTK 的 Key 没有单独的"角色"字段可选——**创建**一把 Key 本身需要用 `operator` 或
`admin` 权限的账号登录控制台（`viewer` 账号看不到 Create key 的入口），用刚才
安装向导创建的管理员账号登录去建就行。但 Key 一旦建好，`douyin:read` /
`archive:read` 这些端点**只按 Key 自己的 scope 鉴权，不看创建它的账号是什么角色
**——所以只勾这两个 scope，就算是管理员账号建出来的 Key，也一样不能碰读写之外的
任何东西（详见[权限 / API Key](/reference/permissions)）。
:::

如果打算后续开启[归档下载](/guide/archive-download)，需要额外申请
`media:write`，但**不建议在第一次部署时就开**，等确认监控本身跑稳了再考虑。

### Key 的形态

API Key 的格式是 `dtk_<12位十六进制>_<32位base64url>`，共 **49 个字符**。
**完整值只在创建时显示一次**，请立刻保存下来——这是后续在 dywatch 侧最容易出错的
地方（复制不全会直接导致 `401 UNAUTHENTICATED`，见[排障](/operations/troubleshooting)）。

拿到 API Key 之后，就可以去[安装 dywatch](/guide/quick-start)了。
