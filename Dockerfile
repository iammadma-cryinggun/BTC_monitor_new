FROM python:3.11-slim

WORKDIR /app

# install deps first (better layer caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# copy project
COPY . /app

# state/notification logs persist here (mount a Zeabur volume at /app/data)
VOLUME ["/app/data"]

# long-running monitor + live web dashboard on a single port.
# Zeabur injects PORT; web server binds 0.0.0.0:$PORT (falls back to 8080).
CMD ["sh", "-c", "exec python monitor.py --web --host 0.0.0.0 --port ${PORT:-8080}"]
