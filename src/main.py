import pandas as pd
import requests
import time
import os
from support.constants import *
from support.utils import merge_and_clean_datasets, load_checkpoint, save_checkpoint

def main():
    print("--- STARTING ENVIRONMENTAL CAUSAL DATASET PIPELINE ---")
    
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
        if content.get("weather_data"):
            #The cache key is composed of: (latitude, longitude, date_minus_10, date_plus_10)
            cache_key = (content["latitude"], content["longitude"], content["date_minus_10"], content["date_plus_10"])
            weather_cache[cache_key] = content["weather_data"]
    
    print(f"Weather cache initialized with {len(weather_cache)} unique locations from history.")
    print("Step 3: Starting weather data download loop...")

    #Main loop for data extraction
    #It uses itertuples() to transform the rows into named tuples, which are much faster to iterate over compared to iterrows(). This optimization is crucial for processing large datasets efficiently, especially when making API calls for each event.
    for index, row in enumerate(execution_df.itertuples()):
        disaster_id = row.disasterno
        
        # CHECKPOINT RESILIENCE: SKIP RECORDS ALREADY PROCESSED IN PREVIOUS CHECKPOINT
        if disaster_id in final_dataset:
            continue
            
        #Key generation for the spatiotemporal cache of duplicates
        cache_key = (row.latitude, row.longitude, row.date_minus_10, row.date_plus_10)
        
        # Creation of the final record structure
        disaster_record = {
            "disaster_id": disaster_id,
            "disaster_type": row.disaster_type, 
            "latitude": row.latitude,
            "longitude": row.longitude,
            "start_date": row.start_date,
            "date_minus_10": row.date_minus_10,
            "date_plus_10": row.date_plus_10,
            "weather_data": None,        # Weather data
            "satellite_data_path": None, # Path for satellite images 
            "news_data": None            # News data 
        }
        
        #Cache check to avoid unnecessary API calls
        if cache_key in weather_cache:
            disaster_record["weather_data"] = weather_cache[cache_key]
            final_dataset[disaster_id] = disaster_record
            
            #Immediate writing of the checkpoint to the hard drive 
            save_checkpoint(final_dataset, FINAL_DATASET_OUTPUT_PATH)
            continue

        #Params configuration for the network request to the Open-Meteo API
        api_parameters = {
            "latitude": row.latitude,
            "longitude": row.longitude,
            "start_date": row.date_minus_10,
            "end_date": row.date_plus_10,
            "daily": WEATHER_VARIABLES,
            "timezone": "auto"
        }
        
        try:
            response = requests.get(OPEN_METEO_ARCHIVE_URL, params=api_parameters)
            
            if response.status_code == 200:
                json_data = response.json()
                
                if "daily" in json_data:
                    extracted_weather = json_data["daily"]
                    
                    #Saves the weather data in the cache and in the current record
                    weather_cache[cache_key] = extracted_weather
                    disaster_record["weather_data"] = extracted_weather
                    
                    #Inserts the final record into the main data structure
                    final_dataset[disaster_id] = disaster_record
                    
                    #Incremental save to the hard drive
                    save_checkpoint(final_dataset, FINAL_DATASET_OUTPUT_PATH)
            else:
                print(f"Warning at row {index} (ID: {disaster_id}): HTTP Error {response.status_code}")
                
        except Exception as e:
            print(f"Unexpected network error at row {index} (ID: {disaster_id}): {e}")
            
        # Progress control print on the terminal every 50 processed events
        if index % 50 == 0 and index > 0:
            print(f"Progress: processed {index}/{len(execution_df)} events.")
            
        #Preventive Rate Limiting to avoid being banned by Open-Meteo servers (100 milliseconds)
        time.sleep(0.1)
        
    print("\n--- PIPELINE EXECUTION COMPLETED SUCCESSFULY ---")
    print(f"Final dataset structure compiled and updated at: {FINAL_DATASET_OUTPUT_PATH}")

if __name__ == "__main__":
    main()