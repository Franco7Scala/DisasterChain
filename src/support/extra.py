import trafilatura

# code to extract text from google news
def _resolve_and_extract_text(url, fallback_text=""):
    try:
        r = requests.get(url, allow_redirects=True, timeout=10, headers=HEADERS)
        final_url = r.url
        html = r.text
        extracted_text = trafilatura.extract(html)
        if extracted_text and len(extracted_text) > 150:
            return final_url, extracted_text

    except Exception:
        pass

    return url, fallback_text
