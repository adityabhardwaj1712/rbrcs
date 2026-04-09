FROM python:3.11-slim

WORKDIR /app

# Install ping for health checks
RUN apt-get update && apt-get install -y \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose web dashboard port and syslog port
EXPOSE 5000
EXPOSE 514/udp

CMD ["python", "app.py"]
