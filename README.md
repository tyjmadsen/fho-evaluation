# FHO IBW Validation Tool

A web application for validating Impact-Based Warning (IBW) forecasts against Flash Flood Warnings (FFWs).

## Features

- Interactive map display of FFWs and FHO forecast areas
- Verification statistics calculation
- Support for Considerable and Catastrophic impact levels
- Quick selection of high-impact events
- Customizable date and time period filtering

## Setup

### Prerequisites

- Python 3.9+
- Docker (optional)

### Local Development

1. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Run the application:
```bash
python app.py
```

### Using Docker

1. Build and run using Docker Compose:
```bash
docker-compose up --build
```

The application will be available at http://localhost:5000

## Development

- The main application is in `app.py`
- Templates are in the `templates` directory
- Static files are in the `static` directory

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request 