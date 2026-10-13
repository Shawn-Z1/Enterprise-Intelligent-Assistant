# 维护工具持有管理凭证；普通API/worker镜像不包含备份程序和这些凭证。
FROM zhixing-backend:0.12.0 AS application
FROM postgres:17.6-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libbz2-1.0 libexpat1 libffi8 libgdbm6 liblzma5 libncursesw6 libreadline8 libsqlite3-0 libuuid1 libstdc++6 libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=application /usr/local /usr/local
COPY --from=application /app /app
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "app.maintenance"]
