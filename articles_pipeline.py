#!/usr/bin/env python3
"""
Article scraper and enricher

This pipeline automates the process of extracting relevant articles and their metadata,
enriches these articles with the use of LLMs, correlates sensors with articles
that have an event in their close proximity, and saves all this data to the MongoDB database.
"""

import argparse
import logging
import math
import os
import re
import requests
import sys
import unicodedata

from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from pymongo import MongoClient
from readability import Document

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# MongoDB configuration
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "article_database")
ARTICLE_COLLECTION = os.environ.get("ARTICLE_COLLECTION", "articles")

def get_mongo_client():
    return MongoClient(MONGO_URI)

def get_database():
    client = get_mongo_client()
    return client[DATABASE_NAME]

def get_articles_collection():
    db = get_database()
    return db[ARTICLE_COLLECTION]

# LM Studio endpoints and model configuration
CHAT_URL = os.environ.get("CHAT_URL", "http://localhost:1234/v1/chat/completions")
DESCRIPTION_MODEL_ID = os.environ.get("DESCRIPTION_MODEL_ID", "llama-krikri-8b-instruct")
TAG_MODEL_ID = os.environ.get("TAG_MODEL_ID", "qwen2.5-14b-instruct")
HEADERS = {"Content-Type": "application/json"}

# Google API Key for Geocoding and Places APIs
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "OPTIONALLY INSERT IT HERE")

# Threshold distance (km) to consider an event near a sensor
THRESHOLD_KM = 3

# Maximum allowed date range (in days) to prevent oversized crawls
MAX_RECOMMENDED_DAYS = 90

# Timeout in seconds for standard HTTP requests (scraping, geocoding)
REQUEST_TIMEOUT = 10

# Timeout in seconds for LLM calls (can be longer due to text generation)
LLM_REQUEST_TIMEOUT = 300

# ================================ LLM PROMPTS ================================
with open("prompts/description_prompt.txt", "r", encoding="utf-8") as f:
    DESCRIPTION_PROMPT = f.read()
with open("prompts/relevancy_prompt.txt", "r", encoding="utf-8") as f:
    RELEVANCY_PROMPT = f.read()
with open("prompts/location_prompt.txt", "r", encoding="utf-8") as f:
    LOCATION_PROMPT = f.read()
with open("prompts/primary_tag_prompt.txt", "r", encoding="utf-8") as f:
    PRIMARY_TAG_PROMPT = f.read()
with open("prompts/secondary_tags/pollution_events_prompt.txt", "r", encoding="utf-8") as f:
    POLLUTION_EVENTS_PROMPT = f.read()
with open("prompts/secondary_tags/public_events_prompt.txt", "r", encoding="utf-8") as f:
    PUBLIC_EVENTS_PROMPT = f.read()
with open("prompts/secondary_tags/transportation_and_traffic_prompt.txt", "r", encoding="utf-8") as f:
    TRANSPORTATION_AND_TRAFFIC_PROMPT = f.read()
with open("prompts/secondary_tags/weather_and_natural_phenomena_prompt.txt", "r", encoding="utf-8") as f:
    WEATHER_AND_NATURAL_PHENOMENA_PROMPT = f.read()
with open("prompts/date_prompt.txt", "r", encoding="utf-8") as f:
    DATE_PROMPT = f.read()

