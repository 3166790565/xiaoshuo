# 青灯书斋运行镜像：单进程 uvicorn，可变数据全在 /data（由外面挂载进来）。
# 镜像里不含 .env 与库文件，换库只换挂载卷里的文件。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Shanghai \
    NOVEL_DATA_DIR=/data \
    NOVEL_DB_PATH=/data/novel.db

WORKDIR /app

# 依赖单独一层，改代码不用重装
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 全文检索靠 FTS5 + trigram 分词器（SQLite >= 3.34）。
# 构建时就验证，免得等到有人搜索时才发现基础镜像的 sqlite 不带这两样。
RUN python -c "import sqlite3;c=sqlite3.connect(':memory:');c.execute(\"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')\");print('sqlite', sqlite3.sqlite_version, 'fts5 + trigram OK')"

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY legado ./legado
COPY scripts ./scripts
COPY run.py ./

# 用固定 uid 1000 跑，和多数 Linux 首个普通用户一致，bind mount 权限通常直接对得上。
# 对不上就在宿主机 `chown -R 1000:1000 data`。
RUN useradd --create-home --uid 1000 novel \
    && mkdir -p /data \
    && chown -R novel:novel /data /app
USER novel

EXPOSE 8000

# /healthz 只 ping 一次数据库，不碰 FTS，13 GB 的库也是毫秒级
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=5)"

# 不用 run.py：它默认监听 127.0.0.1，在容器里等于外面连不上。
# 必须 0.0.0.0，端口固定 8000，对外端口交给 compose 映射。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
