import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
worker_class = "gthread"
threads = int(os.environ.get("WEB_THREADS", "4"))
timeout = 30
graceful_timeout = 30
max_requests = 2000
max_requests_jitter = 200
accesslog = "-"
errorlog = "-"
