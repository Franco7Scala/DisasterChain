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
from datetime import datetime, timedelta
from support.constants import *
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
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
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
            "url": url, "raw_text": str(raw_text).strip()[:1000]}

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
# Used by query_google_news() e query_floodlist()
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
        results.append(make_article(label, title, link,
                                    f"{title} [{source}, {pub}]"))
        if len(results) >= max_results:
            break

    return results

# ============================================================
# SOURCE 1 - RELIEFWEB (reports + disasters + updates)
# ============================================================

def query_reliefweb(disaster_type, country, start_date,
                    appname="Unical-EnvironmentalCausalDataset-432353"):
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

def query_google_news(disaster_type, country, start_date):
    year, _, mname, _ = parse_date_parts(start_date)
    primary = get_search_terms(disaster_type)[0]

    try:
        event_dt  = datetime.strptime(start_date, "%Y-%m-%d")
        after_dt  = (event_dt - timedelta(days=21)).strftime("%Y-%m-%d")
        before_dt = (event_dt + timedelta(days=21)).strftime("%Y-%m-%d")
        date_filter = f"after:{after_dt} before:{before_dt}"
    except Exception:
        date_filter = year

    queries = [
        f'"{country}" "{primary}" {mname} {year} {date_filter}',
        f'"{country}" {primary} {year} {date_filter}',
        f'{country} {primary} {year} {date_filter}',
    ]

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

def query_duckduckgo(disaster_type, country, start_date):
    year, _, mname, _ = parse_date_parts(start_date)
    primary = get_search_terms(disaster_type)[0]
    results = []

    queries = [
        f'site:reliefweb.int OR site:floodlist.com OR site:gdacs.org '
        f'"{country}" {primary} {mname} {year}',
        f'"{country}" "{primary}" {mname} {year} disaster',
        f'{country} {primary} {year}',
    ]

    for q in queries:
        r = safe_get("https://html.duckduckgo.com/html/",
                     params={"q":q}, headers=HEADERS)
        if r is None:
            continue
        snippets, urls = [], []
        for pat in [r'<a class="result__snippet"[^>]*>(.*?)</a>',
                    r'<div class="result__snippet"[^>]*>(.*?)</div>']:
            snippets = re.findall(pat, r.text, re.DOTALL)
            if snippets:
                break
        for pat in [r'<a class="result__url"[^>]*href="([^"]+)"',
                    r'<a class="result__a"[^>]*href="([^"]+)"']:
            urls = re.findall(pat, r.text)
            if urls:
                break
        for snippet_raw, link in zip(snippets, urls):
            snippet = clean_html(snippet_raw).strip()
            if not snippet:
                continue
            if "uddg=" in link:
                try:
                    link = urllib.parse.unquote(
                        urllib.parse.parse_qs(
                            urllib.parse.urlparse(link).query
                        ).get("uddg",[link])[0])
                except Exception:
                    pass
            if is_relevant(snippet, snippet, country, start_date, disaster_type,
                           window_days=30):
                results.append(make_article("DuckDuckGo",
                    f"{country} {primary} {year}",
                    link if link.startswith("http") else f"https:{link}",
                    snippet))
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

# ============================================================
# ORCHESTRATOR - all listed sources
# ============================================================

