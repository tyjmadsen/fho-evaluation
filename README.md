# FHO Evaluation Tool

A web application for evaluating Flood Hazard Outlook (FHO) data.

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

## Docker Setup

### Prerequisites
- Docker Desktop installed
- At least 4GB of RAM available for Docker
- The following data files in the project root:
  - `fho_all.gpkg`
  - `LSRs_flood_allYears.gpkg`
  - `flood_warnings_all.gpkg`

### Running with Docker

1. Build the Docker container:
```bash
docker-compose -f docker/docker-compose.yml build
```

2. Start the application:
```bash
docker-compose -f docker/docker-compose.yml up
```

3. Access the application:
- Open your web browser and navigate to `http://localhost:5000`
- The application will take a few minutes to load the data files initially

### Docker Configuration

The Docker setup includes:
- Optimized Gunicorn configuration for better performance
- Resource limits (2 CPUs, 4GB RAM)
- Read-only volume mounts for data files
- Health checks and automatic restarts

### Troubleshooting

If you encounter issues:
1. Check Docker Desktop is running
2. Verify data files are in the correct location
3. Check Docker logs for errors:
```bash
docker-compose -f docker/docker-compose.yml logs
```

4. If the application fails to start:
```bash
docker-compose -f docker/docker-compose.yml down
docker-compose -f docker/docker-compose.yml up --build
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

2. Activate the virtual environment:
   ```bash
   # On Windows
   .\venv\Scripts\activate
   # On macOS/Linux
   source venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run the application:
   ```bash
   python app.py
   ```

5. Access the application:
   - Open your web browser and navigate to `http://localhost:5000`
   - The application will take a few minutes to load the data files initially

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
     - Modify port in docker/docker-compose.yml
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

## Running the Application

### Option 1: Local Development
1. Navigate to the project directory:
   ```bash
   cd fho-evaluation
   ```

2. Run the application:
   ```bash
   python app.py
   ```

3. Access the application:
   Open your web browser and navigate to: http://localhost:5000

### Option 2: Docker Deployment
1. Navigate to the docker directory:
   ```bash
   cd docker
   ```

2. Build and run with Docker Compose:
   ```bash
   docker-compose up --build
   ```

3. Access the application:
   Open your web browser and navigate to: http://localhost:5000

### Notes
- The application requires three data files that will be downloaded automatically when needed:
  - `fho_all.gpkg` (~1.3GB)
  - `LSRs_flood_allYears.gpkg` (~8.6MB)
  - `flood_warnings_all.gpkg` (~11MB)
- These files are not included in the repository due to their size
- The download script will check if the files exist and download them if needed 