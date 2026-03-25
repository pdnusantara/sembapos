# Dipanggil: gunicorn -c deploy/gunicorn.conf.py wsgi:app
# Direktori kerja harus root proyek (berisi wsgi.py).

bind = "127.0.0.1:8000"
workers = 2
threads = 1
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
wsgi_app = "wsgi:app"
