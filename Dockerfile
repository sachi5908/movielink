FROM python:3.11.4-slim

WORKDIR /app

# Install system dependencies for Chromium + Playwright
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libdbus-glib-1-2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium
RUN playwright install --with-deps chromium

# Copy app files
COPY . .

# Expose port for health check server
EXPOSE 8080

# Start the bot
CMD ["python", "main.py"]
