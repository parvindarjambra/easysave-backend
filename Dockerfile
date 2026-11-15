FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir flask yt-dlp
WORKDIR /app
COPY app.py .
EXPOSE 8080
CMD ["python", "app.py"]
