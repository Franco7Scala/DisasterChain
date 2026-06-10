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
#satellite_engine
#########################################################

# Copernicus Data Space Ecosystem / Sentinel Hub endpoints.
COPERNICUS_AUTH_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
SENTINEL_HUB_BASE_URL = "https://sh.dataspace.copernicus.eu"
CATALOG_SEARCH_URL = f"{SENTINEL_HUB_BASE_URL}/catalog/v1/search"
PROCESS_URL = f"{SENTINEL_HUB_BASE_URL}/process/v1"
SATELLITE_OUTPUT_DIR = os.path.join(RESULTS_DIR, "satellite")


#########################################################
#news_engine
#########################################################

# Version 
NEWS_ENGINE_VERSION = "8.17-filter-generic-wikipedia"

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

# Article text enrichment
ENRICH_ARTICLE_RAW_TEXT = True
ARTICLE_TEXT_TIMEOUT = 5
ARTICLE_TEXT_MIN_CHARS = 500
ARTICLE_TEXT_MAX_CHARS = 6000
ARTICLE_TEXT_FETCH_LIMIT_PER_EVENT = 5

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

NON_ARTICLE_URL_PATTERNS = [
    r"/taxonomy/term/",
    r"/wiki/(?:List_of|Category:|Portal:|Template:)",
    r"/(?:category|tag|tags)/",
]
NON_ARTICLE_URL_RE = [re.compile(p, re.IGNORECASE) for p in NON_ARTICLE_URL_PATTERNS]

NON_ARTICLE_TITLE_PATTERNS = [
    r"^\s*list of\b",
    r"\btaxonomy\b",
    r"\bcategory\b",
]
NON_ARTICLE_TITLE_RE = [re.compile(p, re.IGNORECASE) for p in NON_ARTICLE_TITLE_PATTERNS]

SOURCE_RELEVANCE_THRESHOLDS = {
    "ReliefWeb": 4,
    "ReliefWeb Disasters": 4,
    "ReliefWeb Updates": 4,
    "GDACS": 4,
    "NASA EONET": 4,
    "NASA Earth Observatory/EONET": 4,
    "IFRC GO": 4,
    "ERCC Copernicus": 4,
    "ERCC portal": 4,
    "WMO": 4,
    "FloodList": 5,
    "ADRC Asia": 4,
    "AHA Centre": 4,
    "Africa Hazards Watch": 4,
    "ReliefWeb Africa": 4,
    "PAHO": 4,
    "NOAA Climate Report": 5,
    "CIMA Research": 4,
    "MeteoAlarm": 4,
    "Wikipedia": 7,
    "Google News": 6,
    "DuckDuckGo": 7,
}


DIRECT_SOURCE_PRIORITY = {
    "FloodList": 100,
    "ReliefWeb": 95,
    "ReliefWeb Disasters": 94,
    "ReliefWeb Updates": 94,
    "IFRC GO": 90,
    "NASA EONET": 88,
    "NASA Earth Observatory/EONET": 88,
    "GDACS": 86,
    "WMO": 84,
    "Wikipedia": 70,
    "Google News": 30,
    "DuckDuckGo": 20,
}





#########################################################
#analyze_news_benchmark
#########################################################
DEFAULT_INPUT_PATH = os.path.join(RESULTS_DIR, "news_benchmark_review.xlsx")
DEFAULT_REPORT_PATH = os.path.join(RESULTS_DIR, "news_benchmark_report.txt")
DEFAULT_WRONG_OUTPUT_PATH = os.path.join(RESULTS_DIR, "news_benchmark_wrong_cases.csv")


VALID_LABELS = {"correct", "wrong", "uncertain"}

##########################################################
#create_news_benchmark
##########################################################
DEFAULT_OUTPUT_PATH = os.path.join(RESULTS_DIR, "news_benchmark_review.csv")
DEFAULT_EXCEL_OUTPUT_PATH = os.path.join(RESULTS_DIR, "news_benchmark_review.xlsx")




CSV_COLUMNS = [
    "benchmark_event_number",
    "disaster_id",
    "country",
    "region",
    "disaster_type",
    "start_date",
    "latitude",
    "longitude",
    "news_engine_version",
    "total_articles_for_event",
    "sources_successfully_resolved",
    "article_index",
    "source",
    "title",
    "url",
    "raw_text",
    "raw_text_status",
    "raw_text_length",
    "raw_text_url",
    "relevance_score",
    "relevance_threshold",
    "confidence",
    "is_non_article_candidate",
    "relevance_reasons",
    "relevance_penalties",
    "search_query",
    "query_precision",
    "published_at",
    "manual_label",
    "manual_notes",
]

##########################################################
#analyze_news_sources
##########################################################
DEFAULT_REPORT_PATH = os.path.join(RESULTS_DIR, "news_source_statistics.txt")
DEFAULT_CSV_PATH = os.path.join(RESULTS_DIR, "news_source_statistics.csv")
