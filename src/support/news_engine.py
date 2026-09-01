"""
news_engine.py

News retrieval engine for disaster-specific sources.

Main behavior:
  1. Query all configured global and regional sources for each disaster.
  2. Use event names and local place names, when available, to improve precision.
  3. Use source-specific fallbacks for sites that block direct scraping.
  4. Use DuckDuckGo only as a last-resort fallback.
"""

import requests
import urllib.parse
import html as html_lib
from datetime import datetime, timedelta
from support.constants import *
from support.extra import _resolve_and_extract_text as _extra_resolve_and_extract_text
from difflib import SequenceMatcher
import re
import time


def get_date_window(start_date_str, days=21):
    try:
        dt = datetime.strptime(start_date_str, "%Y-%m-%d")
        return (
            (dt - timedelta(days=days)).strftime("%Y-%m-%d"),
            (dt + timedelta(days=days)).strftime("%Y-%m-%d"),
        )
    except Exception:
        return start_date_str, start_date_str

def clean_html(text):
    return re.sub(r"<[^>]+>", "", str(text)).strip()


def normalize_whitespace(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()

def parse_date_parts(start_date):
    """Return (year, month_num, month_name, day) from a YYYY-MM-DD string."""
    p = start_date.split("-")
    y = p[0] if len(p) > 0 else ""
    m = p[1] if len(p) > 1 else "01"
    d = p[2] if len(p) > 2 else "01"
    return y, m, MONTH_NAMES.get(m, ""), d

def parse_pubdate(s):
    if not s:
        return None
    for fmt in ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y%m%d%H%M%S", "%Y-%m-%d"]:
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            pass
    return None

def days_from_event(pub_date_str, start_date):
    pub_dt = parse_pubdate(pub_date_str)
    if pub_dt is None:
        return None
    try:
        return abs((pub_dt - datetime.strptime(start_date, "%Y-%m-%d")).days)
    except Exception:
        return None

def make_article(source, title, url, raw_text):
    return {"source": source, "title": title.strip(),
            "url": url, "raw_text": str(raw_text).strip()[:ARTICLE_TEXT_MAX_CHARS]}

def log_source(source_name, count):
    """Log how many articles a source returned."""
    if count > 0:
        print(f"    [{source_name}] -> {count} articles found")

def get_search_terms(disaster_type):
    clean = disaster_type.split("(")[0].strip().lower()
    for key, terms in SEARCH_TERMS.items():
        if key in clean:
            return terms
    return [clean]

def get_synonyms(disaster_type):
    clean = disaster_type.split("(")[0].strip().lower()
    for key, syns in SEMANTIC_SYNONYMS.items():
        if key in clean:
            return syns
    return [clean]


# ============================================================
# HELPER: GENERIC GOOGLE NEWS RSS 
# Used by query_google_news() and query_floodlist().
# ============================================================

def _google_news_rss(query, country, start_date, disaster_type,
                     window_days=30, max_results=5, source_label="Google News"):
    """
    Query Google News RSS with the provided query.
    Filter results through is_relevant() using feed publication dates.
    Return a list of already validated articles.
    """
    year, _, mname, _ = parse_date_parts(start_date)

    r = safe_get("https://news.google.com/rss/search",
                 params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                 headers=HEADERS_XML)
    if r is None:
        return []

    raw_items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
    if not raw_items:
        # Google News may return an HTML error page instead of RSS.
        if "<html" in r.text[:200].lower():
            print(f"    [WARN] Google News returned HTML instead of RSS "
                  f"(possible captcha/rate limit). Query: {query[:60]}")
        return []

    results, seen = [], set()
    for item_text in raw_items:
        title_m  = re.search(r"<title>(.*?)</title>", item_text, re.DOTALL)
        link_m   = re.search(r"<link>(.*?)</link>", item_text, re.DOTALL)
        pub_m    = re.search(r"<pubDate>(.*?)</pubDate>", item_text, re.DOTALL)
        source_m = re.search(r"<source[^>]*>(.*?)</source>", item_text, re.DOTALL)
        if not title_m:
            continue

        title = clean_html(title_m.group(1)).strip()
        # Remove the trailing "- Outlet Name" suffix added by Google.
        title = re.sub(r"\s*-\s*[^-]{3,50}$", "", title).strip()
        norm  = title.lower()
        if norm in seen or len(title) < 8:
            continue

        pub    = pub_m.group(1).strip() if pub_m else ""
        source = clean_html(source_m.group(1)).strip() if source_m else source_label
        link   = link_m.group(1).strip() if link_m else ""

        if not is_relevant(title, title, country, start_date, disaster_type,
                           pub_date_str=pub, window_days=window_days):
            continue

        seen.add(norm)
        label = f"{source_label} ({source})" if source != source_label else source_label
        fallback_text = f"{title} [{source}, {pub}]"
        final_link, full_text = _resolve_and_extract_text(
            link,
            fallback_text=fallback_text,
        )
        article = make_article(label, title, final_link, full_text)
        if len(full_text) > len(fallback_text):
            article["raw_text_status"] = "fetched_full_text"
            article["raw_text_url"] = final_link
            article["raw_text_length"] = len(article["raw_text"])
        results.append(article)
        if len(results) >= max_results:
            break

    return results

# ============================================================
# SOURCE 1 - RELIEFWEB (reports + disasters + updates)
# ============================================================

def query_reliefweb(disaster_type, country, start_date,
                    appname="Unical-EnvironmentalCausalDataset-432353",
                    location_context=None):
    year, _, mname, _ = parse_date_parts(start_date)
    start_w, end_w = get_date_window(start_date, 30)
    terms = get_search_terms(disaster_type)
    clean_type = disaster_type.split("(")[0].strip()

    results = []

    queries = [
        f"{country} {terms[0]} {mname} {year}",
        f"{country} {terms[0]} {year}",
        f"{country} {clean_type}",
        country,
    ]

    # --- /reports and /updates ---
    for endpoint in ["reports", "updates"]:
        for q in queries:
            params = {
                "appname": appname,
                "query[value]": q,
                "query[operator]": "AND",
                "filter[operator]": "AND",
                "filter[conditions][0][field]": "date.created",
                "filter[conditions][0][value][from]": f"{start_w}T00:00:00+00:00",
                "filter[conditions][0][value][to]": f"{end_w}T23:59:59+00:00",
                "filter[conditions][0][operator]": "range",
                "filter[conditions][1][field]": "primary_country.name",
                "filter[conditions][1][value]": country,
                "fields[include][]": ["title","url","body","date"],
                "limit": 5, "profile": "full",
            }
            r = safe_get(f"https://api.reliefweb.int/v2/{endpoint}",
                         params=params, headers=HEADERS_JSON)
            if r is None:
                continue
            try:
                data = r.json()
            except Exception:
                continue
            for item in data.get("data", []):
                f = item.get("fields", {})
                title    = f.get("title", "")
                body     = clean_html(f.get("body", "") or f.get("body-html", ""))
                item_url = f.get("url", "https://reliefweb.int")
                pub      = (f.get("date") or {}).get("created", "")
                src      = "ReliefWeb" if endpoint == "reports" else "ReliefWeb Updates"
                if is_relevant(title, body, country, start_date, disaster_type,
                               pub_date_str=pub, window_days=30):
                    results.append(make_article(src, title, item_url, body))
            if results:
                log_source("ReliefWeb", len(results))
                return results
    # --- /disasters (better historical coverage for pre-2005 events) ---
    for q in queries[:2]:
        params_d = {
            "appname": appname,
            "query[value]": q,
            "filter[conditions][0][field]": "date.event",
            "filter[conditions][0][value][from]": f"{start_w}T00:00:00+00:00",
            "filter[conditions][0][value][to]": f"{end_w}T23:59:59+00:00",
            "filter[conditions][0][operator]": "range",
            "fields[include][]": ["name","glide","url","date","country","type","description"],
            "limit": 5, "profile": "full",
        }
        r = safe_get("https://api.reliefweb.int/v2/disasters",
                     params=params_d, headers=HEADERS_JSON)
        if r is None:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        for item in data.get("data", []):
            f    = item.get("fields", {})
            name = f.get("name", "")
            desc = f.get("description", "") or name
            url  = f.get("url", "https://reliefweb.int")
            pub  = (f.get("date") or {}).get("event", "")
            if is_relevant(name, desc, country, start_date, disaster_type,
                           pub_date_str=pub, window_days=45):
                results.append(make_article("ReliefWeb Disasters", name, url, desc))
        if results:
            log_source("ReliefWeb Disasters", len(results))
            return results

    return results

# ============================================================
# SOURCE 2 - GDACS
# ============================================================

def query_gdacs(disaster_type, country, start_date):
    type_map = {"flood":"FL","storm":"TC","earthquake":"EQ","drought":"DR",
                "volcanic":"VO","wildfire":"WF","landslide":"FL"}
    clean = disaster_type.split("(")[0].strip().lower()
    gdacs_type = next((v for k,v in type_map.items() if k in clean), None)
    start_w, end_w = get_date_window(start_date, 30)
    # GDACS RSS does not support date filters via GET, so filtering is client-side.
    params = {"profile":"ARCHIVE","alertlevel":"Green,Orange,Red",
              "country": country}
    if gdacs_type:
        params["eventtype"] = gdacs_type

    r = safe_get("https://www.gdacs.org/xml/rss.ashx", params=params, headers=HEADERS_XML)
    if r is None:
        return []

    results = []
    for item_text in re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL):
        title_m = re.search(r"<title>(.*?)</title>", item_text)
        link_m  = re.search(r"<link>(.*?)</link>", item_text)
        desc_m  = re.search(r"<description>(.*?)</description>", item_text)
        pub_m   = re.search(r"<pubDate>(.*?)</pubDate>", item_text)
        if not title_m:
            continue
        title = clean_html(title_m.group(1))
        desc  = clean_html(desc_m.group(1)) if desc_m else ""
        link  = link_m.group(1).strip() if link_m else "https://www.gdacs.org"
        pub   = pub_m.group(1) if pub_m else ""
        if is_relevant(title, desc, country, start_date, disaster_type,
                       pub_date_str=pub, window_days=30):
            results.append(make_article("GDACS", title, link, desc))

    log_source("GDACS", len(results))
    return results

