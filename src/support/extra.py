import re

import requests

try:
    import trafilatura
except ImportError:
    trafilatura = None

from support.constants import HEADERS


def _clean_html(text):
    text = re.sub(r"(?is)<script.*?</script>", " ", str(text or ""))
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li)>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _fallback_extract(html):
    paragraphs = re.findall(r"(?is)<p[^>]*>(.*?)</p>", html or "")
    if paragraphs:
        return _clean_html(" ".join(paragraphs))
    return _clean_html(html)

# code to extract text from google news
def _resolve_and_extract_text(url, fallback_text=""):
    try:
        r = requests.get(url, allow_redirects=True, timeout=10, headers=HEADERS)
        final_url = r.url
        html = r.text
        extracted_text = trafilatura.extract(html) if trafilatura else _fallback_extract(html)
        if extracted_text and len(extracted_text) > 150:
            return final_url, extracted_text

    except Exception:
        pass

    return url, fallback_text
