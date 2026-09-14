# 服务只用标准库，不需要构建工具链，slim 足够。
FROM python:3.12-slim

# 不写 pyc、日志不缓冲——容器里日志被缓冲会让排查时看不到实时输出。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 非 root 运行。这个进程接收公网可达（或内网可达）的 POST，
# 没有任何理由让它有 root 权限。
RUN useradd --create-home --shell /usr/sbin/nologin notifier

COPY notifier/ /app/notifier/

USER notifier

# 容器内监听所有地址：Docker 网络里其他容器要能连进来，
# 绑 127.0.0.1 会让 backend 连不上。对外暴露与否由 compose 的 ports 决定。
ENV BIND_HOST=0.0.0.0 \
    BIND_PORT=8787

EXPOSE 8787

# healthcheck 用服务自己的 /healthz。不装 curl——多一个二进制多一分攻击面，
# 用 Python 标准库发请求即可。
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request,os,sys; \
url=f'http://127.0.0.1:{os.environ.get(\"BIND_PORT\",\"8787\")}/healthz'; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

CMD ["python3", "-m", "notifier.server"]
