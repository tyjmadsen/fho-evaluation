# FHO IBW Validation Tool

A web application for validating Impact-Based Warning (IBW) forecasts against Flash Flood Warnings (FFWs).

## Features

- Interactive map display of FFWs and FHO forecast areas
- Verification statistics calculation
- Support for Considerable and Catastrophic impact levels
- Quick selection of high-impact events
- Customizable date and time period filtering

## System Requirements

### Minimum Requirements
- 4GB RAM (8GB recommended for large datasets)
- 4GB free disk space
- Docker Desktop (if using Docker deployment)
- Internet connection for initial data download

### Supported Operating Systems
- Windows 10/11
- macOS 10.15 or later
- Linux (Ubuntu 20.04 or later recommended)

## Quick Start with Docker

1. Install Docker:
   - Windows/macOS: Download and install [Docker Desktop](https://www.docker.com/products/docker-desktop)
   - Linux: Install Docker Engine and Docker Compose:
     ```bash
     # Ubuntu/Debian
     sudo apt-get update
     sudo apt-get install docker.io docker-compose
     ```

2. Clone the repository:
   ```bash
   git clone https://github.com/tyjmadsen/fho-evaluation.git
   cd fho-evaluation
   ```

3. **IMPORTANT**: Download required data files first:
   ```bash
   python download_data.py
   ```
   This will download three required files:
   - fho_all.gpkg (~1.3GB)
   - LSRs_flood_allYears.gpkg (~8.6MB)
   - flood_warnings_all.gpkg (~11MB)

4. Build and run with Docker Compose:
   ```bash
   docker-compose up --build
   ```

5. Access the application:
   Open your web browser and navigate to: http://localhost:5000

## Docker Deployment Guide

### Configuration

1. Port Configuration:
   - Default port is 5000
   - To change the port, modify `docker-compose.yml`:
     ```yaml
     ports:
       - "YOUR_PORT:5000"
     ```

2. Volume Mounts:
   - Data files are mounted from the host system
   - Ensure data files are in the correct location before building

3. Environment Variables:
   - Set in `docker-compose.yml` or use a `.env` file
   - Available variables:
     - `FLASK_ENV`: Set to `production` or `development`
     - `PORT`: Application port (default: 5000)

### Production Deployment

For production deployment, additional steps are recommended:

1. Use a production-ready web server:
   ```dockerfile
   # In Dockerfile
   RUN pip install gunicorn
   CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
   ```

2. Enable HTTPS:
   - Set up a reverse proxy (nginx recommended)
   - Configure SSL certificates

3. Set secure configurations:
   ```yaml
   # docker-compose.prod.yml
   version: '3.8'
   services:
     web:
       environment:
         - FLASK_ENV=production
         - FLASK_APP=app.py
       restart: unless-stopped
       # Add your production configs here
   ```

## Data Files

The application requires three data files that are not included in the repository due to size:
- `fho_all.gpkg` (~1.3GB): FHO forecast data
- `LSRs_flood_allYears.gpkg` (~8.6MB): LSR verification data
- `flood_warnings_all.gpkg` (~11MB): Flood Warning data

These files are automatically downloaded from Google Drive when you run `download_data.py`. The script will:
- Check if files already exist to avoid re-downloading
- Show download progress for each file
- Verify the files are downloaded successfully

## Manual Setup (without Docker)

### Prerequisites

- Python 3.9+
- pip (Python package installer)
- Virtual environment tool (venv or conda)

### Steps

1. Create a virtual environment:
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On Unix/macOS:
   source venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Download the data files:
   ```bash
   python download_data.py
   ```

4. Run the application:
   ```bash
   python app.py
   ```

## Troubleshooting

### Common Issues

1. **Missing Data Files**
   - Error: "Could not read layer..."
   - Solution: Run `python download_data.py` to download required data files
   - Alternative: Manually download from Google Drive and place in application root

2. **Port Already in Use**
   - Error: "Port 5000 is already in use"
   - Solution: 
     - Stop other services using port 5000
     - Modify port in docker-compose.yml
     - Or run with different port: `docker-compose up -p 5001:5000`

3. **Docker Memory Issues**
   - Error: "Out of memory" or container stops
   - Solutions:
     - Increase Docker memory limit in Docker Desktop settings
     - Recommended: At least 4GB for Docker
     - For Windows: Configure in Docker Desktop > Settings > Resources
     - For Linux: Set in daemon.json or use runtime flags

4. **Download Issues**
   - Error: "Failed to download file..."
   - Solutions:
     - Check internet connection
     - Verify Google Drive access permissions
     - Try manual download from Google Drive
     - Contact repository maintainers for direct file access

5. **Docker Build Fails**
   - Error: "Build failed" or dependency issues
   - Solutions:
     - Ensure Docker is up to date
     - Clear Docker cache: `docker system prune -a`
     - Check system resources
     - Verify all required files are present

### Need Help?

If you encounter any issues:
1. Check that all required data files are present
2. Verify Docker is running (if using Docker)
3. Check the application logs for error messages
4. Create an issue on GitHub with error details

## Development

- The main application is in `app.py`
- Templates are in the `templates` directory
- Static files are in the `static` directory
- Data processing scripts in root directory

### Development Environment

1. Set up pre-commit hooks:
   ```bash
   pip install pre-commit
   pre-commit install
   ```

2. Run tests:
   ```bash
   python -m pytest tests/
   ```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details. 