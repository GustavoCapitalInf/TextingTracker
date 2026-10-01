"""Gunicorn configuration for the dedicated Mac mini (Unix only)."""

import os

bind = "127.0.0.1:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
timeout = 60
graceful_timeout = 30
keepalive = 5
worker_class = "sync"
preload_app = False
daemon = False
pidfile = "/Library/PhoneTracker/data/gunicorn.pid"
accesslog = None  # Apache owns access logs. Do not log uploaded content.
errorlog = "-"
capture_output = True
forwarded_allow_ips = "127.0.0.1"

