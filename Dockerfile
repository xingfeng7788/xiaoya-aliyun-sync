# ---- 构建阶段 ----
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- 运行阶段 ----
FROM python:3.11-slim

LABEL maintainer="xiaoya-helper"
LABEL description="小雅资源助手 - 阿里云盘资源搜索与管理工具"

# 安装运行时依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends aria2 tini && \
    rm -rf /var/lib/apt/lists/*

# 复制 Python 依赖
COPY --from=builder /install /usr/local

WORKDIR /app

# 先复制不常变动的文件（利用缓存）
COPY aligo/ ./aligo/
COPY requirements.txt .

# 复制应用代码
COPY app.py db.py scheduler.py ./
COPY templates/ ./templates/

# 数据持久化目录
RUN mkdir -p /app/data /downloads

# 环境变量
ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=5666 \
    DB_PATH=/app/data/xiaoya.db

EXPOSE 5666

VOLUME ["/app/data", "/downloads"]

# 使用 tini 作为 PID 1，正确处理信号
ENTRYPOINT ["tini", "--"]
CMD ["python", "app.py"]
