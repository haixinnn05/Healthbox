# Hosted public demo only: simulated dispenser and cameras (see PUBLIC_DEMO in README).
FROM python:3.13-slim
WORKDIR /srv/healthbox
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
ENV PUBLIC_DEMO=1 PYTHONUNBUFFERED=1
RUN useradd --create-home healthbox && mkdir -p data && chown healthbox data
USER healthbox
CMD ["python", "-m", "app.main"]
