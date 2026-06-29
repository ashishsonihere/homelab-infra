FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Default: run as an RQ worker. Seed with:  docker exec mr-worker python reddit_worker.py seed
CMD ["python", "reddit_worker.py", "worker"]
