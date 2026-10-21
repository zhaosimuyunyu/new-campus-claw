FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn docker
COPY server.py .
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8200"]
