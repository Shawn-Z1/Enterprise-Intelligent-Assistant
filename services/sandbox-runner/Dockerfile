FROM python:3.12.12-slim-bookworm
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt docker==7.1.0
COPY server.py /app/server.py
COPY recover_node.py /app/recover_node.py
WORKDIR /app
ENTRYPOINT ["uvicorn","server:app","--host","0.0.0.0","--port","8091"]
