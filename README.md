# Article & Sensor Analytics Platform

This project correlates local news events with air quality sensor data. It includes a full scraping and enrichment pipeline, a sensor readings importer, and a Flask API designed for integration with Grafana dashboards.

## Features

- 📰 Article scraping and enrichment using LLMs
- 📍 Geolocation and nearest-sensor tagging via Google Maps APIs
- 🌫️ Automated scraping of air quality sensor data (PurpleAir)
- 📊 REST API for time series data, article metadata, tag distribution
- 🧩 MongoDB backend, Grafana-compatible output

---

## Components

### `articles_pipeline.py`
- Scrapes local news articles
- Enriches with LLMs (description, relevancy, tags, dates)
- Geocodes locations and assigns to nearby sensors
- Saves enriched articles to MongoDB

### `sensor_readings_pipeline.py`
- Uses Selenium to download previous-day air quality sensor readings from PurpleAir
- Parses and averages CSV data
- Inserts daily readings into MongoDB

### `flask_api.py`
- Serves data to Grafana dashboards via REST API
- Supports filtering by area, sensor, date range, tags
- Includes endpoints for:
  - Filtered time series data
  - Relevant article tables
  - Tag pie charts
  - Year-based averages (2024, 2025)

---

## Setup

### 1. Clone the Repository

```bash
git clone https://github.com/p-tsitsekidis/article-sensor-analytics.git
cd article-sensor-analytics
```

### 2. Create a Virtual Environment

```bash
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Set Up Environment Variables

Create a `.env` file using the provided template:

```bash
cp .env.example .env
```

Update the file with your actual configuration:
- MongoDB URI and collection names
- LM Studio URL and model IDs
- Google API Key for Places and Geocoding

---

## Grafana Integration

The Flask API is designed to power a multi-panel Grafana dashboard:

- `/api/filtered_readings`: Sensor-level time series with tag filtering  
- `/api/area_filtered_readings`: Area-level time series  
- `/api/sensor_article_urls`: Articles relevant to a sensor  
- `/api/area_article_urls`: Articles relevant to an area  
- `/api/primary_tag_piechart`: Pie chart of tag distribution  
- `/api/average_2024` and `/api/average_2025`: Yearly reading averages  

---

## Requirements

- Python 3.10+
- MongoDB running locally or remotely
- Chrome WebDriver (for Selenium)
- LM Studio (for local LLM completions) — https://lmstudio.ai/
- Google Cloud API access (Places + Geocoding) — https://map.purpleair.com/

See `requirements.txt` for the full Python package list.

---

## License

This project is licensed under the MIT License.