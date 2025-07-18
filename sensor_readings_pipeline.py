#!/usr/bin/env python3
"""
Air Quality Sensor Data Scraper and Importer

This pipeline automates downloading sensor CSV data from PurpleAir,
parses it, and inserts it into a MongoDB collection.
"""

import time
import logging
import os
import pandas as pd
import shutil

from datetime import datetime, timedelta
from pymongo import MongoClient
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Download directory
DOWNLOAD_DIR = os.path.abspath("csv_readings")
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# MongoDB configuration
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "article_database")
SENSOR_DATA_COLLECTION = os.environ.get("SENSOR_DATA_COLLECTION", "sensor_readings")

def get_mongo_client():
    return MongoClient(MONGO_URI)

def get_database():
    client = get_mongo_client()
    return client[DATABASE_NAME]

def get_sensor_data_collection():
    db = get_database()
    return db[SENSOR_DATA_COLLECTION]

# Sensor ID to Name dictionary
SENSOR_ID_TO_NAME = {
    "101609": "Paralia",
    "121199": "Gymnasio Ovryas",
    "121529": "Dimotiko Saravaliou",
    "101589": "Lefka",
    "14857": "Demenika",
    "30673": "New Port of Patras 2024_V2",
    "199745": "PANSEN_Patras_Psahou",
    "121535": "15 Dimotiko Patras",
    "199165": "PANSENS-KETX",
    "101611": "Trion Navarchon",
    "199161": "PANSEN_PATRA_PSILALONIA",
    "23759": "Germanou",
    "133608": "Kritika_2024",
    "199149": "PANSEN_PATRA_AROI",
    "202573": "PANSEN_PATRA_LAQS_AGIASOFIA",
    "202603": "64 Dimotiko Patras 2024",
    "101537": "Agyia_2024",
    "56113": "Kato Sichena",
    "202609": "49 Dimotiko Patras 2024",
    "1672": "Kastelokampos",
    "1566": "University of Patras",
    "56491": "Rio 2024",
    "172993": "FORTH/ICE-HT",
    "23837": "ICE/ FORTH",
    "1006": "Platani",
    "121547": "20o Gymnasio Patras"
}


def download_readings():
    """
    Downloads sensor CSV data from the PurpleAir website using Selenium.
    Saves all downloaded CSV files to the DOWNLOAD_DIR.
    """
    
    # Base URL used for each sensor
    base_url = (
        "https://map.purpleair.com/air-quality-standards-us-epa-aqi?"
        "opt=%2F1%2Flp%2Fa10%2Fp604800%2FcC0&select={sensor_id}#9/38.24138/21.76289"
    )
    
    # Set up Chrome options for automatic downloads (Selenium)
    chrome_options = webdriver.ChromeOptions()
    prefs = {
        "download.default_directory": DOWNLOAD_DIR,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--no-sandbox")
    driver = webdriver.Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 20)
    
    def remove_interfering_popups():
        """
        Remove any interfering popup elements from the DOM.
        """
        try:
            selectors = [".popup-conditions", ".mapboxgl-popup"]
            for sel in selectors:
                elems = driver.find_elements(By.CSS_SELECTOR, sel)
                for elem in elems:
                    if elem.is_displayed():
                        driver.execute_script("arguments[0].remove();", elem)
                        logger.info(f"Removed interfering element with selector: {sel}")
        except Exception as e:
            logger.error(f"Error removing interfering popups: {e}")
    
    def js_click(element):
        """Dispatch a click event on the element using JavaScript."""
        driver.execute_script("""
            var evt = new MouseEvent('click', {bubbles: true, cancelable: true});
            arguments[0].dispatchEvent(evt);
            """, element)
    
    try:
        for sensor_id, sensor_name in SENSOR_ID_TO_NAME.items():
            url = base_url.format(sensor_id=sensor_id)
            driver.get(url)
            
            try:
                settings_button = wait.until(EC.element_to_be_clickable((By.ID, "mapToolsCogButton")))
                js_click(settings_button)
                logger.info(f"Opened settings for sensor '{sensor_name}'.")
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error clicking settings button for sensor '{sensor_name}': {e}")
            
            try:
                average_select = wait.until(EC.presence_of_element_located((By.ID, "mapDataAverage")))
                select = Select(average_select)
                select.select_by_value("60")
                logger.info(f"Averaging period for sensor '{sensor_name}' set to 1-hour.")
                time.sleep(2)
            except Exception as e:
                logger.error(f"Error setting averaging period for sensor '{sensor_name}': {e}")
            
            # Wait until the sensor's data label is populated (data is not loaded bug)
            try:
                wait.until(lambda d: d.find_element(By.CSS_SELECTOR, ".highcharts-data-label text").text.strip() != "")
                logger.info(f"Data loaded for sensor '{sensor_name}'.")
            except TimeoutException:
                logger.warning(f"Sensor '{sensor_name}' did not load data (possibly not available on the map). Skipping sensor.")
                continue
            
            remove_interfering_popups()
            
            try:
                export_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".highcharts-contextbutton")))
                js_click(export_button)
            except Exception as e:
                logger.error(f"Error clicking export button for sensor '{sensor_name}': {e}")
                continue
            
            remove_interfering_popups()
            
            try:
                download_csv_option = wait.until(EC.element_to_be_clickable((By.XPATH, "//li[normalize-space()='Download CSV']")))
                js_click(download_csv_option)
            except Exception as e:
                logger.error(f"Error clicking download CSV option for sensor '{sensor_name}': {e}")
                continue
            
            # Wait for a few seconds for the download to finish
            time.sleep(5)
    finally:
        driver.quit()
    
    logger.info(f"All sensor CSV files have been downloaded in: {DOWNLOAD_DIR}")


