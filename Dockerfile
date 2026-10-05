FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8080
CMD ["sh", "-c", "gunicorn -w 2 --threads 4 -t 200 -b 0.0.0.0:${PORT:-8080} server:app"]
