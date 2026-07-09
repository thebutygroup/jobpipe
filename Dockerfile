# jobpipe — web + scheduler share this image (like bot + poller in the stack).
# The submitter uses a separate Playwright image (see docker-compose service).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --shell /bin/bash appuser
WORKDIR /app

# Install deps first for layer caching
COPY app/pyproject.toml ./
COPY app/jobpipe ./jobpipe
RUN pip install .

# App code + config templates + scripts
COPY app/ ./

RUN mkdir -p /app/data/screenshots /app/data/browser_profile \
    && chown -R appuser:appuser /app
USER appuser

# Default command is overridden per-service in docker-compose.yml:
#   web       -> gunicorn jobpipe.dashboard.wsgi
#   scheduler -> python -m jobpipe.scheduler
CMD ["python", "-m", "jobpipe.scheduler"]
