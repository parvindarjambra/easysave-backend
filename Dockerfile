FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl nodejs \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY app.py .
EXPOSE 8080
CMD ["python", "app.py"]
HEALTHCHECK CMD curl -f http://localhost:8080/health || exit 1
