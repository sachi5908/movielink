# Use an official Python runtime as a parent image
# Using a specific version is good practice for stability
FROM python:3.11.4-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies required by Playwright and its browsers
# This is the crucial step that fixes the "Host system is missing dependencies" error
# We run this as root before installing python packages
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
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# Use --no-cache-dir to reduce image size
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
# This command now runs in an environment with all the necessary system dependencies
RUN playwright install --with-deps

# Copy the rest of your application's code into the container
COPY . .

# Command to run your application
# This will be executed when the container starts
CMD ["python", "main.py"]