def get_all_news_sources(disaster_type, country, start_date, region, lat, lon,
                         reliefweb_appname="Unical-EnvironmentalCausalDataset-432353",
                         location_context=None):
    """
    Query all required sources for the specific event.

    DuckDuckGo is used only as a last resort, when all
    global and regional sources returned zero valid articles.
    """
    articles = []
    source_results = {}
    source_errors = {}
    location_context = location_context or {}

    global_sources = [
        ("ReliefWeb", lambda: query_reliefweb(
            disaster_type, country, start_date, reliefweb_appname)),
        ("GDACS", lambda: query_gdacs(disaster_type, country, start_date)),
        ("NASA Earth Observatory/EONET", lambda: query_nasa_eonet(
            disaster_type, country, start_date)),
        ("Wikipedia", lambda: query_wikipedia(
            disaster_type, country, start_date, location_context=location_context)),
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
            found = deduplicate(query_fn() or [])
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

    articles = deduplicate(articles)
    duckduckgo_used = False

    if len(articles) == 0:
        duckduckgo_used = True
        try:
            ddg_articles = deduplicate(query_duckduckgo(
                disaster_type, country, start_date) or [])
        except Exception as exc:
            ddg_articles = []
            source_errors["DuckDuckGo"] = f"{type(exc).__name__}: {exc}"
            print(f"    [WARN] DuckDuckGo failed: {source_errors['DuckDuckGo']}")

        source_results["DuckDuckGo"] = {
            "queried": True,
            "articles_retrieved": len(ddg_articles),
            "articles": ddg_articles,
        }
        articles = deduplicate(ddg_articles)
    else:
        source_results["DuckDuckGo"] = {
            "queried": False,
            "articles_retrieved": 0,
            "articles": [],
        }

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
                    appname="Unical-EnvironmentalCausalDataset-432353"):
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

    queries = [
        f"{country} {primary} {mname} {year}",
        f"{country} {primary} {year}",
        f"{country} {clean_type} {year}",
    ]

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
                    body = clean_html(fields.get("description", "") or title)
                    url = fields.get("url", "https://reliefweb.int/disasters")
                    pub = (fields.get("date") or {}).get("event", "")
                    source = "ReliefWeb Disasters"

                key = (source, title.lower(), url.rstrip("/"))
                if key in seen:
                    continue
                if pub and not (start_w <= pub[:10] <= end_w):
                    continue
                if is_relevant(title, body, country, start_date, disaster_type,
                               pub_date_str=pub, window_days=60):
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
    search_phrases = []

    for place in places[:4]:
        search_phrases.extend([
            f"{place} {country} {primary} {year}",
            f"{place} {primary} {mname} {year}",
            f"{place} {primary} {year}",
        ])
    search_phrases.extend([
        f"{country} {primary} {mname} {year}",
        f"{country} {primary} {year}",
    ])

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


def _event_terms(location_context):
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


def _has_event_term(text, location_context):
    terms = _event_terms(location_context)
    if not terms:
        return True
    text_lower = str(text).lower()
    return any(term.lower() in text_lower for term in terms)


def is_relevant(title, text, country, start_date, disaster_type,
                pub_date_str=None, window_days=21,
                require_country_in_title=False,
                location_context=None):
    combined = (str(title) + " " + str(text)).lower()
    title_lower = str(title).lower()
    year, _, _, _ = parse_date_parts(start_date)

    if pub_date_str:
        delta = days_from_event(pub_date_str, start_date)
        if delta is not None and delta > window_days:
            return False

    for pat in METAPHOR_RE:
        if pat.search(combined):
            return False

    if year not in combined:
        return False

    country_variants = _country_variants(country)
    if not any(v in combined for v in country_variants):
        return False

    if require_country_in_title and not any(v in title_lower for v in country_variants):
        return False

    if not any(s in combined for s in get_synonyms(disaster_type)):
        return False

    if location_context and _event_terms(location_context):
        if not _has_event_term(combined, location_context):
            return False

    for m in re.finditer(r"\b(\d{4})\s*[-\u2013]\s*(\d{4})\b", combined):
        y1, y2 = m.group(1), m.group(2)
        if y1 != year and y2 != year:
            return False

    return True


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
    queries.extend([
        f"{year} {country_for_query} {terms[0]}",
        f"{mname} {year} {country_for_query} {terms[0]}",
        f"{country_for_query} {terms[0]} {year}",
    ])

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
            if event_terms and not _has_event_term(title, location_context):
                continue
            wiki_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
            results.append(make_article("Wikipedia", title, wiki_url, extract))
            if len(results) >= 3:
                log_source("Wikipedia", len(results))
                return results

    log_source("Wikipedia", len(results))
    return results