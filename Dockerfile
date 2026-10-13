FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
RUN useradd -r -u 10001 bot && mkdir /data && chown bot /data
USER bot
CMD ["python", "-m", "bot.main"]
