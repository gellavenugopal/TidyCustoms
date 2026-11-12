FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8000
# Apply migrations, then serve. 2 workers x 4 threads is plenty for a small team.
CMD ["sh", "-c", "python -m eoubond.cli upgrade && exec gunicorn eoubond.wsgi:app --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --access-logfile -"]
