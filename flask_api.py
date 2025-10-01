#!/usr/bin/env python3
"""
Flask API for data exchange between a MongoDB backend and Grafana visualizations.

This service exposes endpoints for:
- Fetching time series sensor readings filtered by tags, date range, and sensor/area.
- Retrieving linked article metadata (title, URL, publication date, tags for the tables).
- Computing average readings for defined periods (2024, 2025).
- Aggregating and visualizing tag distributions (pie chart).

Typical use case involves a dashboard where sensor measurements and related news articles
are jointly explored by area or sensor granularity, with support for multi-tag filtering.
"""

import math
import os

from flask import Flask, request, jsonify
from pymongo import MongoClient
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask_cors import CORS

app = Flask(__name__)

CORS(app, resources={r"/api/*": {"origins": "*"}})

load_dotenv()

# MongoDB configuration
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "article_database")
ARTICLE_COLLECTION = os.environ.get("ARTICLE_COLLECTION", "articles")
SENSOR_DATA_COLLECTION = os.environ.get("SENSOR_DATA_COLLECTION", "sensor_readings")

def get_mongo_client():
    client = MongoClient(MONGO_URI)
    return client

def get_database():
    client = get_mongo_client()
    return client[DATABASE_NAME]

def get_article_collection():
    database = get_database()
    return database[ARTICLE_COLLECTION]

def get_sensor_data_collection():
    database = get_database()
    return database[SENSOR_DATA_COLLECTION]

# Area to sensor_id map
AREA_SENSOR_MAP = {
    "All": [
        "101609","121199","121529","101589","14857",
        "30673","199745","121535","199165","101611",
        "199161","23759","133608","199149","202573",
        "202603","101537","56113","202609",
        "1672","1566","56491","172993","23837","1006"
    ],
    
    "South": ["101609","121199","121529","14857","101589"],
    
    "Center": [
        "30673","199745","101611","199161",
        "121535","199165","23759","133608",
        "199149","202573","202603","101537"
    ],
    
    "North": ["1672","1566","56491","172993","23837","1006"]
}

SPECIAL_WEATHER_TAG = "Καιρικά και Φυσικά Φαινόμενα"


# ================================ HELPERS ======================================
@app.get("/health")
def health():
    return {"status": "ok"}

def parse_grafana_timestamp(ts_ms_string):
    """
    Parses a Grafana timestamp string (milliseconds since epoch) into a UTC datetime object.
    Returns None if parsing fails.
    """
    if not ts_ms_string:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms_string) / 1000.0, tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def parse_list(s):
    """
    Parses a Grafana multi-value string into a list.
    
    Handles values wrapped in { }, comma-separated, and optionally quoted.
    Returns an empty list for 'all' or empty input.
    """
    if not s:
        return []
    s = s.strip()
    if s.startswith('{') and s.endswith('}'):
        s = s[1:-1]
    if s.lower() == 'all':
        return []
    parts = [p.strip() for p in s.split(',')]
    cleaned = []
    for p in parts:
        if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
            p = p[1:-1]
        cleaned.append(p)
    return cleaned

def compute_overall_average(cursor):
    """
    Computes the average of all numeric 'values' fields across documents in a MongoDB cursor.
    Ignores NaNs and empty value lists.
    """
    total = 0.0
    count = 0
    for doc in cursor:
        vals = list((doc.get("readings") or {}).values())
        for v in vals:
            if isinstance(v, (int, float)) and not math.isnan(v):
                total += v
                count += 1
    return total / count if count > 0 else 0.0

def _find_articles(sensor_ids: list[str] | None,
                primary_tags: list[str],
                secondary_tags: list[str],
                start_dt: datetime | None,
                end_dt:   datetime | None):
    """
    Finds articles that match:
        - Any of the given sensor IDs (if provided),
        - Any of the provided primary or secondary tags,
        - At least one date within the given datetime range.
    
    Returns a list of article dictionaries with keys: title, url, pub_datetime, primary_tag, secondary_tag, and dates (as strings).
    """
    clauses = []
    
    # 1) Sensor filter (skip for weather articles – they have no “sensors” field)
    if sensor_ids is not None:
        clauses.append({"sensors": {"$in": sensor_ids}})
    
    # 2) Tag filters
    if primary_tags:
        clauses.append({"primary_tag": {"$in": primary_tags}})
    if secondary_tags:
        clauses.append({"secondary_tag": {"$in": secondary_tags}})
    
    query = {"$and": clauses} if clauses else {}
    
    # 3) Clamp to dashboard time range
    if start_dt or end_dt:
        dt_clause = {}
        if start_dt:
            dt_clause["$gte"] = start_dt
        if end_dt:
            dt_clause["$lte"] = end_dt
        query["dates"] = {"$elemMatch": dt_clause}
    
    # Fetch unique articles, newest first
    rows = []
    for art in get_article_collection().find(
            query,
            {"url": 1, "title": 1, "dates": 1, "pub_datetime": 1, "primary_tag": 1, "secondary_tag": 1}
        ).sort("pub_datetime", -1):
        
        url      = art.get("url")
        title    = art.get("title", "")
        pub_dt   = art.get("pub_datetime")
        primary_tag = art.get("primary_tag")
        secondary_tag = art.get("secondary_tag")
        dates = art.get("dates")
        
        if isinstance(pub_dt, datetime):
            pub_datetime = pub_dt.strftime("%Y-%m-%d %H:%M")
        
        rows.append({
            "title":        title,
            "url":          url,
            "pub_datetime": pub_datetime,
            "primary_tag": primary_tag,
            "secondary_tag": secondary_tag,
            "dates": [d.strftime("%Y-%m-%d") for d in dates if isinstance(d, datetime)]
        })
    return rows


