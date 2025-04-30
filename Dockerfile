# Use Python 3.9 as base image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libspatialindex-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Create a data directory
RUN mkdir -p /app/data

# Create a script to check and download data files
RUN echo '#!/bin/bash\n\
cd /app\n\
if [ ! -f "fho_all.gpkg" ] || [ ! -f "LSRs_flood_allYears.gpkg" ] || [ ! -f "flood_warnings_all.gpkg" ]; then\n\
    echo "Required data files are missing. Please run the download script first:"\n\
    echo "python download_data.py"\n\
    exit 1\n\
fi\n\
python app.py' > /app/start.sh && chmod +x /app/start.sh

# Expose the port the app runs on
EXPOSE 5000

# Command to run the application
CMD ["./start.sh"] 