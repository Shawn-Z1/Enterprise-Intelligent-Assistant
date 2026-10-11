FROM python:3.12.12-slim-bookworm
# 图表需显示中文人员/部门名称；字体仅在镜像构建阶段准备。
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pandas==2.2.3 openpyxl==3.1.5 duckdb==1.2.2 matplotlib==3.10.1 pillow==12.3.0 sqlglot==30.22.0
# 依赖只在构建时安装，任务环境不保留pip/ensurepip或用户级自动包路径。
RUN python -m pip uninstall -y pip && rm -rf /usr/local/lib/python3.12/ensurepip
COPY guest.py /opt/guest.py
RUN mkdir -p /input /output /tmp /work && chmod 755 /opt/guest.py
ENV MPLBACKEND=Agg OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1
ENTRYPOINT ["python", "/opt/guest.py"]