def save_readings():
    """
    Reads downloaded CSV files, processes them, and inserts data into MongoDB.
    Deletes processed files from DOWNLOAD_DIR.
    """
    
    NAME_TO_ID = {name: sid for sid, name in SENSOR_ID_TO_NAME.items()}
    
    sensor_data_collection = get_sensor_data_collection()
    
    today = datetime.today()
    yesterday = today - timedelta(days=1)
    target_dates = {yesterday.strftime("%Y-%m-%d")}
    
    insert_count = 0
    for filename in os.listdir(DOWNLOAD_DIR):
        file_path = os.path.join(DOWNLOAD_DIR, filename)
        try:
            df = pd.read_csv(file_path, encoding="utf-8-sig")
        except Exception as e:
            logger.error(f"Error reading CSV file {filename}: {e}")
            continue
        
        try:
            # Calculate average from the last two columns
            last_two_columns = df.iloc[:, -2:]
            average = last_two_columns.mean(axis=1).tolist()
            
            # Retrieve date and time
            first_col = df.iloc[:, 0]
            split_data = first_col.str.split(" ", expand=True)
            dates = split_data[0].astype(str).tolist()
            times = split_data[1].astype(str).tolist()
            
            sensor_name = df.columns[-1].replace(" B", "")
            sensor_id = NAME_TO_ID.get(sensor_name)
            
            # Group readings by date
            readings_by_date = {}
            for d, t, val in zip(dates, times, average):
                if d not in target_dates:
                    continue
                if d not in readings_by_date:
                    readings_by_date[d] = {"times": [], "values": []}
                readings_by_date[d]["times"].append(t)
                readings_by_date[d]["values"].append(val)
            
            # Insert one document per date
            for date_key, data in readings_by_date.items():
                try:
                    iso_date = datetime.strptime(date_key, "%Y-%m-%d")
                except ValueError:
                    logger.warning(f"Invalid date format in filename: {date_key}")
                    continue
                
                document = {
                    "sensor_name": sensor_name,
                    "sensor_id": sensor_id,
                    "date": iso_date,
                    "readings": {t: v for t, v in zip(data["times"], data["values"])}
                }
                
                try:
                    sensor_data_collection.insert_one(document)
                    insert_count += 1
                except Exception as e:
                    logger.error(f"Error inserting document for sensor {sensor_name} on {date_key}: {e}")
        except Exception as e:
            logger.error(f"Error processing file {filename}: {e}")
    
    logger.info(f"Inserted {insert_count} documents.")
    
    # Delete all files after processing
    for filename in os.listdir(DOWNLOAD_DIR):
        file_path = os.path.join(DOWNLOAD_DIR, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            logger.error(f"Failed to delete {file_path}. Reason: {e}")


def main():
    download_readings()
    save_readings()


if __name__ == "__main__":
    main()