# ============================================================
# SOURCE 3 - NASA EONET
# ============================================================

def query_nasa_eonet(disaster_type, country, start_date):
    cat_map = {"flood":"floods","storm":"severeStorms","wildfire":"wildfires",
               "earthquake":"earthquakes","volcanic":"volcanoes",
               "drought":"drought","landslide":"landslides"}
    clean = disaster_type.split("(")[0].strip().lower()
    category = next((v for k,v in cat_map.items() if k in clean), None)
    start_w, end_w = get_date_window(start_date, 30)

    params = {"status":"all","start":start_w,"end":end_w,"limit":200}
    if category:
        params["category"] = category

    r = safe_get("https://eonet.gsfc.nasa.gov/api/v3/events",
                 params=params, headers=HEADERS_JSON)
    if r is None:
        return []

    results = []
    country_lower = country.lower()
    try:
        for e in r.json().get("events", []):
            title = e.get("title","")
            desc  = e.get("description","") or ""
            if country_lower in (title+desc).lower():
                cats = [c.get("title","") for c in e.get("categories",[])]
                raw  = desc or f"NASA EONET - {title}. Categories: {cats}"
                results.append(make_article("NASA EONET", title,
                    e.get("link","https://eonet.gsfc.nasa.gov"), raw))
    except Exception:
        pass

    log_source("NASA EONET", len(results))
    return results


def _wiki_extract(title):
    r = safe_get("https://en.wikipedia.org/w/api.php", params={
        "action":"query","prop":"extracts","exintro":True,
        "explaintext":True,"titles":title,"format":"json","exsentences":8,
    }, headers=HEADERS_JSON, retries=0)
    if r is None:
        return None
    try:
        pages = r.json().get("query",{}).get("pages",{})
        for pid, pdata in pages.items():
            if pid != "-1":
                return pdata.get("extract","")
    except Exception:
        pass
    return None

# ============================================================
# SOURCE 5 - IFRC GO
# ============================================================

def query_ifrc_go(disaster_type, country, start_date):
    start_w, end_w = get_date_window(start_date, 45)
    results = []
    country_lower = country.lower()
    # IFRC GO: /event/ is the correct endpoint; /disaster/ returns 404.
    # The relevant date field is "created_at" (emergency creation date).
    for endpoint in ["event"]:
        r = safe_get(f"https://goadmin.ifrc.org/api/v2/{endpoint}/", params={
            "created_at__gte": start_w,
            "created_at__lte": end_w,
            "limit":50,"format":"json",
        }, headers=HEADERS_JSON)
        if r is None:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        for item in data.get("results",[]):
            name    = item.get("name","") or ""
            summary = item.get("summary","") or item.get("description","") or name
            countries_in = [c.get("name","").lower() for c in item.get("countries",[])]
            if not (country_lower in name.lower() or country_lower in summary.lower()
                    or any(country_lower in c for c in countries_in)):
                continue
            if is_relevant(name, summary, country, start_date, disaster_type,
                           window_days=45):
                eid = item.get("id","")
                results.append(make_article("IFRC GO", name,
                    f"https://go.ifrc.org/emergencies/{eid}", summary))
        if results:
            log_source("IFRC GO", len(results))
            return results
    return results

# ============================================================
# SOURCE 6 - ERCC / COPERNICUS
# ============================================================

def query_ercc(disaster_type, country, start_date):
    year, _, _, _ = parse_date_parts(start_date)
    clean_type = disaster_type.split("(")[0].strip()
    # ERCC: the /Publications endpoint returns 404. Use the public ECHO RSS feed.
    # Fallback to Google News with site:erccportal.jrc.ec.europa.eu.
    r = safe_get(
        "https://erccportal.jrc.ec.europa.eu/api/v1/publications",
        params={"search":f"{country} {clean_type} {year}","page":0,"size":5},
        headers=HEADERS_JSON)
    if r is None:
        return []
    results = []
    try:
        data = r.json()
        for item in data.get("items", data.get("results",[])):
            title    = item.get("title", item.get("name",""))
            item_url = item.get("url", item.get("link","https://erccportal.jrc.ec.europa.eu"))
            desc     = item.get("description", item.get("abstract",""))
            pub      = item.get("date","")
            if is_relevant(title, desc, country, start_date, disaster_type,
                           pub_date_str=pub, window_days=30):
                results.append(make_article("ERCC Copernicus", title, item_url, desc))
    except Exception:
        pass
    log_source("ERCC", len(results))
    return results


# ============================================================
# SOURCE 8 - WMO
# ============================================================

def query_wmo(disaster_type, country, start_date):
    year, _, _, _ = parse_date_parts(start_date)
    clean_type = disaster_type.split("(")[0].strip()
    # WMO redesigned its site; try both the old and new search paths.
    results = []
    for wmo_url, wmo_params in [
        ("https://wmo.int/search",
         {"query": f"{country} {clean_type} {year}"}),
        ("https://public.wmo.int/en/search",
         {"query": f"{country} {clean_type} {year}"}),
    ]:
        r = safe_get(wmo_url, params=wmo_params, headers=HEADERS)
        if r is None:
            continue
        for path, title_raw in re.findall(
                r'href="(/[^"]*news[^"]*)"[^>]*>(.*?)</(?:a|span)', r.text, re.DOTALL):
            title = clean_html(title_raw).strip()
            if len(title) < 5:
                continue
            base = wmo_url.split("/search")[0]
            if is_relevant(title, title, country, start_date, disaster_type, window_days=30):
                results.append(make_article("WMO", title, f"{base}{path}", title))
            if len(results) >= 3:
                break
        if results:
            break

    # Fallback: Google News with site:wmo.int.
    if not results:
        q = f'site:wmo.int "{country}" {clean_type} {year}'
        results.extend(_google_news_rss(q, country, start_date, disaster_type,
                                        window_days=30, max_results=3,
                                        source_label="WMO"))

    log_source("WMO", len(results))
    return results

# ============================================================
# SOURCE 9 - ADPC / AHA CENTRE (Asia-Pacific)
# ============================================================

def query_adpc(disaster_type, country, start_date):
    year, _, _, _ = parse_date_parts(start_date)
    clean_type = disaster_type.split("(")[0].strip()
    results = []

    r = safe_get("https://www.adrc.asia/view_disaster_en.php",
                 params={"lang":"eng","SearchWord":country,"Year":year},
                 headers=HEADERS)
    if r is not None:
        for path, title_raw in re.findall(
                r'<a href="(view_disaster_en\.php\?[^"]+)"[^>]*>(.*?)</a>',
                r.text, re.DOTALL)[:8]:
            title = clean_html(title_raw).strip()
            if is_relevant(title, title, country, start_date, disaster_type,
                           window_days=45):
                results.append(make_article("ADRC Asia", title,
                    f"https://www.adrc.asia/{path}", title))

    r2 = safe_get("https://ahacentre.org/situation-report/",
                  params={"s":f"{country} {clean_type} {year}"}, headers=HEADERS)
    if r2 is not None:
        for link, title_raw in re.findall(
                r'<a[^>]+href="(https://ahacentre\.org/[^"]+)"[^>]*>(.*?)</a>',
                r2.text, re.DOTALL)[:8]:
            title = clean_html(title_raw).strip()
            if len(title) < 5:
                continue
            if is_relevant(title, title, country, start_date, disaster_type,
                           window_days=45):
                results.append(make_article("AHA Centre", title, link, title))

    log_source("ADPC/AHA", len(results))
    return results

# ============================================================
# SOURCE 1 - RELIEFWEB (reports + disasters + updates)
# ============================================================

def query_africa_hazards(disaster_type, country, start_date):
    year, _, _, _ = parse_date_parts(start_date)
    clean_type = disaster_type.split("(")[0].strip()
    results = []

    r = safe_get("https://africahazards.watch/country-profiles",
                 params={"country":country,"year":year,"hazard":clean_type},
                 headers=HEADERS)
    if r is not None:
        for link, title_raw in re.findall(
                r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.DOTALL)[:8]:
            title = clean_html(title_raw).strip()
            if is_relevant(title, title, country, start_date, disaster_type,
                           window_days=45):
                results.append(make_article("Africa Hazards Watch", title, link, title))

    r2 = safe_get("https://api.reliefweb.int/v2/reports", params={
        "appname": "Unical-EnvironmentalCausalDataset-432353",
        "query[value]": f"{country} {clean_type} {year}",
        "filter[field]": "primary_region.name",
        "filter[value]": "Africa",
        "fields[include][]": ["title","url","body","date"],
        "limit": 3,
    }, headers=HEADERS_JSON)
    if r2 is not None:
        try:
            for item in r2.json().get("data",[]):
                f     = item.get("fields",{})
                title = f.get("title","")
                body  = clean_html(f.get("body","") or "")
                pub   = (f.get("date") or {}).get("created","")
                if is_relevant(title, body, country, start_date, disaster_type,
                               pub_date_str=pub, window_days=45):
                    results.append(make_article("ReliefWeb Africa", title,
                        f.get("url","https://reliefweb.int"), body))
        except Exception:
            pass

    log_source("Africa Hazards/ReliefWeb Africa", len(results))
    return results