# Sensor configuration (sensor_id: (latitude, longitude))
SENSORS = {
    "101609": (38.19944, 21.69919),
    "121199": (38.18873, 21.72194),
    "121529": (38.19163, 21.76065),
    "101589": (38.20659, 21.72714),
    "14857":  (38.20011, 21.74378),
    "121547": (38.20851, 21.77319),
    "30673":  (38.2252, 21.7196),
    "199745": (38.22995, 21.73221),
    "121535": (38.23513, 21.74543),
    "199165": (38.23391, 21.75137),
    "101611": (38.24223, 21.73161),
    "199161": (38.23939, 21.73478),
    "23759":  (38.24118, 21.74158),
    "133608": (38.24384, 21.74438),
    "199149": (38.24498, 21.75484),
    "202573": (38.25897, 21.7465),
    "116409": (38.2586, 21.75007),
    "202603": (38.26461, 21.74111),
    "101537": (38.26283, 21.74869),
    "56113":  (38.26517, 21.75695),
    "202609": (38.27636, 21.76044),
    "1672":   (38.28935, 21.77386),
    "1566":   (38.28943, 21.78551),
    "56491":  (38.30436, 21.79483),
    "172993": (38.29702, 21.79753),
    "23837":  (38.29779, 21.8096),
    "1006":   (38.29776, 21.82289)
}

# ============================= EXTRACT ARTICLES =============================


def get_yesterday_date():
    """
    Return yesterday's date as a datetime.date object.
    """
    return (datetime.now() - timedelta(days=1)).date()


def to_midnight(dt: datetime):
    """
    Return *dt* truncated to 00:00:00 (keeps tzinfo if present).
    """
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_article_date(time_tag):
    """
    Parse a <time> tag in 'DD/MM/YYYY, HH:MM' format to a datetime object.
    """
    date_str = time_tag.get_text(strip = True)
    dt = datetime.strptime(date_str, "%d/%m/%Y, %H:%M")
    return dt


def extract_article_urls_for_dates(target_dates):
    """
    Crawl the site and collect the tuples (url, pub_datetime)
    for articles whose publication date is in target_dates.
    """
    base_url = "https://www.thebest.gr/patra-dytiki-ellada"
    page_number = 1
    collected = []
    
    while True:
        # Construct the URL for page 1 or subsequent pages
        url = f"{base_url}/page-{page_number}"
        logger.info(f"Processing page: {url}")
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Error fetching page {url}: {e}")
            break
        
        soup = BeautifulSoup(response.text, "html.parser")
        for aside in soup.find_all("aside", class_="lg-pl-15"):
            aside.decompose()
        
        articles = soup.find_all("article")
        if not articles:
            logger.info("No more articles found on this page.")
            break
        
        for article in articles:
            time_tag = article.find("time")
            if not time_tag:
                continue  # Skip if no time info is available.
            pub_datetime = parse_article_date(time_tag)
            pub_date = pub_datetime.date()
            if pub_date is None:
                continue
            if pub_date in target_dates:
                # Retrieve the article URL from the first <a> tag.
                a_tag = article.find("a", href=True)
                if a_tag:
                    article_url = a_tag["href"]
                    if article_url.startswith("/"):
                        article_url = "https://www.thebest.gr" + article_url
                    collected.append((article_url, pub_datetime))
        
        # Limits the pages searched. (So it does not keep going or get stuck in the first page.)
        if pub_date < min(target_dates):
            logger.info(f"Found all articles within the specified range. Stopping.")
            break
        
        page_number += 1
    
    return collected


def normalize_text(text):
    """
    Normalize text to Unicode NFD form and apply case folding.
    """
    return unicodedata.normalize("NFD", text).casefold()


def clean_text(text):
    """
    Remove all non-word characters from the text.
    """
    return re.sub(r"\W+", "", text)


