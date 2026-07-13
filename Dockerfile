# 抖音多用户视频监控 - Docker 镜像
#
# 设计要点：
# - 代码打进镜像，配置和运行数据全部放在 /data（挂载卷），镜像本身不持有
#   任何需要持久化的东西，升级时直接重新 build/pull 镜像即可，不用管数据
# - DOUYIN_MONITOR_HOME 指向 /data，douyin_monitor/config.py 本身就支持
#   这个环境变量覆盖工作目录，不需要额外适配代码
# - entrypoint.sh 只做一件事：/data 里没有 .env / users.conf 时，从镜像里
#   打包的模板复制一份过去，方便第一次启动；已存在的文件绝不覆盖

FROM python:3.12-slim

WORKDIR /app

# 依赖单独一层，代码变动不会导致重新下载依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY douyin_monitor.py .
COPY douyin_monitor/ ./douyin_monitor/
COPY .env.example users.conf.example ./
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV DOUYIN_MONITOR_HOME=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

# 只有开启 Web 状态面板（WEB_ENABLED=true）时这个端口才有意义，
# 默认监听地址是 127.0.0.1，容器里要用 WEB_HOST=0.0.0.0 端口映射才生效，
# 详见 README「Docker 部署」一节
EXPOSE 8787

# 简单的存活探测：status.json 长时间没更新（远超正常轮询间隔）就判定不健康。
# 这是个宽松的兜底心跳检查，不代表业务完全正常（比如 Cookie 过期时进程
# 仍然存活、status.json 也在正常更新，这种情况请配合推送通知一起看）
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "\
import pathlib, sys, time; \
p = pathlib.Path('/data/status.json'); \
sys.exit(0 if p.exists() and time.time() - p.stat().st_mtime < 3600 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "douyin_monitor.py"]
