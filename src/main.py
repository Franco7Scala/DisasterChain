import pandas as pd
import requests
import time
import os
from datetime import datetime, timedelta
from support.constants import *
from support.utils import merge_and_clean_datasets, load_checkpoint, save_checkpoint, calculate_weather_summaries
from support.news_engine import NEWS_ENGINE_VERSION, get_all_news_sources

def fetch_weather_data(api_parameters, disaster_id, row_index, max_retries=0):
    """
    Fetch weather data with retry/backoff for transient Open-Meteo failures.
    """
    for attempt in range(max_retries + 1):
        try:
            response = requests.get(
                OPEN_METEO_ARCHIVE_URL,
                params=api_parameters,
                timeout=30
            )

            if response.status_code == 200:
                json_data = response.json()
                return json_data.get("daily")

            if response.status_code in WEATHER_RETRY_STATUS_CODES and attempt < max_retries:
                wait_seconds = 5 * (attempt + 1)
                print(
                    f"Warning at row {row_index} (ID: {disaster_id}): "
                    f"HTTP {response.status_code}. Retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)
                continue

            print(f"Warning at row {row_index} (ID: {disaster_id}): HTTP Error {response.status_code}")
            return fetch_nasa_power_weather_data(api_parameters, disaster_id, row_index)

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                wait_seconds = 5 * (attempt + 1)
                print(
                    f"Warning at row {row_index} (ID: {disaster_id}): "
                    f"Open-Meteo timeout. Retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)
                continue
            print(f"Unexpected network error at row {row_index} (ID: {disaster_id}): Open-Meteo timeout")
            return fetch_nasa_power_weather_data(api_parameters, disaster_id, row_index)

        except Exception as e:
            print(f"Unexpected network error at row {row_index} (ID: {disaster_id}): {e}")
            return fetch_nasa_power_weather_data(api_parameters, disaster_id, row_index)

    return fetch_nasa_power_weather_data(api_parameters, disaster_id, row_index)


def fetch_nasa_power_weather_data(api_parameters, disaster_id, row_index):
    """
    Fallback weather source when Open-Meteo is unavailable.
    NASA POWER uses different variable names, so the response is normalized
    to the same daily_series structure used by the rest of the pipeline.
    """
    print(f"LOG [{disaster_id}]: Open-Meteo unavailable, trying NASA POWER fallback...")

    start = api_parameters["start_date"].replace("-", "")
    end = api_parameters["end_date"].replace("-", "")
    params = {
        "parameters": "PRECTOTCORR,T2M_MAX,T2M_MIN",
        "community": "RE",
        "longitude": api_parameters["longitude"],
        "latitude": api_parameters["latitude"],
        "start": start,
        "end": end,
        "format": "JSON"
    }

    try:
        response = requests.get(NASA_POWER_DAILY_URL, params=params, timeout=45)
        if response.status_code != 200:
            print(
                f"Warning at row {row_index} (ID: {disaster_id}): "
                f"NASA POWER HTTP Error {response.status_code}"
            )
            return None

        parameters = response.json().get("properties", {}).get("parameter", {})
        rainfall = parameters.get("PRECTOTCORR", {})
        temp_max = parameters.get("T2M_MAX", {})
        temp_min = parameters.get("T2M_MIN", {})

        start_dt = datetime.strptime(api_parameters["start_date"], "%Y-%m-%d")
        end_dt = datetime.strptime(api_parameters["end_date"], "%Y-%m-%d")

        times = []
        rain_values = []
        temp_max_values = []
        temp_min_values = []

        current = start_dt
        while current <= end_dt:
            key = current.strftime("%Y%m%d")
            times.append(current.strftime("%Y-%m-%d"))
            rain_values.append(float(rainfall.get(key, 0.0)))
            temp_max_values.append(float(temp_max.get(key, 0.0)))
            temp_min_values.append(float(temp_min.get(key, 0.0)))
            current += timedelta(days=1)

        print(f"LOG [{disaster_id}]: NASA POWER fallback succeeded.")
        return {
            "time": times,
            "rain_sum": rain_values,
            "snowfall_sum": [0.0] * len(times),
            "temperature_2m_max": temp_max_values,
            "temperature_2m_min": temp_min_values
        }

    except Exception as e:
        print(f"Unexpected NASA POWER error at row {row_index} (ID: {disaster_id}): {e}")
        return None

def main():
    print("--- STARTING ENVIRONMENTAL CAUSAL DATASET PIPELINE ---")

    # Ensure that the data directory exists before attempting to read input files
    os.makedirs(os.path.dirname(EMDAT_INPUT_PATH), exist_ok=True)
    
    #Cleaning and merging the original datasets to create a unified structure with all necessary information for the next steps of the pipeline. This step ensures that we have a clean and consistent dataset to work with for the weather data extraction.
    print("Step 1: Merging and cleaning source datasets...")
    disasters_df = merge_and_clean_datasets(EMDAT_INPUT_PATH, GDIS_INPUT_PATH)
    
    #Secure saving of the intermediate CSV file in the results directory
    os.makedirs(RESULTS_DIR, exist_ok=True)
    disasters_df.to_csv(CLEANED_DISASTERS_OUTPUT_PATH, index=False)
    print(f"Merged dataset saved locally at: {CLEANED_DISASTERS_OUTPUT_PATH}")
    
    # Apply test limit if IS_TEST_MODE constant is set to True
    if IS_TEST_MODE:
        print(f"LOG: Test mode ACTIVE. Limiting processing to the first {TEST_LIMIT} events.")
        execution_df = disasters_df.head(TEST_LIMIT)
    else:
        print(f"LOG: Production mode ACTIVE. Processing all {len(disasters_df)} events.")
        execution_df = disasters_df

    #Checkpoint and cache initialization
    #Loads the previously saved work (if present) to avoid losing progress in case of a crash. This allows the application to resume from where it left off without having to start over, ensuring that all previously collected weather data is preserved and can be reused.
    final_dataset = load_checkpoint(FINAL_DATASET_OUTPUT_PATH)
    print(f"Step 2: Checkpoint loaded. Already processed events: {len(final_dataset)}")
    
    #Reconstructs the dynamic in-memory cache based on the weather data already downloaded in previous checkpoints. This allows us to avoid making redundant API calls for locations and date ranges that have already been processed, significantly improving efficiency and reducing the number of requests to the Open-Meteo API.
    weather_cache = {}
    for disaster_id, content in final_dataset.items():
        if content.get("weather_data") and content["weather_data"].get("daily_series"):
            #The cache key is composed of: (latitude, longitude, date_minus_10, date_plus_10)
            cache_key = (content["latitude"], content["longitude"], content["date_minus_10"], content["date_plus_10"])
            weather_cache[cache_key] = content["weather_data"]["daily_series"]
    
    print(f"Weather cache initialized with {len(weather_cache)} unique locations from history.")
    print("Step 3: Starting weather data download loop...")

    #Temporary counter to track how many events had a successful weather data retrieval, for logging purposes
    total_processed_disasters = 0
    disasters_with_news = 0

    #Main loop for data extraction
    #It uses itertuples() to transform the rows into named tuples, which are much faster to iterate over compared to iterrows(). This optimization is crucial for processing large datasets efficiently, especially when making API calls for each event.
    for index, row in enumerate(execution_df.itertuples()):
        disaster_id = row.emdat_disaster_id
        
        #Selective rereading for missing news data
        already_processed = disaster_id in final_dataset
        news_already_searched = False
        if already_processed:
            current_news_version = (
                final_dataset[disaster_id]
                .get("news_data", {})
                .get("search_metadata", {})
                .get("news_engine_version")
            )
            news_already_searched = (
                final_dataset[disaster_id].get("news_data_searched", False)
                and current_news_version == NEWS_ENGINE_VERSION
            )
        
        if already_processed and news_already_searched:
            continue # Skip this event as it has already been processed and has news data
            
        #Key generation for the spatiotemporal cache of duplicates
        cache_key = (row.latitude, row.longitude, row.date_minus_10, row.date_plus_10)
        
        if already_processed:
            disaster_record = final_dataset[disaster_id]
            for internal_location_field in ["geolocation", "adm1", "adm2", "adm3", "location"]:
                disaster_record.pop(internal_location_field, None)
        else:
        # Creation of the final record structure
            disaster_record = {
                "disaster_id": disaster_id,
                "disaster_type": row.disaster_type, 
                "country": row.country,
                "region": row.region,
                "latitude": row.latitude,
                "longitude": row.longitude,
                "start_date": row.start_date,
                "date_minus_10": row.date_minus_10,
                "date_plus_10": row.date_plus_10,
                "weather_data": {
                    "pre_event_summary": None, # Summary of weather conditions in the 10 days before the event
                    "post_event_summary": None, # Summary of weather conditions in the 10 days after the event
                    "daily_series": None          # Daily weather data for the entire 20-day window
                },
                "satellite_data_path": None, # Path for satellite images 
                "news_data": None ,   
                "news_data_searched": False  # Flag to indicate if news data has been searched
            }
        
        #Flag to indicate whether weather data was successfully fetched
        weather_success = False

        if disaster_record["weather_data"]["daily_series"] is not None:
            weather_success = True
        #Cache check to avoid unnecessary API calls
        elif cache_key in weather_cache:
            extracted_weather = weather_cache[cache_key]
            disaster_record["weather_data"]["daily_series"] = extracted_weather

            #Compute summaries even when reloading from cache
            pre_sum, post_sum = calculate_weather_summaries(extracted_weather)
            disaster_record["weather_data"]["pre_event_summary"] = pre_sum
            disaster_record["weather_data"]["post_event_summary"] = post_sum
            weather_success = True
        
        else:
            api_parameters = {
                "latitude": row.latitude,
                "longitude": row.longitude,
                "start_date": row.date_minus_10,
                "end_date": row.date_plus_10,
                "daily": WEATHER_VARIABLES,
                "timezone": "auto"
            }
            extracted_weather = fetch_weather_data(api_parameters, disaster_id, index)

            if extracted_weather:
                #Saves the weather data in the cache and in the current record
                weather_cache[cache_key] = extracted_weather
                disaster_record["weather_data"]["daily_series"] = extracted_weather

                #Compute summaries for newly fetched data
                pre_sum, post_sum = calculate_weather_summaries(extracted_weather)
                disaster_record["weather_data"]["pre_event_summary"] = pre_sum
                disaster_record["weather_data"]["post_event_summary"] = post_sum

                weather_success = True
        #If weather data was successfully fetched, proceed with news data extraction
        if weather_success:

            print(f"LOG [{disaster_id}]: Starting news data extraction based on coordinates and date...")

            # Call the updated news engine with the real event coordinates.
            news_payload = get_all_news_sources(
                disaster_type=disaster_record["disaster_type"],
                country=disaster_record["country"],
                start_date=disaster_record["start_date"],
                region=disaster_record.get("region", "Global"),
                lat=disaster_record["latitude"],
                lon=disaster_record["longitude"],
                location_context={
                    "event_name": getattr(row, "event_name", None),
                    "emdat_location": getattr(row, "emdat_location", None),
                    "geolocation": getattr(row, "geolocation", None),
                    "adm1": getattr(row, "adm1", None),
                    "adm2": getattr(row, "adm2", None),
                    "adm3": getattr(row, "adm3", None),
                    "location": getattr(row, "location", None),
                },
                reliefweb_appname=RELIEFWEB_APPNAME
            )

            disaster_record["news_data"] = news_payload
            disaster_record["news_data_searched"] = True # Set the flag to indicate that news data has been searched for this event
            resolved = news_payload["search_metadata"]["sources_successfully_resolved"]
            n_articles = news_payload["search_metadata"]["total_articles_retrieved"]

            total_processed_disasters += 1

            if n_articles > 0:
                disasters_with_news += 1
            
            success_rate = (disasters_with_news / total_processed_disasters) * 100 

            print(f" -> Completed. Articles retrieved: {n_articles}. Sources resolved: {resolved}. Current news retrieval success rate: {success_rate:.2f}% ({disasters_with_news}/{total_processed_disasters})")
            print(f" -> Telemetry: Disasters with news: {disasters_with_news}/{total_processed_disasters}, Current success rate: {success_rate:.2f}%")
            #Persistence: Saving the final record into the main data structure and immediately writing the checkpoint to the hard drive to ensure that progress is not lost in case of an unexpected interruption. This approach allows for incremental saving of results, providing robustness and reliability to the data collection process.
            final_dataset[disaster_id] = disaster_record
            
            #Immediate writing of the checkpoint to the hard drive 
            save_checkpoint(final_dataset, FINAL_DATASET_OUTPUT_PATH)

        # Progress control print on the terminal every 50 processed events
        if index % 50 == 0 and index > 0:
            print(f"Progress: processed {index}/{len(execution_df)} events.")
            
        #Preventive Rate Limiting to avoid being banned by Open-Meteo servers (100 milliseconds)
        time.sleep(3)
        
    print("\n--- PIPELINE EXECUTION COMPLETED SUCCESSFULY ---")
    print(f"Final dataset structure compiled and updated at: {FINAL_DATASET_OUTPUT_PATH}")

if __name__ == "__main__":
    main()
