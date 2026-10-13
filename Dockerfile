FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY leadgen/*.py leadgen/
COPY leadgen/sources.json leadgen/
COPY leadgen/web_templates/ leadgen/web_templates/
COPY leadgen/web_static/ leadgen/web_static/
COPY supabase/migrations/ supabase/migrations/
RUN mkdir -p /app/leadgen/data && useradd --create-home --uid 10001 leadgen \
    && chown -R leadgen:leadgen /app
USER leadgen
CMD ["python", "-m", "leadgen.app", "bot"]
