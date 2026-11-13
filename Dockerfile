FROM python:3.10-slim

WORKDIR /app

# System deps for cryptography / tgcrypto
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bot + web listen on this port (main.py / Config.PORT)
ENV PORT=10000
ENV WEB_ONLY=false
EXPOSE 10000

CMD ["python", "main.py"]
