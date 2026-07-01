# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

UNDP Meteorology platform for Thailand. Pulls weather forecast data from Open-Meteo, satellite imagery from NASA HLS, and serves it through a FastAPI backend to a React/Deck.gl map frontend.

## Key Commands

### Infrastructure
```bash
# Start PostgreSQL + PostGIS database (port 5433)
docker compose up -d
docker compose down
```

### Backend API
```bash
# Run FastAPI dev server (port 8001)
uvicorn api:app --reload --port 8001
```

### Pipelines (run from project root)
```bash
# Primary daily pipeline: fetch forecasts + compute heat index + insert alerts for all Thailand districts
python pipeline/weather_forecast_alert_v2.py

# Legacy heatwave alert generator (reads from weather_forecasts, writes to heatwave_alerts)
python heatwave_alert.py

# Seed historical observations from Open-Meteo ERA5 archive (one-time)
python setup_data.py

# NASA HLS satellite pipeline (NDVI + LST for Bangkok bounding box)
python etl_nasa_pipeline.py

# Import population data from JSON
python create_table/population_tables.py --file <population.json>
python create_table/population_tables.py --file <population.json> --truncate
```

### Frontend
```bash
cd undp-map-fe
npm install
npm run dev       # dev server (Vite)
npm run build
npm run lint
```

## Architecture

### Database (PostgreSQL 15 + PostGIS 3.5)
All scripts connect via env vars: `DB_HOST`, `DB_PORT` (default 5433), `DB_NAME` (undp_db), `DB_USER` (admin), `DB_PASSWORD` (secretpassword). Connection pool in `api.py`; direct `psycopg2.connect()` in pipelines.

Key tables:
- `admin_polygons` / `admin_polygons_district` — Administrative boundaries with PostGIS geometry; `country_code = 'THA'` for Thailand districts
- `weather_alerts` — Primary output of the v2 pipeline: heat index, alert level (NORMAL/CAUTION/WARNING/DANGER/EXTREME_DANGER), population, and `percen_previous` (% change vs prior forecast day). Unique on `(gid_2, forecast_date, forecast_run_date)`.
- `weather_forecasts` — Daily forecasts per district (older pipeline)
- `heatwave_alerts` — Simple temperature-threshold alerts from `heatwave_alert.py`
- `weather_observations` — Hourly ERA5 grid data with min-max normalized `temp_nor`/`humidity_nor`
- `grid_points` — 0.1°×0.1° grid covering Southeast Asia
- `pop_district` — Population and density keyed by `district_code` (= `gid_2`)
- `hls_data_points` — Satellite NDVI + LST pixels; `hls_tracking_logs` tracks download status per scene

### Pipeline Flow (v2 — primary)
`pipeline/weather_forecast_alert_v2.py` is the authoritative daily pipeline:
1. Load all Thailand district centroids from `admin_polygons_district`
2. Batch-call Open-Meteo Forecast API (50 districts/batch, 40s delay between batches)
3. Compute mean temp/humidity = (max + min) / 2
4. Compute Heat Index via NOAA Rothfusz formula (`compute_heat_index`)
5. Assign alert level from `HI_THRESHOLDS` (NORMAL/CAUTION/WARNING/DANGER/EXTREME_DANGER)
6. JOIN population from `pop_district` by `gid_2`
7. Compute `percen_previous` = % ΔHI vs previous forecast day (0.0 for baseline day)
8. Truncate today's run then bulk-insert into `weather_alerts`

### API (`api.py`)
FastAPI with two endpoint groups:
- `/api/times` + `/api/weather-grid` — serve historical ERA5 grid data (passes through `filter_to_land` to drop ocean points)
- `/api/heatwave/dates` + `/api/heatwave/alerts` — serve heatwave alert GeoJSON FeatureCollection from `heatwave_alerts` joined with district geometry

### Frontend (`undp-map-fe/`)
React 19 + Vite. Single-component app (`App.jsx`) using Deck.gl `GeoJsonLayer` over MapLibre GL for the map. Calls `http://localhost:8001` (hardcoded in `App.jsx:8`). Color-codes districts by alert level and temperature.

### Geometry Handling
WKT geometry from PostGIS is converted to GeoJSON strings in two places:
- `pipeline/weather_forecast_alert_v2.py` → `wkt_to_geojson()` (pure regex, no deps)
- `transform_temp.py` → uses `shapely` for the same conversion

`filter_land_points.py` loads a Natural Earth land shapefile via `geodatasets` at import time and is used by the API to filter ocean grid points.
