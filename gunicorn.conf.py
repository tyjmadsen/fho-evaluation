import multiprocessing

# Bind to all interfaces on port 5000
bind = "0.0.0.0:5000"

# Number of workers = (2 x CPU cores) + 1
workers = (2 * multiprocessing.cpu_count()) + 1
worker_class = 'gthread'
threads = 4
timeout = 600  # Increase timeout for data loading
keepalive = 5
max_requests = 1000
max_requests_jitter = 50
preload_app = True  # Preload the application to share memory between workers

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'

# Performance
worker_connections = 2000
backlog = 2048 