# ============================================================
# SOURCE 11 - PAHO / NOAA (Americas)
# ============================================================

def query_paho_noaa(disaster_type, country, start_date):
    year, month_num, mname, _ = parse_date_parts(start_date)
    clean_type = disaster_type.split("(")[0].strip()
    results = []

    r = safe_get("https://www.paho.org/en/search",
                 params={"q":f"{country} {clean_type} {year}",
                         "f[0]":"category:emergencies"}, headers=HEADERS)
    if r is not None:
        for link, title in re.findall(
                r'href="(https://www\.paho\.org/en/[^"]+)"[^>]*>\s*([^<]{10,150})',
                r.text)[:8]:
            title = title.strip()
            if is_relevant(title, title, country, start_date, disaster_type,
                           window_days=45):
                results.append(make_article("PAHO", title, link, title))

    r2 = safe_get(
        f"https://www.ncei.noaa.gov/access/monitoring/monthly-report/global/{year}{month_num}",
        headers=HEADERS)
    if r2 is not None and country.lower() in r2.text.lower():
        idx = r2.text.lower().find(country.lower())
        snippet = clean_html(r2.text[max(0,idx-100):idx+400])
        if is_relevant(country, snippet, country, start_date, disaster_type,
                       window_days=45):
            results.append(make_article("NOAA Climate Report",
                f"NOAA Global Climate Report {mname} {year}",
                f"https://www.ncei.noaa.gov/access/monitoring/monthly-report/global/{year}{month_num}",
                snippet))

    log_source("PAHO/NOAA", len(results))
    return results

# ============================================================
# SOURCE 12 - CIMA / METEOALARM (Europe)
# ============================================================

def query_cima_meteoalarm(disaster_type, country, start_date):
    year, _, _, _ = parse_date_parts(start_date)
    clean_type = disaster_type.split("(")[0].strip()
    results = []

    r = safe_get("https://www.cimafoundation.org/news/",
                 params={"s":f"{country} {clean_type} {year}"}, headers=HEADERS)
    if r is not None:
        for link, title_raw in re.findall(
                r'<a[^>]+href="(https://www\.cimafoundation\.org/[^"]+)"[^>]*>(.*?)</a>',
                r.text, re.DOTALL)[:8]:
            title = clean_html(title_raw).strip()
            if is_relevant(title, title, country, start_date, disaster_type,
                           window_days=45):
                results.append(make_article("CIMA Research", title, link, title))

    r2 = safe_get("https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-europe",
                  headers=HEADERS_XML)
    if r2 is not None:
        for entry in re.findall(r"<entry>(.*?)</entry>", r2.text, re.DOTALL)[:30]:
            title_m   = re.search(r"<title>(.*?)</title>", entry)
            link_m    = re.search(r'<link[^>]+href="([^"]+)"', entry)
            updated_m = re.search(r"<updated>(.*?)</updated>", entry)
            if not title_m:
                continue
            title = clean_html(title_m.group(1))
            link  = link_m.group(1) if link_m else "https://www.meteoalarm.org"
            pub   = updated_m.group(1) if updated_m else ""
            if is_relevant(title, title, country, start_date, disaster_type,
                           pub_date_str=pub, window_days=30):
                results.append(make_article("MeteoAlarm", title, link, title))

    log_source("CIMA/MeteoAlarm", len(results))
    return results

# ============================================================
# SOURCE 13 - GOOGLE NEWS RSS (general search)
# ============================================================

def query_google_news(disaster_type, country, start_date, location_context=None):
    year, _, mname, _ = parse_date_parts(start_date)
    primary = get_search_terms(disaster_type)[0]

    try:
        event_dt  = datetime.strptime(start_date, "%Y-%m-%d")
        after_dt  = (event_dt - timedelta(days=21)).strftime("%Y-%m-%d")
        before_dt = (event_dt + timedelta(days=21)).strftime("%Y-%m-%d")
        date_filter = f"after:{after_dt} before:{before_dt}"
    except Exception:
        date_filter = year

    queries = build_event_search_queries(
        disaster_type,
        country,
        start_date,
        location_context=location_context,
        include_date_filter=True,
        max_queries=8,
    )

    results = []
    for i, q in enumerate(queries):
        if i > 0:
            time.sleep(2)  # delay between successive queries to avoid 429
        found = _google_news_rss(q, country, start_date, disaster_type,
                                 window_days=21, max_results=5,
                                 source_label="Google News")
        results.extend(found)
        if results:
            break

    log_source("Google News", len(results))
    return results

# ============================================================
# SOURCE 14 - DUCKDUCKGO (last resort)
# ============================================================

def _decode_duckduckgo_url(link):
    if not link:
        return ""
    if "uddg=" in link:
        try:
            return urllib.parse.unquote(
                urllib.parse.parse_qs(
                    urllib.parse.urlparse(link).query
                ).get("uddg", [link])[0]
            )
        except Exception:
            pass
    if link.startswith("//"):
        return f"https:{link}"
    return link


def _parse_duckduckgo_results(html):
    results = []
    blocks = re.findall(
        r'<div[^>]+class="[^"]*result[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html,
        re.DOTALL,
    )
    if not blocks:
        blocks = re.findall(
            r'<div[^>]+class="[^"]*web-result[^"]*"[^>]*>(.*?)</div>\s*</div>',
            html,
            re.DOTALL,
        )

    for block in blocks:
        title_match = re.search(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            re.DOTALL,
        )
        snippet_match = re.search(
            r'<(?:a|div)[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>',
            block,
            re.DOTALL,
        )

        if not title_match:
            continue

        url = _decode_duckduckgo_url(title_match.group(1))
        title = clean_html(title_match.group(2))
        snippet = clean_html(snippet_match.group(1)) if snippet_match else ""
        if title and url:
            results.append((title, url, snippet))

    return results


def _duckduckgo_query_specs(disaster_type, country, start_date, location_context):
    year, _, month_name, _ = parse_date_parts(start_date)
    primary = get_search_terms(disaster_type)[0]
    clean_type = disaster_type.split("(")[0].strip()
    country_name = _country_variants(country)[0]
    event_terms = _compact_terms(_event_terms(location_context, include_aliases=True), limit=4)
    location_terms = _compact_terms(_location_terms(location_context), limit=6)

    specs = []

    def add(query, precision):
        normalized = re.sub(r"\s+", " ", query).strip()
        if not normalized:
            return
        if normalized.lower() not in {item["query"].lower() for item in specs}:
            specs.append({"query": normalized, "precision": precision})

    reliable_sites = [
        "reliefweb.int",
        "floodlist.com",
        "gdacs.org",
        "ifrc.org",
        "earthobservatory.nasa.gov",
    ]

    for event in event_terms:
        add(f'"{event}" "{country_name}" {primary} {year}', "high")
        add(f'"{event}" disaster {year}', "high")
        for site in reliable_sites[:3]:
            add(f'site:{site} "{event}" "{country_name}" {year}', "high")

    for place in location_terms:
        add(f'"{place}" "{country_name}" {primary} {month_name} {year}', "high")
        add(f'"{place}" "{country_name}" {clean_type} {year}', "high")
        add(f'"{place}" {primary} disaster {year}', "medium")
        for site in reliable_sites[:3]:
            add(f'site:{site} "{place}" "{country_name}" {primary} {year}', "high")

    add(f'"{country_name}" "{primary}" "{month_name}" {year} disaster', "medium")
    add(f'"{country_name}" "{clean_type}" {year} disaster', "low")

    return specs[:18]


def query_duckduckgo(disaster_type, country, start_date, location_context=None):
    results = []
    seen = set()
    query_specs = _duckduckgo_query_specs(
        disaster_type,
        country,
        start_date,
        location_context or {},
    )

    for spec in query_specs:
        q = spec["query"]
        r = safe_get("https://html.duckduckgo.com/html/",
                     params={"q": q}, headers=HEADERS)
        if r is None:
            continue

        for title, link, snippet in _parse_duckduckgo_results(r.text):
            key = link.rstrip("/") or title.lower()
            if key in seen:
                continue
            seen.add(key)

            raw_text = " ".join(part for part in [title, snippet] if part)
            if is_relevant(title, raw_text, country, start_date, disaster_type,
                           window_days=30,
                           location_context=location_context):
                article = make_article("DuckDuckGo", title, link, raw_text)
                article["search_query"] = q
                article["query_precision"] = spec["precision"]
                results.append(article)

            if len(results) >= 3:
                log_source("DuckDuckGo", len(results))
                return results
        if results:
            break
        time.sleep(1)

    log_source("DuckDuckGo", len(results))
    return results

# ============================================================
# DEDUPLICATION
# ============================================================

def deduplicate(articles):
    seen_urls, seen_titles, unique = set(), set(), []
    for a in articles:
        url  = a.get("url","").strip().rstrip("/")
        norm = re.sub(r"\s+", " ", a.get("title","")).strip().lower()
        if url in seen_urls or (norm and norm in seen_titles):
            continue
        seen_urls.add(url)
        if norm:
            seen_titles.add(norm)
        unique.append(a)
    return unique


