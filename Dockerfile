FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --uid 10001 naviqra && install -d -o naviqra /app/data
COPY station.py inventory.py cameras.py commander.py backup.py ./
COPY migrations migrations
COPY templates templates
COPY static static
USER naviqra
CMD ["waitress-serve", "--listen=0.0.0.0:8080", "--call", "station:create_app"]
