#!/bin/sh
# 容器入口脚本：只做一件事——首次启动时，如果挂载的 /data 目录里还没有
# .env / users.conf，从镜像自带的模板复制一份过去，方便第一次启动。
# 已存在的文件绝不覆盖，不会打扰你已经改好的配置。

set -e

DATA_DIR="${DOUYIN_MONITOR_HOME:-/data}"
mkdir -p "$DATA_DIR"

if [ ! -f "$DATA_DIR/.env" ]; then
    echo "[entrypoint] 未找到 $DATA_DIR/.env，从模板复制一份"
    cp /app/env.example "$DATA_DIR/.env"
    echo "[entrypoint] 请编辑 $DATA_DIR/.env 填好推送渠道等配置，然后重启容器"
fi

if [ ! -f "$DATA_DIR/users.conf" ]; then
    echo "[entrypoint] 未找到 $DATA_DIR/users.conf，从模板复制一份"
    cp /app/users.conf.example "$DATA_DIR/users.conf"
    echo "[entrypoint] 请编辑 $DATA_DIR/users.conf 添加要监控的账号"
fi

exec "$@"