def _source_family(source):
    source = str(source or "")
    google_match = re.match(r"Google News \((.*?)\)", source)
    if google_match:
        return google_match.group(1).strip()
    for prefix in DIRECT_SOURCE_PRIORITY:
        if source.startswith(prefix):
            return prefix
    return source


def _source_priority(source):
    source = str(source or "")
    if source.startswith("Google News"):
        return DIRECT_SOURCE_PRIORITY["Google News"]
    family = _source_family(source)
    for prefix, priority in DIRECT_SOURCE_PRIORITY.items():
        if family.startswith(prefix):
            return priority
    return 50


def _canonical_title(title):
    title = clean_html(title)
    title = re.sub(r"\s*[-|]\s*[^-|]{3,60}$", "", title)
    title = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return re.sub(r"\s+", " ", title).strip()


def _article_identity_keys(article):
    title_key = _canonical_title(article.get("title", ""))
    source_family = _source_family(article.get("source", "")).lower()
    keys = []
    if title_key:
        keys.append(("title", title_key))
        if source_family:
            keys.append(("source_title", source_family, title_key))

    url = str(article.get("url", "")).strip().rstrip("/")
    if url and "news.google.com/rss/articles/" not in url:
        keys.append(("url", url.lower()))

    return keys


def deduplicate_prefer_direct_sources(articles):
    by_key = {}
    output = []
    direct_source_families = {
        _source_family(article.get("source", "")).lower()
        for article in articles
        if not str(article.get("source", "")).startswith("Google News")
    }

    def article_rank(article):
        confidence_rank = {"high": 3, "medium": 2, "low": 1}
        return (
            _source_priority(article.get("source", "")),
            confidence_rank.get(article.get("confidence", "low"), 0),
            article.get("relevance_score", 0),
        )

    for article in articles:
        source = str(article.get("source", ""))
        source_family = _source_family(source).lower()
        if source.startswith("Google News") and source_family in direct_source_families:
            continue

        keys = _article_identity_keys(article)
        matching_indexes = [
            by_key[key] for key in keys
            if key in by_key
        ]
        if not matching_indexes:
            output.append(article)
            index = len(output) - 1
            for key in keys:
                by_key[key] = index
            continue

        index = matching_indexes[0]
        current = output[index]
        if article_rank(article) > article_rank(current):
            replacement = dict(article)
            replacement["deduplicated_from"] = current.get("source", "")
            output[index] = replacement
            for key in keys:
                by_key[key] = index
        else:
            output[index].setdefault("deduplicated_sources", [])
            output[index]["deduplicated_sources"].append(article.get("source", ""))

    return output


def _extract_text_from_html(html_text):
    html_text = str(html_text or "")
    html_text = re.sub(r"(?is)<script.*?</script>", " ", html_text)
    html_text = re.sub(r"(?is)<style.*?</style>", " ", html_text)
    html_text = re.sub(r"(?is)<noscript.*?</noscript>", " ", html_text)
    html_text = re.sub(r"(?is)<svg.*?</svg>", " ", html_text)
    html_text = re.sub(r"(?is)<(nav|header|footer|aside|form)[^>]*>.*?</\1>", " ", html_text)

    chunks = []
    for pattern in [
        r"(?is)<article[^>]*>(.*?)</article>",
        r"(?is)<main[^>]*>(.*?)</main>",
    ]:
        chunks.extend(re.findall(pattern, html_text))

    if not chunks:
        chunks = re.findall(r"(?is)<p[^>]*>(.*?)</p>", html_text)

    if not chunks:
        chunks = re.findall(r"(?is)<div[^>]*>(.*?)</div>", html_text)

    cleaned_chunks = []
    for chunk in chunks:
        chunk = re.sub(r"(?i)<br\s*/?>", " ", chunk)
        chunk = re.sub(r"(?i)</(p|div|h[1-6]|li)>", " ", chunk)
        text = html_lib.unescape(clean_html(chunk))
        text = normalize_whitespace(text)
        if len(text) >= 50:
            cleaned_chunks.append(text)

    return normalize_whitespace(" ".join(cleaned_chunks))


def _article_text_fetch_allowed(article):
    url = str(article.get("url", ""))
    if not url.startswith("http"):
        return False
    if "news.google.com/rss/articles/" in url:
        return False
    if article.get("is_non_article_candidate"):
        return False
    if url.lower().endswith((".pdf", ".zip", ".jpg", ".jpeg", ".png")):
        return False
    return True


def _is_consent_or_block_page(url, text):
    url = str(url or "").lower()
    text = normalize_whitespace(text).lower()
    consent_markers = [
        "consent.google.com",
        "if you choose to accept all",
        "if you choose to reject all",
        "manage your privacy settings",
        "we use cookies and data",
        "before you continue",
        "enable javascript and cookies",
    ]
    return any(marker in url or marker in text for marker in consent_markers)


def enrich_article_raw_text(article):
    enriched = dict(article)
    current_text = normalize_whitespace(enriched.get("raw_text", ""))
    enriched["raw_text_length"] = len(current_text)

    if enriched.get("raw_text_status") == "fetched_full_text":
        return enriched

    if not ENRICH_ARTICLE_RAW_TEXT:
        enriched["raw_text_status"] = "disabled"
        return enriched

    if not _article_text_fetch_allowed(enriched):
        if "news.google.com/rss/articles/" in str(enriched.get("url", "")):
            enriched["raw_text_status"] = "skipped_google_news_redirect"
        else:
            enriched["raw_text_status"] = "skipped"
        return enriched

    response = safe_get(
        enriched.get("url", ""),
        headers=HEADERS,
        timeout=ARTICLE_TEXT_TIMEOUT,
        retries=0,
    )
    if response is None:
        enriched["raw_text_status"] = "fetch_failed"
        return enriched

    if _is_consent_or_block_page(response.url, response.text):
        enriched["raw_text_status"] = "blocked_or_consent_page"
        return enriched

    content_type = response.headers.get("Content-Type", "").lower()
    if "html" not in content_type and "text" not in content_type:
        enriched["raw_text_status"] = "unsupported_content_type"
        return enriched

    extracted_text = _extract_text_from_html(response.text)
    if _is_consent_or_block_page(response.url, extracted_text):
        enriched["raw_text_status"] = "blocked_or_consent_page"
        return enriched

    if len(extracted_text) < ARTICLE_TEXT_MIN_CHARS or len(extracted_text) <= len(current_text):
        enriched["raw_text_status"] = "kept_original"
        return enriched

    enriched["raw_text"] = extracted_text[:ARTICLE_TEXT_MAX_CHARS]
    enriched["raw_text_length"] = len(enriched["raw_text"])
    enriched["raw_text_status"] = "fetched_full_text"
    enriched["raw_text_url"] = response.url
    return enriched


def _resolve_and_extract_text(url, fallback_text=""):
    try:
        final_url, extracted_text = _extra_resolve_and_extract_text(
            url,
            fallback_text=fallback_text,
        )
        if (
            extracted_text
            and len(extracted_text) > len(normalize_whitespace(fallback_text))
            and not _is_consent_or_block_page(final_url, extracted_text)
        ):
            return final_url, extracted_text[:ARTICLE_TEXT_MAX_CHARS]
    except Exception:
        pass

    response = safe_get(
        url,
        headers=HEADERS,
        timeout=ARTICLE_TEXT_TIMEOUT,
        retries=0,
    )
    if response is None:
        return url, fallback_text

    if _is_consent_or_block_page(response.url, response.text):
        return url, fallback_text

    content_type = response.headers.get("Content-Type", "").lower()
    if "html" not in content_type and "text" not in content_type:
        return url, fallback_text

    extracted_text = _extract_text_from_html(response.text)
    if len(extracted_text) > len(normalize_whitespace(fallback_text)):
        return response.url, extracted_text[:ARTICLE_TEXT_MAX_CHARS]

    return url, fallback_text


def enrich_final_articles_raw_text(articles):
    enriched_articles = []
    fetched = 0

    for article in articles:
        if fetched >= ARTICLE_TEXT_FETCH_LIMIT_PER_EVENT:
            skipped = dict(article)
            skipped["raw_text_length"] = len(normalize_whitespace(skipped.get("raw_text", "")))
            skipped["raw_text_status"] = "skipped_fetch_limit"
            enriched_articles.append(skipped)
            continue

        enriched = enrich_article_raw_text(article)
        if enriched.get("raw_text_status") in {
            "fetched_full_text",
            "fetch_failed",
            "kept_original",
            "unsupported_content_type",
        }:
            fetched += 1
        enriched_articles.append(enriched)

    return enriched_articles


def _is_weak_google_news_final_article(article):
    source = str(article.get("source", ""))
    if not source.startswith("Google News"):
        return False

    penalties = set(article.get("relevance_penalties") or [])
    raw_text_status = article.get("raw_text_status")
    has_full_text = raw_text_status == "fetched_full_text"

    if article.get("confidence") == "low":
        return True

    if (
        not has_full_text
        and "missing_local_or_event_context" in penalties
    ):
        return True

    return False


