import os

# API Configurations
# Base URL for the Open-Meteo Archive API, used to fetch historical weather data based on latitude, longitude, and date range.
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Base Paths
# Define the base directory of the project and the results directory where all output files will be saved. This ensures that all file paths are relative to the project structure and can be easily modified if needed.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS_DIR = os.path.join(BASE_DIR, "results")

# Input File Paths
# Define the exact paths of the original Excel and CSV files that contain the raw data.
EMDAT_INPUT_PATH = os.path.join(BASE_DIR, "data", "public_emdat_dal_2000.xlsx")
GDIS_INPUT_PATH = os.path.join(BASE_DIR, "data", "pend-gdis-1960-2018-disasterlocations.csv")

# Output File Paths
# The files where we will save the results. The final file will be a single structured JSON.
CLEANED_DISASTERS_OUTPUT_PATH = os.path.join(RESULTS_DIR, "disasters_per_satellite.csv")
FINAL_DATASET_OUTPUT_PATH = os.path.join(RESULTS_DIR, "final_environmental_causal_dataset.json")

# Weather API Variables
# The weather variables requested from the API, centralized for easy modification.
WEATHER_VARIABLES = ["rain_sum", "snowfall_sum", "temperature_2m_max", "temperature_2m_min"]

# Execution Settings
# Control variable. If True, run only on 500 events for testing. If False, run on all 52,000 events.
IS_TEST_MODE = True
TEST_LIMIT = 500