FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ws_runner.py wallet.py experiment.py fleet.json ./
# Optional: mount wallet.key at runtime via -v $PWD/wallet.key:/app/wallet.key
CMD ["python3", "-u", "ws_runner.py"]