def _is_generic_wikipedia_final_article(article):
    if str(article.get("source", "")) != "Wikipedia":
        return False

    title = normalize_whitespace(article.get("title", "")).lower()
    url = str(article.get("url", "")).lower()
    penalties = set(article.get("relevance_penalties") or [])

    generic_title_prefixes = (
        "list of ",
        "timeline of ",
        "index of ",
        "outline of ",
    )
    generic_url_markers = (
        "/list_of_",
        "/timeline_of_",
        "/category:",
    )

    if title.startswith(generic_title_prefixes):
        return True

    if any(marker in url for marker in generic_url_markers):
        return True

    if (
        article.get("confidence") == "low"
        and "missing_local_or_event_context" in penalties
    ):
        return True

    return False


def filter_final_articles_for_precision(articles):
    kept = []
    removed = []

    for article in articles:
        if (
            _is_weak_google_news_final_article(article)
            or _is_generic_wikipedia_final_article(article)
        ):
            removed.append(article)
        else:
            kept.append(article)

    return kept, removed


def _source_threshold(source_name, default_threshold):
    source_name = str(source_name or "")
    for prefix, threshold in SOURCE_RELEVANCE_THRESHOLDS.items():
        if source_name.startswith(prefix):
            return threshold
    return default_threshold


def _article_relevance_threshold(article, default_threshold):
    threshold = _source_threshold(article.get("source", ""), default_threshold)

    if str(article.get("source", "")).startswith("DuckDuckGo"):
        precision = article.get("query_precision")
        if precision == "high":
            threshold = min(threshold, 6)
        elif precision == "low":
            threshold += 1

    return threshold


def _is_non_article_result(article):
    title = str(article.get("title", ""))
    url = str(article.get("url", ""))
    return (
        any(pat.search(url) for pat in NON_ARTICLE_URL_RE)
        or any(pat.search(title) for pat in NON_ARTICLE_TITLE_RE)
    )


def _article_confidence(article, scoring, threshold):
    if article.get("is_non_article_candidate"):
        return "low"

    source = str(article.get("source", ""))
    score = scoring["score"]
    reasons = set(scoring.get("reasons", []))
    penalties = set(scoring.get("penalties", []))

    strong_reasons = {
        "event_name_in_title",
        "event_name_in_text",
        "local_place_in_title",
        "publication_date_near_event",
    }
    risky_penalties = {
        "missing_local_or_event_context",
        "generic_title",
        "different_year_in_title",
        "publication_date_far_from_event",
    }

    if score >= threshold + 4 and reasons & strong_reasons and not penalties:
        return "high"

    if source.startswith(("ReliefWeb", "GDACS", "IFRC GO", "FloodList")):
        if score >= threshold + 2 and not (penalties & risky_penalties):
            return "high"
        return "medium"

    if source.startswith(("Google News", "DuckDuckGo", "Wikipedia")):
        if penalties & risky_penalties:
            return "low"
        if score >= threshold + 3 and reasons & strong_reasons:
            return "high"
        return "medium"

    if penalties & risky_penalties:
        return "low"
    if score >= threshold + 3:
        return "high"
    return "medium"


def filter_and_score_articles(articles, country, start_date, disaster_type,
                              location_context=None):
    scored_articles = []
    for article in articles:
        is_non_article = _is_non_article_result(article)

        scoring = score_relevance(
            title=article.get("title", ""),
            text=article.get("raw_text", ""),
            country=country,
            start_date=start_date,
            disaster_type=disaster_type,
            pub_date_str=(
                article.get("published_date")
                or article.get("date")
                or article.get("seendate")
            ),
            location_context=location_context,
            window_days=90,
        )
        threshold = _article_relevance_threshold(article, scoring["threshold"])
        if scoring["score"] < threshold:
            continue

        enriched = dict(article)
        if is_non_article:
            enriched["is_non_article_candidate"] = True
        enriched["relevance_score"] = scoring["score"]
        enriched["relevance_threshold"] = threshold
        enriched["relevance_reasons"] = scoring["reasons"]
        enriched["confidence"] = _article_confidence(enriched, scoring, threshold)
        if scoring["penalties"]:
            enriched["relevance_penalties"] = scoring["penalties"]
        scored_articles.append(enriched)

    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    return sorted(
        deduplicate(scored_articles),
        key=lambda item: (
            confidence_rank.get(item.get("confidence", "low"), 0),
            item.get("relevance_score", 0),
        ),
        reverse=True,
    )

# ============================================================
# ORCHESTRATOR - all listed sources
# ============================================================

def get_all_news_sources(disaster_type, country, start_date, region, lat, lon,
                         reliefweb_appname="Unical-EnvironmentalCausalDataset-432353",
                         location_context=None,
                         source_mode="all"):
    """
    Query all required sources for the specific event.

    DuckDuckGo is used only as a last resort, when all
    global and regional sources returned zero valid articles.
    """
    articles = []
    source_results = {}
    source_errors = {}
    location_context = location_context or {}

    if source_mode == "gdelt-only":
        global_sources = [
            ("GDELT DOC 2.0", lambda: query_gdelt_doc(
                disaster_type,
                country,
                start_date,
                location_context=location_context,
            )),
        ]
        regional_sources = []
    else:
        global_sources = [
            ("ReliefWeb", lambda: query_reliefweb(
                disaster_type, country, start_date, reliefweb_appname,
                location_context=location_context)),
            ("GDACS", lambda: query_gdacs(disaster_type, country, start_date)),
            ("NASA Earth Observatory/EONET", lambda: query_nasa_eonet(
                disaster_type, country, start_date)),
            ("Wikipedia", lambda: query_wikipedia(
                disaster_type, country, start_date, location_context=location_context)),
            ("Google News RSS", lambda: query_google_news(
                disaster_type, country, start_date, location_context=location_context)),
            ("GDELT DOC 2.0", lambda: query_gdelt_doc(
                disaster_type,
                country,
                start_date,
                location_context=location_context,
            )),
            ("IFRC GO", lambda: query_ifrc_go(disaster_type, country, start_date)),
            ("ERCC portal", lambda: query_ercc(disaster_type, country, start_date)),
            ("FloodList", lambda: query_floodlist(
                disaster_type, country, start_date, location_context=location_context)),
            ("WMO", lambda: query_wmo(disaster_type, country, start_date)),
        ]

        regional_sources = [
            ("ADPC/AHA Centre", lambda: query_adpc(disaster_type, country, start_date)),
            ("Africa Hazards Watch/SADRI", lambda: query_africa_hazards(
                disaster_type, country, start_date)),
            ("PAHO/NOAA", lambda: query_paho_noaa(disaster_type, country, start_date)),
            ("CIMA Research Foundation/MeteoAlarm", lambda: query_cima_meteoalarm(
                disaster_type, country, start_date)),
        ]

    for source_name, query_fn in global_sources + regional_sources:
        try:
            found = filter_and_score_articles(
                deduplicate(query_fn() or []),
                country=country,
                start_date=start_date,
                disaster_type=disaster_type,
                location_context=location_context,
            )
        except Exception as exc:
            found = []
            source_errors[source_name] = f"{type(exc).__name__}: {exc}"
            print(f"    [WARN] {source_name} failed: {source_errors[source_name]}")

        print(f"    [{source_name}] -> {len(found)} validated articles")
        source_results[source_name] = {
            "queried": True,
            "articles_retrieved": len(found),
            "articles": found,
        }
        articles.extend(found)

    articles = deduplicate_prefer_direct_sources(deduplicate(articles))
    duckduckgo_used = False

    if len(articles) == 0 and source_mode != "gdelt-only":
        duckduckgo_used = True
        try:
            ddg_articles = filter_and_score_articles(
                deduplicate(query_duckduckgo(
                    disaster_type,
                    country,
                    start_date,
                    location_context=location_context
                ) or []),
                country=country,
                start_date=start_date,
                disaster_type=disaster_type,
                location_context=location_context,
            )
        except Exception as exc:
            ddg_articles = []
            source_errors["DuckDuckGo"] = f"{type(exc).__name__}: {exc}"
            print(f"    [WARN] DuckDuckGo failed: {source_errors['DuckDuckGo']}")

        source_results["DuckDuckGo"] = {
            "queried": True,
            "articles_retrieved": len(ddg_articles),
            "articles": ddg_articles,
        }
        articles = deduplicate_prefer_direct_sources(deduplicate(ddg_articles))
    elif source_mode != "gdelt-only":
        source_results["DuckDuckGo"] = {
            "queried": False,
            "articles_retrieved": 0,
            "articles": [],
        }
    else:
        source_results["DuckDuckGo"] = {
            "queried": False,
            "articles_retrieved": 0,
            "articles": [],
        }

    articles = enrich_final_articles_raw_text(articles)
    articles, filtered_articles = filter_final_articles_for_precision(articles)
    resolved = sorted(set(a["source"] for a in articles))

    return {
        "search_metadata": {
            "global_sources_queried": [name for name, _ in global_sources],
            "regional_sources_queried": [name for name, _ in regional_sources],
            "fallback_sources_queried": ["DuckDuckGo"] if duckduckgo_used else [],
            "duckduckgo_used": duckduckgo_used,
            "total_articles_retrieved": len(articles),
            "sources_successfully_resolved": resolved,
            "source_errors": source_errors,
            "source_mode": source_mode,
            "final_articles_filtered": len(filtered_articles),
            "filtered_google_news_articles": len([
                article for article in filtered_articles
                if str(article.get("source", "")).startswith("Google News")
            ]),
            "filtered_wikipedia_articles": len([
                article for article in filtered_articles
                if str(article.get("source", "")) == "Wikipedia"
            ]),
            "news_engine_version": NEWS_ENGINE_VERSION,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        },
        "source_results": source_results,
        "articles": articles,
    }