def extract_article(url, pub_date):
    """
    Fetch the article HTML, extract title and content, and clean it.
    Returns a dict with metadata if relevant, else None.
    """
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        doc = Document(response.text)
        title = doc.title()
        
        if not is_title_relevant(title):
            return None
        
        # ------------------------------------------------------------------
        # Try to extract content from the expected article structure
        # ------------------------------------------------------------------
        article_div = soup.find("div", class_="article-content")
        if article_div:
            header = article_div.find("p", class_="m-0 mb-4 h4")
            header_text = header.get_text(strip=True) if header else ""
            
            body_divs = article_div.find_all(
                "div",
                class_=lambda c: c and "bodypart-text" in c
            )
            
            target_clean = clean_text(normalize_text("Ειδήσεις τώρα"))
            filtered_body_texts = []
            
            for div in body_divs:
                # Skip promo blocks with many links
                if "mt-3" in div.get("class", []) and len(div.find_all('a')) >= 3:
                    continue
                
                paragraphs = div.find_all("p")
                if any(
                    clean_text(normalize_text(p.get_text(strip=True))) == target_clean
                    for p in paragraphs
                ):
                    continue
                
                for p in paragraphs:
                    # Remove paragraphs that are just single links
                    a_tag = p.find("a")
                    if a_tag and p.get_text(strip=True) == a_tag.get_text(strip=True):
                        p.decompose()
                        continue
                    
                    # Remove paragraphs starting with "Ειδήσεις τώρα"
                    if clean_text(normalize_text(p.get_text(strip=True))).startswith(target_clean):
                        p.decompose()
                        continue
                    
                    # Remove sportin.gr mentions
                    for a in p.find_all("a"):
                        if "sportin.gr" in (a.get("href", "").lower() + a.get_text(strip=True).lower()):
                            a.decompose()
                    
                    if not p.get_text(strip=True):
                        p.decompose()
                
                text = div.get_text(" ", strip=True)
                filtered_body_texts.append(text)
            
            # Combine header and body
            article_text = "\n".join([header_text] + filtered_body_texts).strip()
        
        else:
            # Fallback using readability extraction
            article_text = BeautifulSoup(
                doc.summary(), "html.parser"
            ).get_text(separator="\n")
        
        # ------------------------------------------------------------------
        # Final cleaning: trim lines, remove "Ειδήσεις τώρα" cutoff
        # ------------------------------------------------------------------
        lines = [line.strip() for line in article_text.splitlines() if line.strip()]
        article_text = "\n".join(lines)
        
        pattern = re.compile(r"(?i)\bειδησεις\s*τωρα\b")
        match = pattern.search(article_text)
        if match:
            article_text = article_text[:match.start()].strip()
        
        return {
            "url": url,
            "title": title,
            "content": article_text,
            "pub_datetime": pub_date,
        }
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching {url}: {e}")
        return None


def is_title_relevant(title):
    """
    Check if the given article title contains any of the target keywords
    indicating that it is relevant for processing.
    """
    keywords = ["απεργία", "πορεία", "ποιότητα αέρα", "κακή ποιότητα", "πάτρα:", "πάτρα", "πάτρας",
                "25η μαρτίου", "25η μαρτίου:", "εκδήλωση", "εκδηλώσεις", "εκδηλώσεων", "παρέλαση",
                "παρελάσεις", "αγώνας", "αγώνες", "αγώνα", "προμηθέας", "ατμόσφαιρα", "ατμοσφαιρική",
                "ρύπανση", "καιρός", "κακοκαιρία", "καπνός", "καπνού", "πλήθος κόσμου", "βούλιαξε",
                "κατανυκτική", "γήπεδο", "γήπεδα", "έκαψαν", "πυρκαγιές", "φωτιά", "κυκλοφορία", "έρχεται",
                "σωματίδια", "κορωνοϊός", "κορωνοϊού", "εμπρησμός", "κυκλοφορίας", "ντέρμπι", "κάηκαν",
                "διοργάνωση", "τροχαίο", "συναυλία", "μπάσκετ", "ποδόσφαιρο", "εκλογές", "πυρκαγιά",
                "προμηθέα", "εορτή", "γιορτή", "πλήθος", "αιθαλομίχλη", "μάγεψε", "προσέλευση",
                "κόσμου", "γιορτινή", "τόφαλο", "τόφαλος", "πατρών", "περιβάλλον", "πυροσβέστες",
                "πυροσβεστών", "κίνηση", "εορταστικό", "ομιλία", "στους δρόμους", "συλλαλητήριο",
                "πυρκαγιάς", "πορείας", "τροχαίου", "ένταση", "κινητοποίηση", "καρναβάλι", "καρναβαλιού",
                '"πάτρα:', '"πλήθος κόσμου', "προμηθέας:", "ρύποι", "ρύπους", "γιορταστική", "εορταστική",
                "εορτασμός", "επίσκεψη", "επισκέψεις"]
    return any(keyword in title.lower() for keyword in keywords)