# ================================ SENSOR-SCOPED ================================


# Panel 1: Timeseries readings for individual sensors that satisfy the filters
@app.route('/api/filtered_readings')
def get_filtered_readings():
    """
    Returns a time series of average sensor readings for a specific sensor,
    optionally filtered by primary/secondary tags and a time window.
    
    Readings are linked to articles via overlapping dates between the article's
    'dates' field (i.e., affected dates) and the reading timestamps.
    """
    sensor_name   = request.args.get('sensor')
    primary_str   = request.args.get('primary_tag',   '')
    secondary_str = request.args.get('secondary_tag', '')
    from_str      = request.args.get('from')
    to_str        = request.args.get('to')
    
    start_dt = parse_grafana_timestamp(from_str)
    end_dt   = parse_grafana_timestamp(to_str)
    
    # lookup sensor_id
    sensor_doc = get_sensor_data_collection().find_one(
        {"sensor_name": sensor_name}, {"sensor_id":1}
    )
    if not sensor_doc:
        return jsonify([{
            "target": f"{sensor_name} (Sensor ID not found)",
            "datapoints": []
        }])
    sensor_id = sensor_doc["sensor_id"]
    
    primary_tags   = parse_list(primary_str)
    secondary_tags = parse_list(secondary_str)
    is_weather     = SPECIAL_WEATHER_TAG in primary_tags
    
    # build article‐date filter (or just time window)
    if not (primary_tags or secondary_tags):
        # no tag filters → time‐range only
        date_q = {}
        if start_dt: date_q["$gte"] = start_dt
        if end_dt:   date_q["$lte"] = end_dt
        sensor_q = {"sensor_id": sensor_id}
        if date_q: sensor_q["date"] = date_q
    else:
        clauses = []
        if is_weather:
            clauses.append({"primary_tag": SPECIAL_WEATHER_TAG})
        non_weather = [t for t in primary_tags if t != SPECIAL_WEATHER_TAG]
        if non_weather or secondary_tags:
            c = {"sensors": sensor_id}
            if non_weather:
                c["primary_tag"]   = {"$in": non_weather}
            if secondary_tags:
                c["secondary_tag"] = {"$in": secondary_tags}
            clauses.append(c)
        
        # collect article dates
        article_dates = []
        for art in get_article_collection().find({"$or": clauses}, {"dates":1}):
            for d in art.get("dates", []):
                if isinstance(d, datetime):
                    article_dates.append(d.replace(tzinfo=timezone.utc))
        article_dates = sorted(set(article_dates))
        # no match?
        if not article_dates:
            return jsonify([{
                "target": f"{sensor_name} (No events match selected filters)",
                "datapoints": []
            }])
        # clamp to Grafana window
        if start_dt:
            article_dates = [d for d in article_dates if d >= start_dt]
        if end_dt:
            article_dates = [d for d in article_dates if d <= end_dt]
        sensor_q = {"sensor_id": sensor_id, "date": {"$in": article_dates}}
    
    # fetch & average
    try:
        docs = get_sensor_data_collection().find(sensor_q)
        datapoints = []
        for doc in docs:
            vals = list((doc.get("readings") or {}).values())
            if not vals: continue
            avg = sum(vals) / len(vals)
            if math.isnan(avg): avg = 0.0
            
            dt = doc.get("date")
            if not isinstance(dt, datetime):
                continue
            ts = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
            
            datapoints.append([avg, ts])
        datapoints.sort(key=lambda x: x[1])
        return jsonify([{
            "target": f"{sensor_name} (P:{primary_str or 'All'} / S:{secondary_str or 'All'})",
            "datapoints": datapoints
        }])
    except Exception as e:
        return jsonify({"error": f"Failed to fetch sensor data: {e}"}), 500


