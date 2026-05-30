import os
import re

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

#Official appname for ReliefWeb API
RELIEFWEB_APPNAME = "Unical-EnvironmentalCausalDataset-432353"
#Status codes
WEATHER_RETRY_STATUS_CODES = {429, 502, 503, 504}
#URL for NASA Power Daily
NASA_POWER_DAILY_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"


#########################################################
#news_engine
#########################################################

# Version 
NEWS_ENGINE_VERSION = "8.5-fuzzy-entity-matching"

# Headers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
HEADERS_JSON = {**HEADERS, "Accept": "application/json"}
HEADERS_XML  = {**HEADERS, "Accept": "application/rss+xml,application/xml,text/xml"}
FLOODLIST_SITEMAP_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}

# Utils
MONTH_NAMES = {
    "01":"January","02":"February","03":"March","04":"April",
    "05":"May","06":"June","07":"July","08":"August",
    "09":"September","10":"October","11":"November","12":"December"
}

# Disaster type to search terms
SEARCH_TERMS = {
    "flood":               ["flood","flooding","floods","inundation"],
    "storm":               ["storm","cyclone","hurricane","typhoon","tropical storm"],
    "earthquake":          ["earthquake","quake","tremor","seismic"],
    "landslide":           ["landslide","mudslide","mudflow","rockslide"],
    "drought":             ["drought"],
    "wildfire":            ["wildfire","forest fire","bushfire"],
    "volcanic activity":   ["volcano","eruption","volcanic"],
    "extreme temperature": ["heatwave","heat wave","cold wave","extreme heat","extreme cold"],
    "epidemic":            ["epidemic","outbreak"],
    "mass movement":       ["landslide","mudslide","collapse"],
    "insect infestation":  ["locust","infestation"],
}


SEMANTIC_SYNONYMS = {
    "flood":               ["flood","flooding","inundation","inundated","floodwater",
                            "submerged","overflow","deluge","swamped","rain","rainfall",
                            "downpour","heavy rain","flash flood"],
    "storm":               ["storm","cyclone","typhoon","hurricane","tropical storm",
                            "gale","tempest","rain","rainfall","downpour","wind",
                            "squall","depression","disturbance"],
    "earthquake":          ["earthquake","seismic","tremor","quake","aftershock",
                            "magnitude","richter","fault"],
    "landslide":           ["landslide","mudslide","mudflow","rockfall","avalanche",
                            "debris","slope failure"],
    "drought":             ["drought","dry spell","water shortage","arid",
                            "rainfall deficit","crop failure"],
    "wildfire":            ["wildfire","forest fire","bushfire","blaze","brushfire"],
    "volcanic activity":   ["volcano","volcanic","eruption","lava","ash","pyroclastic"],
    "extreme temperature": ["heatwave","heat wave","cold wave","extreme cold",
                            "extreme heat","frost","freezing","temperature"],
    "epidemic":            ["epidemic","outbreak","disease","cholera","typhoid",
                            "infection","virus"],
    "mass movement":       ["landslide","mudslide","rockfall","avalanche","collapse"],
    "insect infestation":  ["locust","infestation","plague","swarm"],
}


METAPHOR_PATTERNS = [
    r"flood(?:s|ed|ing)?\s+(?:twitter|social[\s-]?media|internet|inbox|market|"
    r"email|news\s+feed|whatsapp|facebook|instagram|tiktok)",
    r"(?:twitter|social[\s-]?media|internet|inbox)\s+flood",
    r"flood(?:s|ed|ing)?\s+(?:of\s+)?(?:message|comment|gif|meme|tweet|post|"
    r"request|complaint|call|order|application|capital|investor|money)",
    r"storm\s+(?:of\s+)?(?:criticism|protest|controversy|backlash|applause|"
    r"praise|tweet|comment|reaction|anger|outrage)",
    r"(?:music|album|song|chart|box[\s-]?office|sales)\s+flood",
    r"flood(?:s|ed|ing)?\s+(?:with\s+)?(?:fake[\s-]?news|misinformation)",
    r"(?:memories|emotion|grief|joy|tears)\s+flood",
    r"underwater\s+(?:paradise|wonderland|fantasyland)",
    r"crystal[\s-]?clear\s+(?:river|water|lake)",
    r"hiking\s+trail.{0,30}flood",
    r"(?:retire|retirement|farewell|transfer|signing|scored|tournament|"
    r"championship|medal|olympic)\b",
    r"election.{0,30}flood",
    r"flood.{0,30}election",
    r"(?:carnival|dancer|dancers|g-string|skimpy|sparkly|parade|photos?).{0,50}flood",
    r"flood.{0,50}(?:carnival|dancer|dancers|g-string|skimpy|sparkly|parade|photos?)",
]
METAPHOR_RE = [re.compile(p, re.IGNORECASE) for p in METAPHOR_PATTERNS]

GENERIC_ARTICLE_PATTERNS = [
    r"\bannual report\b",
    r"\bsituation report\b",
    r"\boverview\b",
    r"\bsummary\b",
    r"\bseason\b",
    r"\bclimate report\b",
    r"\bforecast\b",
    r"\bpreparedness\b",
    r"\bappeal\b",
    r"\boperation update\b",
]
GENERIC_ARTICLE_RE = [re.compile(p, re.IGNORECASE) for p in GENERIC_ARTICLE_PATTERNS]