# Console-safe helpers for Windows terminals using legacy encodings.
def log_source(source_name, count):
    print(f"    [{source_name}] -> {count} articles found")


def safe_get(url, params=None, headers=None, timeout=10, retries=3):
    """
    GET with automatic retry handling for 429 and 503.
    This version uses ASCII-only logs so queries do not
    fail on Windows CP1252 consoles.
    """
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers or HEADERS,
                             timeout=timeout, allow_redirects=True)
            if r.status_code in (429, 503):
                if attempt >= retries:
                    print(f"    [WARN] {url[:50]} -> {r.status_code}, skipped")
                    return None
                wait = 3 * (attempt + 1)
                print(f"    [WARN] {url[:50]} -> {r.status_code}, wait {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code == 403:
                print(f"    [WARN] {url[:60]} -> 403 blocked")
                return None
            r.raise_for_status()
            return r
        except requests.exceptions.Timeout:
            print(f"    [WARN] {url[:60]} -> timeout")
            return None
        except requests.exceptions.ConnectionError:
            print(f"    [WARN] {url[:60]} -> connection error")
            return None
        except Exception as e:
            print(f"    [WARN] {url[:60]} -> {type(e).__name__}: {e}")
            return None
    return None


def query_reliefweb(disaster_type, country, start_date,
                    appname="Unical-EnvironmentalCausalDataset-432353",
                    location_context=None):
    """
    ReliefWeb v8: use simple, valid API parameters, then apply
    the relevance filter locally to avoid 400 errors from nested filters.
    """
    year, _, mname, _ = parse_date_parts(start_date)
    start_w, end_w = get_date_window(start_date, 60)
    primary = get_search_terms(disaster_type)[0]
    clean_type = disaster_type.split("(")[0].strip()
    results = []
    seen = set()

    queries = build_event_search_queries(
        disaster_type,
        country,
        start_date,
        location_context=location_context,
        max_queries=10,
    )

    for endpoint in ["reports", "disasters"]:
        for query in queries:
            params = [
                ("appname", appname),
                ("query[value]", query),
                ("filter[field]", "primary_country.name" if endpoint == "reports" else "country.name"),
                ("filter[value]", country),
                ("limit", 10),
                ("profile", "full"),
                ("sort[]", "date:desc"),
            ]
            if endpoint == "reports":
                params.extend([
                    ("fields[include][]", "title"),
                    ("fields[include][]", "url"),
                    ("fields[include][]", "body"),
                    ("fields[include][]", "date"),
                    ("fields[include][]", "primary_country"),
                ])
            else:
                params.extend([
                    ("fields[include][]", "name"),
                    ("fields[include][]", "url"),
                    ("fields[include][]", "description"),
                    ("fields[include][]", "date"),
                    ("fields[include][]", "country"),
                    ("fields[include][]", "type"),
                ])

            r = safe_get(f"https://api.reliefweb.int/v2/{endpoint}",
                         params=params, headers=HEADERS_JSON)
            if r is None:
                continue

            try:
                items = r.json().get("data", [])
            except Exception:
                continue

            for item in items:
                fields = item.get("fields", {})
                if endpoint == "reports":
                    title = fields.get("title", "")
                    body = clean_html(fields.get("body", "") or "")
                    url = fields.get("url", "https://reliefweb.int")
                    pub = (fields.get("date") or {}).get("created", "")
                    source = "ReliefWeb"
                else:
                    title = fields.get("name", "")
                    raw_desc = fields.get("description", "") or title
                    body = clean_html(raw_desc)
                    url = fields.get("url", "https://reliefweb.int/disasters")
                    pub = (fields.get("date") or {}).get("event", "")
                    source = "ReliefWeb Disasters"

                    if "taxonomy/term" in url or "node/" in raw_desc or "report/" in raw_desc:
                        links = re.findall(
                            r"https://reliefweb\.int/(?:report|node)/[^\s\)\"\]]+",
                            raw_desc,
                        )
                        if links:
                            report_url = links[0].rstrip(").,")
                            final_link, full_text = _resolve_and_extract_text(
                                report_url,
                                fallback_text=body,
                            )
                            if len(full_text) > len(body):
                                body = full_text
                                url = final_link

                key = (source, title.lower(), url.rstrip("/"))
                if key in seen:
                    continue
                if pub and not (start_w <= pub[:10] <= end_w):
                    continue
                if is_relevant(title, body, country, start_date, disaster_type,
                               pub_date_str=pub, window_days=60,
                               location_context=location_context):
                    seen.add(key)
                    results.append(make_article(source, title, url, body))

    log_source("ReliefWeb", len(results))
    return deduplicate(results)


def query_gdacs(disaster_type, country, start_date):
    """
    GDACS v8: use the current public /xml/rss.xml feed and filter client-side.
    """
    type_map = {"flood": "FL", "storm": "TC", "earthquake": "EQ",
                "drought": "DR", "volcanic": "VO", "wildfire": "WF",
                "landslide": "FL"}
    clean = disaster_type.split("(")[0].strip().lower()
    gdacs_type = next((v for k, v in type_map.items() if k in clean), None)

    params = {"profile": "ARCHIVE", "alertlevel": "Green,Orange,Red"}
    if gdacs_type:
        params["eventtype"] = gdacs_type

    r = safe_get("https://www.gdacs.org/xml/rss.xml",
                 params=params, headers=HEADERS_XML)
    if r is None:
        return []

    results = []
    for item_text in re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL):
        title_m = re.search(r"<title>(.*?)</title>", item_text, re.DOTALL)
        link_m = re.search(r"<link>(.*?)</link>", item_text, re.DOTALL)
        desc_m = re.search(r"<description>(.*?)</description>", item_text, re.DOTALL)
        pub_m = re.search(r"<pubDate>(.*?)</pubDate>", item_text, re.DOTALL)
        if not title_m:
            continue

        title = clean_html(title_m.group(1))
        desc = clean_html(desc_m.group(1)) if desc_m else ""
        link = link_m.group(1).strip() if link_m else "https://www.gdacs.org"
        pub = pub_m.group(1) if pub_m else ""

        if is_relevant(title, desc, country, start_date, disaster_type,
                       pub_date_str=pub, window_days=60):
            results.append(make_article("GDACS", title, link, desc))

    log_source("GDACS", len(results))
    return deduplicate(results)


def _location_terms(location_context):
    terms = []
    if not location_context:
        return terms
    for key in ["event_name", "emdat_location", "geolocation", "location", "adm3", "adm2", "adm1"]:
        value = location_context.get(key)
        if _is_blank_context_value(value):
            continue
        for part in re.split(r"[,;/|]", str(value)):
            cleaned = part.strip()
            if cleaned and cleaned.lower() not in {t.lower() for t in terms}:
                terms.append(cleaned)
    return terms


def _compact_terms(terms, limit=6):
    compacted = []
    for term in terms:
        if _is_blank_context_value(term):
            continue
        cleaned = re.sub(r"\s+", " ", str(term)).strip()
        if len(cleaned) < 3:
            continue
        key = cleaned.lower()
        if key not in {item.lower() for item in compacted}:
            compacted.append(cleaned)
        if len(compacted) >= limit:
            break
    return compacted


def build_event_search_queries(disaster_type, country, start_date,
                               location_context=None,
                               include_date_filter=False,
                               max_queries=12):
    """
    Build high-to-low precision search queries for a disaster event.

    The query order favors event names and local places first, then falls back
    to country-level queries. This makes aggregated GDIS/EM-DAT locations
    actionable without storing them in the final JSON.
    """
    year, _, month_name, _ = parse_date_parts(start_date)
    primary = get_search_terms(disaster_type)[0]
    clean_type = disaster_type.split("(")[0].strip()
    event_terms = _compact_terms(_event_terms(location_context, include_aliases=True), limit=6)
    location_terms = _compact_terms(_location_terms(location_context), limit=8)
    country_name = _country_variants(country)[0]

    date_filter = ""
    if include_date_filter:
        try:
            event_dt = datetime.strptime(start_date, "%Y-%m-%d")
            after_dt = (event_dt - timedelta(days=30)).strftime("%Y-%m-%d")
            before_dt = (event_dt + timedelta(days=60)).strftime("%Y-%m-%d")
            date_filter = f" after:{after_dt} before:{before_dt}"
        except Exception:
            date_filter = ""

    queries = []

    def add(query):
        normalized = re.sub(r"\s+", " ", query).strip()
        if not normalized:
            return
        if normalized.lower() not in {item.lower() for item in queries}:
            queries.append(normalized)

    for event in event_terms:
        add(f'"{event}" "{country_name}" {primary} {year}{date_filter}')
        add(f'"{event}" "{country_name}" {clean_type} {year}{date_filter}')
        add(f'"{event}" disaster {year}{date_filter}')

    for place in location_terms:
        add(f'"{place}" "{country_name}" {primary} {month_name} {year}{date_filter}')
        add(f'"{place}" "{country_name}" {primary} {year}{date_filter}')
        add(f'"{place}" {primary} {year}{date_filter}')

    add(f'"{country_name}" "{primary}" {month_name} {year}{date_filter}')
    add(f'"{country_name}" {primary} {year}{date_filter}')
    add(f'{country_name} {clean_type} {year}{date_filter}')

    return queries[:max_queries]