# Panel 2: Show the relevant articles that are connected to the readings shown in Panel 1
@app.route("/api/sensor_article_urls")
def sensor_article_urls():
    """
    Returns a list of articles related to a specific sensor,
    filtered by tags and time range as provided by Grafana.
    """
    sensor_name   = request.args.get("sensor")
    primary_tags  = parse_list(request.args.get("primary_tag",   ""))
    secondary_tags= parse_list(request.args.get("secondary_tag", ""))
    start_dt      = parse_grafana_timestamp(request.args.get("from"))
    end_dt        = parse_grafana_timestamp(request.args.get("to"))
    
    # Resolve sensor_id from the human-readable name
    doc = get_sensor_data_collection().find_one({"sensor_name": sensor_name}, {"sensor_id": 1})
    if not doc:
        return jsonify({"error": f"Sensor '{sensor_name}' not found."}), 400
    sensor_id = doc["sensor_id"]
    
    # Special case: weather articles are not linked to specific sensors
    if SPECIAL_WEATHER_TAG in primary_tags:
        sensor_filter = None
    else:
        sensor_filter = [sensor_id]
    
    articles = _find_articles(sensor_filter, primary_tags, secondary_tags, start_dt, end_dt)
    return jsonify(articles)


# ================================ AREA-SCOPED =================================


# Panel 3: Timeseries readings for areas that satisfy the filters
@app.route('/api/area_filtered_readings')
def get_area_filtered_readings():
    """
    Returns average daily readings for all sensors in a selected area (e.g., Center),
    filtered by tags and/or time window.
    
    Readings are linked to articles via overlapping dates between the article's
    'dates' field (i.e., affected dates) and the reading timestamps.
    """
    area_str      = request.args.get('area', 'All')
    primary_str   = request.args.get('primary_tag',   '')
    secondary_str = request.args.get('secondary_tag', '')
    from_str      = request.args.get('from')
    to_str        = request.args.get('to')
    
    sensor_ids = AREA_SENSOR_MAP.get(area_str, AREA_SENSOR_MAP['All'])
    if not sensor_ids:
        return jsonify([{
            "target": f"{area_str} (No sensors configured)",
            "datapoints": []
        }])
    
    start_dt = parse_grafana_timestamp(from_str)
    end_dt   = parse_grafana_timestamp(to_str)
    
    primary_tags   = parse_list(primary_str)
    secondary_tags = parse_list(secondary_str)
    is_weather     = SPECIAL_WEATHER_TAG in primary_tags
    
    # build article‐date filter
    if not (primary_tags or secondary_tags):
        # no tag filters → time window only
        date_filter = {}
        if start_dt: date_filter["$gte"] = start_dt
        if end_dt:   date_filter["$lte"] = end_dt
    else:
        clauses = []
        if is_weather:
            clauses.append({"primary_tag": SPECIAL_WEATHER_TAG})
        non_weather = [t for t in primary_tags if t != SPECIAL_WEATHER_TAG]
        if non_weather or secondary_tags:
            c = {"sensors": {"$in": sensor_ids}}
            if non_weather:
                c["primary_tag"]   = {"$in": non_weather}
            if secondary_tags:
                c["secondary_tag"] = {"$in": secondary_tags}
            clauses.append(c)
        
        article_dates = []
        for art in get_article_collection().find({"$or": clauses}, {"dates":1}):
            for d in art.get("dates", []):
                if isinstance(d, datetime):
                    article_dates.append(d.replace(tzinfo=timezone.utc))
        article_dates = sorted(set(article_dates))
        if not article_dates:
            return jsonify([{
                "target": f"{area_str} (No events match selected filters)",
                "datapoints": []
            }])
        if start_dt:
            article_dates = [d for d in article_dates if d >= start_dt]
        if end_dt:
            article_dates = [d for d in article_dates if d <= end_dt]
        if not article_dates:
            return jsonify([{
                "target": f"{area_str} (No events in selected time window)",
                "datapoints": []
            }])
        date_filter = {"$in": article_dates}
    
    # query sensor_readings for that area
    q = {"sensor_id": {"$in": sensor_ids}}
    if primary_tags or secondary_tags:
        q["date"] = date_filter
    else:
        # only time window
        if start_dt or end_dt:
            q["date"] = {}
            if start_dt: q["date"]["$gte"] = start_dt
            if end_dt:   q["date"]["$lte"] = end_dt
    
    # fetch & compute daily averages across sensors
    try:
        cursor = get_sensor_data_collection().find(q, {"date":1, "readings":1})
        daily_acc = defaultdict(list)
        for doc in cursor:
            vals = list((doc.get("readings") or {}).values())
            if not vals: continue
            avg = sum(vals) / len(vals)
            if math.isnan(avg): avg = 0.0
            dt = doc["date"].replace(tzinfo=timezone.utc)
            daily_acc[dt].append(avg)
        
        datapoints = []
        for dt in sorted(daily_acc):
            overall = sum(daily_acc[dt]) / len(daily_acc[dt])
            ts = int(dt.timestamp() * 1000)
            datapoints.append([overall, ts])
        
        return jsonify([{
            "target": f"{area_str} (P:{primary_str or 'All'} / S:{secondary_str or 'All'})",
            "datapoints": datapoints
        }])
    except Exception as e:
        return jsonify({"error": f"Failed to fetch area data: {e}"}), 500


