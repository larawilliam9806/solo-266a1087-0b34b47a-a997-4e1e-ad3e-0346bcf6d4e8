FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WQ_DATA_DIR=/data \
    WQ_DOWNSTREAM_DIR=/downstream

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY wq_publish ./wq_publish
COPY wsgi.py ./

RUN mkdir -p /data /downstream
VOLUME ["/data", "/downstream"]

EXPOSE 5000

# 单实例 + 多线程：SQLite BEGIN IMMEDIATE 文件锁与同目录 os.replace
# 的并发正确性在单进程内成立；数据与下游目录通过卷持久化。
# waitress 为生产级 WSGI 服务器（单进程多线程，符合上述并发模型）。
CMD ["waitress-serve", "--host", "0.0.0.0", "--port", "5000", "--call", "wsgi:create_app"]
