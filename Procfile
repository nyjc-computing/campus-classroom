web: gunicorn --bind "[::]:${PORT:-8080}" --workers 2 --threads 8 --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile - main:app