# Panel 4: Show the relevant articles that are connected to the readings shown in Panel 3
@app.route("/api/area_article_urls")
def area_article_urls():
    """
    Returns a list of articles related to a specific area,
    filtered by tags and time range as provided by Grafana.
    """
    area          = request.args.get("area", "All")
    primary_tags  = parse_list(request.args.get("primary_tag",   ""))
    secondary_tags= parse_list(request.args.get("secondary_tag", ""))
    start_dt      = parse_grafana_timestamp(request.args.get("from"))
    end_dt        = parse_grafana_timestamp(request.args.get("to"))
    
    sensor_ids = AREA_SENSOR_MAP.get(area, AREA_SENSOR_MAP["All"])
    
    # Weather again: ignore sensor filter
    sensor_filter = None if SPECIAL_WEATHER_TAG in primary_tags else sensor_ids
    
    articles = _find_articles(sensor_filter, primary_tags, secondary_tags, start_dt, end_dt)
    return jsonify(articles)


# ===================== ANALYTICS ======================


# Average over all readings in 2024
@app.route('/api/average_2024')
def average_2024():
    """
    Returns the average value of all sensor readings recorded during the year 2024.
    Output is formatted for Grafana as a single timestamped datapoint.
    """
    # Define the 2024 date window (UTC midnight).
    start_2024 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    start_2025 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    
    query = {
        "date": {
            "$gte": start_2024,
            "$lt": start_2025
        }
    }
    
    cursor = get_sensor_data_collection().find(query, {"readings": 1})
    avg_value = compute_overall_average(cursor)
    
    now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    return jsonify([{
        "target": "Average 2024",
        "datapoints": [[round(avg_value, 2), now_ts]]
    }])


# Average over all readings in 2025. Gets updated as the year progresses
@app.route('/api/average_2025')
def average_2025():
    """
    Returns the average value of all sensor readings recorded during the year 2025 (up to the present).
    Output is formatted for Grafana as a single timestamped datapoint.
    """
    start_2025 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    start_2026 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    
    query = {
        "date": {
            "$gte": start_2025,
            "$lte": start_2026
        }
    }
    
    cursor = get_sensor_data_collection().find(query, {"readings": 1})
    avg_value = compute_overall_average(cursor)
    
    now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    return jsonify([{
        "target": "Average 2025",
        "datapoints": [[round(avg_value, 2), now_ts]]
    }])


# Shows the distribution of primary tags for the selected period
@app.route("/api/primary_tag_piechart")
def primary_tag_piechart():
    """
    Returns the top 4 primary tags (by article frequency) within a selected time range.
    Output is formatted as percentage shares suitable for a pie chart visualization.
    """
    start_dt = parse_grafana_timestamp(request.args.get("from"))
    end_dt   = parse_grafana_timestamp(request.args.get("to"))
    
    if not (start_dt or end_dt):
        return jsonify({"error": "Missing time window"}), 400
    
    # Build date range query
    date_filter = {}
    if start_dt: date_filter["$gte"] = start_dt
    if end_dt:   date_filter["$lte"] = end_dt
    
    query = {
        "dates": {"$elemMatch": date_filter}
    }
    
    # Count tag occurrences
    tag_counts = defaultdict(int)
    total_articles = 0
    
    for art in get_article_collection().find(query, {"primary_tag": 1}):
        tag = art.get("primary_tag")
        tag_counts[tag] += 1
        total_articles += 1
    
    if total_articles == 0:
        return jsonify([])
    
    # Get top 4 tags by count
    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:4]
    
    # Convert to percentage
    result = [
        {
            "label": tag,
            "value": round((count / total_articles) * 100, 2)
        }
        for tag, count in top_tags
    ]
    
    return jsonify(result)


@app.route('/')
def index():
    return jsonify({'message': 'Welcome to the API!'})

if __name__ == '__main__':
    app.run(port=5050)