def _gdelt_datetime_window(start_date, days_before=10, days_after=60):
    try:
        event_dt = datetime.strptime(start_date, "%Y-%m-%d")
    except Exception:
        return "", ""
    start_dt = event_dt - timedelta(days=days_before)
    end_dt = event_dt + timedelta(days=days_after)
    return start_dt.strftime("%Y%m%d%H%M%S"), end_dt.strftime("%Y%m%d%H%M%S")


def _gdelt_query_term(term):
    cleaned = normalize_whitespace(term).replace('"', " ")
    cleaned = normalize_whitespace(cleaned)
    if not cleaned:
        return ""
    if re.search(r"\s", cleaned):
        return f'"{cleaned}"'
    return cleaned


def _gdelt_or_block(terms):
    pieces = [_gdelt_query_term(term) for term in terms]
    pieces = [piece for piece in pieces if piece]
    if not pieces:
        return ""
    if len(pieces) == 1:
        return pieces[0]
    return "(" + " OR ".join(pieces) + ")"


def build_gdelt_doc_queries(disaster_type, country, location_context=None,
                            max_queries=8):
    hazard_block = _gdelt_or_block(_compact_terms(get_synonyms(disaster_type), limit=5))
    country_name = _country_variants(country)[0]
    country_term = _gdelt_query_term(country_name)
    event_terms = _compact_terms(_event_terms(location_context, include_aliases=True), limit=4)
    location_terms = _compact_terms(_location_terms(location_context), limit=5)

    queries = []

    def add(*parts):
        query = normalize_whitespace(" ".join(part for part in parts if part))
        if query and query.lower() not in {item.lower() for item in queries}:
            queries.append(query)

    for event in event_terms:
        add(_gdelt_query_term(event), country_term, hazard_block)

    for place in location_terms:
        add(_gdelt_query_term(place), country_term, hazard_block)

    add(country_term, hazard_block)
    return queries[:max_queries]


def query_gdelt_doc(disaster_type, country, start_date, location_context=None,
                    max_records=25):
    """
    Query GDELT DOC 2.0 ArticleList around the event date.

    GDELT searches machine-translated global coverage, so English disaster
    terms can retrieve local-language articles when they are in the index.
    """
    startdatetime, enddatetime = _gdelt_datetime_window(start_date)
    if not startdatetime or not enddatetime:
        log_source("GDELT DOC 2.0", 0)
        return []

    results = []
    seen = set()

    for query in build_gdelt_doc_queries(
        disaster_type,
        country,
        location_context=location_context,
    ):
        r = safe_get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": max_records,
                "sort": "datedesc",
                "startdatetime": startdatetime,
                "enddatetime": enddatetime,
            },
            headers=HEADERS_JSON,
            timeout=15,
            retries=2,
        )
        if r is None:
            continue

        try:
            payload = r.json()
        except Exception:
            continue

        for item in payload.get("articles") or []:
            if not isinstance(item, dict):
                continue

            title = clean_html(item.get("title", ""))
            url = item.get("url") or item.get("url_mobile") or ""
            if not title or not url:
                continue

            key = url.rstrip("/") or title.lower()
            if key in seen:
                continue

            published = item.get("seendate") or item.get("date") or ""
            domain = item.get("domain") or ""
            language = item.get("language") or item.get("sourcelang") or ""
            source_country = (
                item.get("sourcecountry")
                or item.get("sourceCountry")
                or item.get("source_country")
                or ""
            )
            raw_text = normalize_whitespace(
                " ".join(
                    str(part)
                    for part in [title, domain, language, source_country]
                    if part
                )
            )

            article = make_article("GDELT DOC 2.0", title, url, raw_text)
            article["published_date"] = published
            article["domain"] = domain
            article["language"] = language
            article["source_country"] = source_country
            article["search_query"] = query
            seen.add(key)
            results.append(article)

    log_source("GDELT DOC 2.0", len(results))
    return deduplicate(results)


def _floodlist_article_url(slug):
    return f"https://floodlist.com/america/{slug}"


def query_floodlist(disaster_type, country, start_date, location_context=None):
    """
    FloodList v8: search directly inside FloodList.

    The previous version only used Google News RSS with site:floodlist.com,
    which may miss historical articles that are still available on the site.
    """
    clean = disaster_type.split("(")[0].strip().lower()
    if not any(t in clean for t in ["flood", "storm", "landslide", "cyclone",
                                     "hurricane", "typhoon"]):
        log_source("FloodList", 0)
        return []

    year, _, mname, _ = parse_date_parts(start_date)
    primary = get_search_terms(disaster_type)[0]
    places = _location_terms(location_context)
    search_phrases = build_event_search_queries(
        disaster_type,
        country,
        start_date,
        location_context=location_context,
        max_queries=14,
    )

    results = []
    seen = set()

    for phrase in search_phrases:
        r = safe_get("https://floodlist.com/wp-json/wp/v2/posts",
                     params={
                         "search": phrase,
                         "per_page": 10,
                         "_fields": "date,link,slug,title,excerpt,content",
                     },
                     headers=HEADERS_JSON)
        if r is None:
            continue

        try:
            posts = r.json()
        except Exception:
            posts = []

        for post in posts:
            title = clean_html((post.get("title") or {}).get("rendered", ""))
            excerpt = clean_html((post.get("excerpt") or {}).get("rendered", ""))
            content = clean_html((post.get("content") or {}).get("rendered", ""))
            raw_text = " ".join([excerpt, content])[:2000]
            link = post.get("link") or _floodlist_article_url(post.get("slug", ""))
            pub = post.get("date", "")
            key = link.rstrip("/") or title.lower()

            if key in seen or not title:
                continue
            if is_relevant(title, raw_text, country, start_date, disaster_type,
                           pub_date_str=pub, window_days=90,
                           location_context=location_context):
                seen.add(key)
                results.append(make_article("FloodList", title, link, raw_text))

    if not results:
        for phrase in search_phrases[:6]:
            r = safe_get("https://floodlist.com/",
                         params={"s": phrase}, headers=HEADERS)
            if r is None:
                continue
            for link, title_raw in re.findall(
                    r'<a[^>]+href="(https://floodlist\.com/[^"]+)"[^>]*>(.*?)</a>',
                    r.text, re.DOTALL):
                title = clean_html(title_raw)
                key = link.rstrip("/")
                if key in seen or len(title) < 8:
                    continue
                if is_relevant(title, title, country, start_date, disaster_type,
                               window_days=90,
                               location_context=location_context):
                    seen.add(key)
                    results.append(make_article("FloodList", title, link, title))
                if len(results) >= 5:
                    break
            if results:
                break

    if not results:
        sitemap_url = f"https://floodlist.com/sitemap-posttype-post.{year}.xml"
        r = safe_get(sitemap_url, headers=FLOODLIST_SITEMAP_HEADERS)
        if r is not None:
            entries = re.findall(r"<url>(.*?)</url>", r.text, re.DOTALL)
            tokens = [country.lower(), primary.lower(), year.lower()]
            tokens.extend(p.lower() for p in places[:4])
            month_token = mname.lower()

            for entry in entries:
                loc_m = re.search(r"<loc>(.*?)</loc>", entry, re.DOTALL)
                lastmod_m = re.search(r"<lastmod>(.*?)</lastmod>", entry, re.DOTALL)
                if not loc_m:
                    continue

                link = clean_html(loc_m.group(1)).strip()
                link_lower = link.lower()
                slug = urllib.parse.urlparse(link).path.rstrip("/").split("/")[-1]
                title = slug.replace("-", " ").title()
                haystack = f"{link_lower} {title.lower()}"

                has_country = country.lower() in haystack
                has_disaster = any(s in haystack for s in get_synonyms(disaster_type))
                has_year = year in haystack
                has_place_or_month = (
                    any(p.lower() in haystack for p in places[:4])
                    or month_token in haystack
                )

                if not (has_country and has_disaster and has_year and has_place_or_month):
                    continue

                key = link.rstrip("/")
                if key in seen:
                    continue
                pub = lastmod_m.group(1) if lastmod_m else ""
                if is_relevant(title, title, country, start_date, disaster_type,
                               window_days=90,
                               location_context=location_context):
                    seen.add(key)
                    results.append(make_article("FloodList", title, link, title))

        if results:
            log_source("FloodList", len(results))
            return deduplicate(results)

    if not results:
        for phrase in search_phrases[:4]:
            q = f'site:floodlist.com "{phrase}"'
            results.extend(_google_news_rss(q, country, start_date, disaster_type,
                                            window_days=90, max_results=5,
                                            source_label="FloodList"))
            if results:
                break

    log_source("FloodList", len(results))
    return deduplicate(results)


def _country_variants(country):
    country_lower = str(country).lower()
    variants = [country_lower]
    alias_map = {
        "iran (islamic republic of)": ["iran"],
        "russian federation": ["russia"],
        "united states of america": ["united states", "usa", "u.s."],
        "bolivia (plurinational state of)": ["bolivia"],
        "venezuela (bolivarian republic of)": ["venezuela"],
        "viet nam": ["vietnam"],
        "lao people's democratic republic": ["laos"],
        "syrian arab republic": ["syria"],
        "democratic republic of the congo": ["dr congo", "drc", "congo"],
        "united republic of tanzania": ["tanzania"],
    }
    variants.extend(alias_map.get(country_lower, []))
    if "(" in country_lower:
        variants.append(country_lower.split("(")[0].strip())
    if " " in country_lower:
        variants.append(country_lower.split()[-1].strip("()"))
    return [v for v in dict.fromkeys(variants) if v]


