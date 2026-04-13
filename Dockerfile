FROM python:3.11-slim

WORKDIR /app

# Install ping for health checks
RUN apt-get update && apt-get install -y \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose syslog port + web dashboard
EXPOSE 514/udp
EXPOSE 8080

CMD ["python", "rbrcs_app_standalone.py"]