# =============================== GEOLOCATION ================================


def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the great-circle distance between two points on Earth (in kilometers).
    """
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(delta_lambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c


def assign_article_to_closest_sensor(article_coord, sensors, threshold=THRESHOLD_KM):
    """
    Assign the article's coordinates to the closest sensor within the threshold distance.
    
    Returns the sensor_id of the closest sensor if within threshold, otherwise None.
    """
    matched_sensors = []
    for sensor_id, (sensor_lat, sensor_lon) in sensors.items():
        distance = haversine_distance(article_coord[0], article_coord[1], sensor_lat, sensor_lon)
        if distance <= threshold:
            matched_sensors.append((sensor_id, distance))
    if not matched_sensors:
        return None
    # Return only the sensor_id of the closest sensor
    return min(matched_sensors, key=lambda x: x[1])[0]


def geocode_address(address, api_key):
    """
    Use Google Geocoding API to convert an address string to (latitude, longitude).
    
    Returns a tuple (lat, lng) if successful, else None.
    """
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": api_key, "language": "en"}
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if response.status_code == 200:
        data = response.json()
        if data.get("status") == "OK" and data.get("results"):
            result = data["results"][0]
            lat = result["geometry"]["location"]["lat"]
            lng = result["geometry"]["location"]["lng"]
            return (lat, lng)
        else:
            logger.warning(f"Geocoding error for '{address}': {data.get('status')}")
    else:
        logger.error(f"HTTP error from Geocoding API: {response.status_code}")
    return None


def get_correct_address_from_places(query, api_key):
    """
    Use Google Places API to find and return a cleaned display name and formatted address.
    
    Returns (display_name, formatted_address) tuple if found, else (None, None).
    """
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress"
    }
    data = {"textQuery": query}
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        result = response.json()
        if "places" in result and len(result["places"]) > 0:
            candidate = result["places"][0]
            display_name = candidate.get("displayName", {}).get("text", "")
            formatted_address = candidate.get("formattedAddress")
            return display_name, formatted_address
        else:
            logger.warning(f"No candidates returned from Places API for: {query}")
    else:
        logger.error(f"Error from Places API: HTTP {response.status_code} - {response.text}")
    return None, None

# =================================== LLM ====================================


def call_llm(prompt, text, model, timeout=LLM_REQUEST_TIMEOUT):
    """
    Calls the local LM Studio server with the given prompt.
    
    Returns the generated content as a string, else None.
    """
    # Call llm through LM Studio's server
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": text}
    ]
    
    payload = {
            "model": model,
            "messages": messages
        }
    
    try:
        response = requests.post(CHAT_URL, headers=HEADERS, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"].strip()
        else:
            return "No response."
    except Exception as e:
        logger.error(f"LLM call error: {e}")
        return None


def generate_description(article):
    """
    Generates a description for the given article content using the description model.
    """
    content = article.get("title", "") + "\n" + article.get("content", "")
    summary = call_llm(DESCRIPTION_PROMPT, content, DESCRIPTION_MODEL_ID)
    return summary


def generate_relevancy(description):
    """
    Determines if the article description is relevant using the tag model.
    
    Returns True if the response is 'σχετικό', else False.
    """
    result = call_llm(RELEVANCY_PROMPT, description, TAG_MODEL_ID)
    return result.strip().lower() == "σχετικό"


def extract_event_location(description):
    """
    Extracts the event location(s) mentioned in the description using the tag model.
    """
    return call_llm(LOCATION_PROMPT, description, TAG_MODEL_ID)


def generate_primary_tag(description):
    """
    Generates the primary tag for the given article description using the tag model.
    """
    return call_llm(PRIMARY_TAG_PROMPT, description, TAG_MODEL_ID)


def generate_secondary_tag(description, primary_tag):
    """
    Generates the secondary tag for the given article description, based on the primary tag.
    Uses the tag model.
    
    Returns None if primary_tag is 'Μη σχετικό'.
    """
    match primary_tag:
        case "Δημόσια Γεγονότα":
            return call_llm(PUBLIC_EVENTS_PROMPT, description, TAG_MODEL_ID)
        case "Καιρικά και Φυσικά Φαινόμενα":
            return call_llm(WEATHER_AND_NATURAL_PHENOMENA_PROMPT, description, TAG_MODEL_ID)
        case "Μεταφορές και Κυκλοφορία":
            return call_llm(TRANSPORTATION_AND_TRAFFIC_PROMPT, description, TAG_MODEL_ID)
        case "Ρύπανση και Περιβαλλοντικά Συμβάντα":
            return call_llm(POLLUTION_EVENTS_PROMPT, description, TAG_MODEL_ID)
        case "Μη σχετικό":
            return None


def extract_date(pub_date, description):
    """
    Extracts affected date(s) mentioned in the description using the tag model.
    """
    message = f"published date: {pub_date}\ndescription: {description}"
    logging.info(f"LLM input message:\n{message}")
    return call_llm(DATE_PROMPT, message, TAG_MODEL_ID)

# ============================= MONGODB AND MAIN =============================


def save_article_to_db(article, collection):
    """Insert the article document into MongoDB."""
    try:
        collection.insert_one(article)
        logger.info(f"Article saved: {article.get('url')}")
    except Exception as e:
        logger.error(f"Error saving article to MongoDB: {e}")


def parse_date(date_str):
    """Parse a date string in DD-MM-YYYY format."""
    return datetime.strptime(date_str, "%d-%m-%Y").date()


def generate_date_range(start_date, end_date):
    """Return a set of dates from start_date to end_date inclusive."""
    delta = end_date - start_date
    return {start_date + timedelta(days=i) for i in range(delta.days + 1)}


def main():
    """
    Entry point for the article scraping and enrichment pipeline.
    Handles CLI parsing, validation, crawling, enrichment, and storage.
    """
    # ----------------------------------------------------------------------
    # Handle CLI arguments
    # ----------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        description="Scrape and enrich news articles between two dates (format: DD-MM-YYYY)."
    )
    parser.add_argument(
        "start_date", nargs="?", help="Start date (DD-MM-YYYY). Defaults to yesterday."
    )
    parser.add_argument(
        "end_date", nargs="?", help="End date (DD-MM-YYYY). Defaults to yesterday."
    )
    args = parser.parse_args()
    
    if args.start_date and args.end_date:
        try:
            start_date = parse_date(args.start_date)
            end_date = parse_date(args.end_date)
        except ValueError:
            logger.error("Invalid date format. Use DD-MM-YYYY.")
            sys.exit(1)
    elif not args.start_date and not args.end_date:
        yesterday = get_yesterday_date()
        start_date = end_date = yesterday
    else:
        logger.error("Either provide both start_date and end_date, or neither.")
        parser.exit(1)
    
    if start_date > end_date:
        logger.error("Start date must be before or equal to end date.")
        sys.exit(1)
    
    delta_days = (end_date - start_date).days + 1
    if delta_days > MAX_RECOMMENDED_DAYS:
        logger.error(
            f"Date range too large ({delta_days} days). Maximum allowed is {MAX_RECOMMENDED_DAYS}."
        )
        sys.exit(1)
    
    if not api_key or api_key == "OPTIONALLY INSERT IT HERE":
        logger.error("Missing or placeholder Google API key. Set GOOGLE_API_KEY env variable.")
        sys.exit(1)
    
    logger.info(
        f"Collecting articles from {start_date} to {end_date} "
        f"({delta_days} day(s))."
    )
    target_dates = generate_date_range(start_date, end_date)
    
    # ----------------------------------------------------------------------
    # Scrape article URLs
    # ----------------------------------------------------------------------
    article_urls = extract_article_urls_for_dates(target_dates)
    if not article_urls:
        logger.error("No articles found in the specified date range.")
        sys.exit(1)
    
    # ----------------------------------------------------------------------
    # Fetch and enrich article content
    # ----------------------------------------------------------------------
    articles = []
    for url, pub_datetime in article_urls:
        article = extract_article(url, pub_datetime)
        if not article:
            continue
        
        logger.info(f"Processing article: {url}")
        article["description"] = generate_description(article)
        articles.append(article)
    
    # ----------------------------------------------------------------------
    # Relevancy filtering
    # ----------------------------------------------------------------------
    relevant_articles = [
        article for article in articles
        if generate_relevancy(article["description"])
    ]
    logger.info(
        f"After relevancy filtering: {len(relevant_articles)} articles remain."
    )
    if not relevant_articles:
        logger.warning("No relevant articles after filtering. Exiting.")
        sys.exit(1)
    
    # ----------------------------------------------------------------------
    # Geolocation and tagging
    # ----------------------------------------------------------------------
    articles_with_sensors = []
    for article in relevant_articles:
        article["location"] = extract_event_location(article["description"])
        logger.info("Article location processed.")
        
        sensors_for_article = set()
        for loc in article["location"].split("/"):
            loc = loc.strip()
            display_name, formatted_address = get_correct_address_from_places(
                loc, api_key
            )
            if not formatted_address:
                logger.error("Failed to retrieve address from Places API.")
                continue
            
            full_address = f"{display_name}, {formatted_address}"
            coordinates = geocode_address(full_address, api_key)
            if not coordinates:
                logger.warning(
                    f"No geocode result for {full_address} - skipping."
                )
                continue
            
            matched = assign_article_to_closest_sensor(coordinates, SENSORS)
            if matched:
                sensors_for_article.add(matched)
        
        logger.info("Geolocation processed.")
        
        if sensors_for_article:
            article["sensors"] = list(sensors_for_article)
        
        article["primary_tag"] = generate_primary_tag(article["description"])
        logger.info("Primary tag processed.")
        
        if (
            article["primary_tag"] == "Μη σχετικό"
            or (
                not article.get("sensors", [])
                and article["primary_tag"] != "Καιρικά και Φυσικά Φαινόμενα"
            )
        ):
            logger.info(
                "Article is not assigned to a sensor and is not related "
                "to Weather phenomena. Skipping..."
            )
            continue
        
        articles_with_sensors.append(article)
    
    # ----------------------------------------------------------------------
    # Secondary tagging and event dates
    # ----------------------------------------------------------------------
    for article in articles_with_sensors:
        article["secondary_tag"] = generate_secondary_tag(
            article["description"], article["primary_tag"]
        )
        logger.info("Secondary tag processed.")
        
        pub_date_midnight = to_midnight(article["pub_datetime"])
        
        pub_date = article["pub_datetime"].date()
        date_str = extract_date(pub_date, article["description"])
        raw_dates = (
            date_str.split("///")
            if date_str and date_str.lower() != "none"
            else []
        )
        
        iso_dates = []
        for raw in raw_dates:
            try:
                iso_dates.append(
                    to_midnight(datetime.strptime(raw.strip(), "%d/%m/%Y"))
                )
            except ValueError:
                logger.warning(
                    f"Un-parsable LLM date '{raw}' in {article.get('url')}"
                )
        
        if not iso_dates:
            iso_dates = [pub_date_midnight]
        
        article["dates"] = sorted(set(iso_dates))
        logger.info("Date(s) processed.")
        
        # Save to database
        save_article_to_db(article, get_articles_collection())
    
    logger.info("Processing completed successfully.")


if __name__ == "__main__":
    api_key = GOOGLE_API_KEY
    main()