def _entity_aliases(term):
    if _is_blank_context_value(term):
        return []

    cleaned = re.sub(r"\s+", " ", str(term)).strip()
    aliases = [cleaned]
    lower = cleaned.lower()

    hazard_prefixes = [
        "cyclone", "tropical cyclone", "storm", "tropical storm",
        "hurricane", "typhoon", "earthquake", "flood", "floods",
        "volcano", "eruption",
    ]

    for prefix in hazard_prefixes:
        prefix_with_space = f"{prefix} "
        if lower.startswith(prefix_with_space):
            aliases.append(cleaned[len(prefix_with_space):].strip())
        elif len(cleaned) >= 4:
            aliases.append(f"{prefix.title()} {cleaned}")

    # Hyphenated article titles often join multiple cyclone names.
    for piece in re.split(r"[-\u2013\u2014]", cleaned):
        piece = piece.strip()
        if len(piece) >= 3:
            aliases.append(piece)

    return _compact_terms(aliases, limit=12)


def _event_terms(location_context, include_aliases=False):
    if not location_context:
        return []
    event_name = location_context.get("event_name")
    if _is_blank_context_value(event_name):
        return []
    terms = []
    for part in re.split(r"[,;/|]", str(event_name)):
        cleaned = part.strip()
        if len(cleaned) >= 3 and cleaned.lower() not in {"nan", "none", "null"}:
            terms.append(cleaned)
            if include_aliases:
                terms.extend(_entity_aliases(cleaned))
    return terms


def _is_blank_context_value(value):
    if value is None:
        return True
    try:
        if value != value:
            return True
    except Exception:
        pass
    text = str(value).strip().lower()
    return text in {"", "nan", "none", "null", "nat"}


def _normalize_entity(text):
    text = str(text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    stopwords = {
        "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
        "state", "province", "district", "region", "city", "area", "areas",
        "department", "county", "municipality", "island", "islands",
    }
    tokens = [
        token for token in re.sub(r"\s+", " ", text).strip().split()
        if token and token not in stopwords
    ]
    return " ".join(tokens)


def _token_set_similarity(left, right):
    left_norm = _normalize_entity(left)
    right_norm = _normalize_entity(right)
    if not left_norm or not right_norm:
        return 0.0

    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    overlap = len(left_tokens & right_tokens)
    if overlap:
        token_score = (2 * overlap) / (len(left_tokens) + len(right_tokens))
    else:
        token_score = 0.0

    sequence_score = SequenceMatcher(None, left_norm, right_norm).ratio()
    return max(token_score, sequence_score)


def _contains_fuzzy(text, values, threshold=0.86):
    text_norm = _normalize_entity(text)
    if not text_norm:
        return False

    for value in values:
        if _is_blank_context_value(value):
            continue
        value_norm = _normalize_entity(value)
        if not value_norm:
            continue
        if value_norm in text_norm:
            return True

        value_len = len(value_norm.split())
        text_tokens = text_norm.split()
        min_window = max(1, value_len - 1)
        max_window = min(len(text_tokens), value_len + 3)
        for size in range(min_window, max_window + 1):
            for start in range(0, len(text_tokens) - size + 1):
                window = " ".join(text_tokens[start:start + size])
                if _token_set_similarity(value_norm, window) >= threshold:
                    return True
    return False


def _contains_any(text, values):
    return _contains_fuzzy(text, values)


def score_relevance(title, text, country, start_date, disaster_type,
                    pub_date_str=None, window_days=21,
                    require_country_in_title=False,
                    location_context=None):
    """
    Score article relevance without hard-requiring year, country, or event name.

    Hard filters are reserved for clear false positives such as metaphorical
    usage. Everything else contributes positive or negative evidence.
    """
    title = str(title or "")
    text = str(text or "")
    combined = f"{title} {text}"
    title_lower = title.lower()
    combined_lower = combined.lower()
    year, _, month_name, _ = parse_date_parts(start_date)

    score = 0
    reasons = []
    penalties = []

    for pat in METAPHOR_RE:
        if pat.search(combined_lower):
            return {
                "score": -99,
                "threshold": 99,
                "reasons": [],
                "penalties": ["metaphorical_or_unrelated_usage"],
                "is_relevant": False,
            }

    country_variants = _country_variants(country)
    if _contains_any(title, country_variants):
        score += 2
        reasons.append("country_in_title")
    elif _contains_any(combined, country_variants):
        score += 1
        reasons.append("country_in_text")

    if require_country_in_title and _contains_any(title, country_variants):
        score += 1
        reasons.append("required_country_title_bonus")

    synonyms = get_synonyms(disaster_type)
    if _contains_any(title, synonyms):
        score += 3
        reasons.append("hazard_in_title")
    elif _contains_any(combined, synonyms):
        score += 2
        reasons.append("hazard_in_text")
    else:
        score -= 3
        penalties.append("missing_hazard_term")

    location_terms = _location_terms(location_context)
    matched_location_or_event = False
    if location_terms:
        if _contains_any(title, location_terms):
            score += 3
            reasons.append("local_place_in_title")
            matched_location_or_event = True
        elif _contains_any(combined, location_terms):
            score += 2
            reasons.append("local_place_in_text")
            matched_location_or_event = True

    event_terms = _event_terms(location_context, include_aliases=True)
    if event_terms:
        if _contains_any(title, event_terms):
            score += 4
            reasons.append("event_name_in_title")
            matched_location_or_event = True
        elif _contains_any(combined, event_terms):
            score += 3
            reasons.append("event_name_in_text")
            matched_location_or_event = True

    if (location_terms or event_terms) and not matched_location_or_event:
        score -= 1
        penalties.append("missing_local_or_event_context")

    if year and year in combined_lower:
        score += 2
        reasons.append("event_year_present")
    else:
        title_years = set(re.findall(r"\b(19\d{2}|20\d{2})\b", title_lower))
        if title_years and year not in title_years:
            score -= 4
            penalties.append("different_year_in_title")

    if month_name and month_name.lower() in combined_lower:
        score += 1
        reasons.append("event_month_present")

    if pub_date_str:
        delta = days_from_event(pub_date_str, start_date)
        if delta is not None:
            if delta <= window_days:
                score += 2
                reasons.append("publication_date_near_event")
            elif delta <= 365:
                score += 1
                reasons.append("publication_date_same_year")
            else:
                score -= 2
                penalties.append("publication_date_far_from_event")

    for pat in GENERIC_ARTICLE_RE:
        if pat.search(title_lower):
            event_or_place_in_title = (
                _contains_any(title, _event_terms(location_context))
                or _contains_any(title, _location_terms(location_context))
            )
            penalty = 2 if event_or_place_in_title else 8
            score -= penalty
            penalties.append("generic_title")
            break

    for m in re.finditer(r"\b(\d{4})\s*[-\u2013]\s*(\d{4})\b", combined_lower):
        y1, y2 = m.group(1), m.group(2)
        if year and y1 != year and y2 != year:
            score -= 3
            penalties.append("unrelated_multi_year_range")
            break

    threshold = 5 if event_terms or location_terms else 4
    return {
        "score": score,
        "threshold": threshold,
        "reasons": reasons,
        "penalties": penalties,
        "is_relevant": score >= threshold,
    }


def is_relevant(title, text, country, start_date, disaster_type,
                pub_date_str=None, window_days=21,
                require_country_in_title=False,
                location_context=None,
                minimum_score=4):
    scoring = score_relevance(
        title=title,
        text=text,
        country=country,
        start_date=start_date,
        disaster_type=disaster_type,
        pub_date_str=pub_date_str,
        window_days=window_days,
        require_country_in_title=require_country_in_title,
        location_context=location_context,
    )
    return scoring["score"] >= minimum_score


def query_wikipedia(disaster_type, country, start_date, location_context=None):
    year, _, mname, _ = parse_date_parts(start_date)
    clean_type = disaster_type.split("(")[0].strip()
    terms = get_search_terms(disaster_type)
    event_terms = _event_terms(location_context)
    country_for_query = _country_variants(country)[0]

    queries = []
    for event in event_terms:
        queries.extend([
            f"{event} {country_for_query} {year}",
            f"{event} {clean_type} {year}",
            f"{event} cyclone storm {year}",
        ])
    queries.extend(build_event_search_queries(
        disaster_type,
        country,
        start_date,
        location_context=location_context,
        max_queries=8,
    ))

    seen = set()
    results = []

    for q in queries[:6]:
        r = safe_get("https://en.wikipedia.org/w/api.php", params={
            "action": "query",
            "list": "search",
            "srsearch": q,
            "format": "json",
            "srlimit": 5,
            "srprop": "snippet|titlesnippet",
        }, headers=HEADERS_JSON, retries=0)
        if r is None:
            continue
        try:
            search_items = r.json().get("query", {}).get("search", [])
        except Exception:
            search_items = []

        for item in search_items:
            title = item.get("title", "")
            if title in seen:
                continue
            seen.add(title)
            snippet = clean_html(item.get("snippet", ""))
            extract = _wiki_extract(title) or snippet
            full = f"{snippet} {extract}"
            if not is_relevant(title, full, country, start_date, disaster_type,
                               window_days=3650, location_context=location_context):
                continue
            wiki_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
            results.append(make_article("Wikipedia", title, wiki_url, extract))
            if len(results) >= 3:
                log_source("Wikipedia", len(results))
                return results

    log_source("Wikipedia", len(results))
    return results
