import re
from web_scrubber.fetch.browser import BrowserFetcher

URL = ("https://www.gov.br/mj/pt-br/assuntos/sua-seguranca/seguranca-publica/"
       "operacoes-integradas/projeto-captura/lista-de-procurados")

with BrowserFetcher(headless=True, force_refresh=True, timeout_ms=45000) as f:
    html = f.fetch(URL, wait_for_state="networkidle")

print("bytes", len(html))
for kw in ["procurado", "foragido", "card", "<img", "iframe", "sinesp", "accordion", "data-elemento"]:
    print(" count", kw, len(re.findall(kw, html, re.I)))

for m in re.findall(r'<iframe[^>]+src="([^"]+)"', html, re.I)[:5]:
    print(" iframe:", m[:160])

imgs = [i for i in re.findall(r'<img[^>]+src="([^"]+)"', html, re.I) if "static" not in i.lower()]
print(" content imgs (first 8):")
for i in imgs[:8]:
    print("   ", i[:120])

from bs4 import BeautifulSoup as _BS
_s = _BS(html, "lxml")
print(" galeria sections:", len(_s.select("div.galeria-imagens-institucional")))
print(" div.imagem total:", len(_s.select("div.imagem")))
print(" div.galeria... div.imagem:", len(_s.select("div.galeria-imagens-institucional div.imagem")))
# pagination hints
for sel in ["a[href*='b_start']", ".paginacao", ".listingBar", "a[rel='next']", "nav.pagination"]:
    print(f"  pagination[{sel}]:", len(_s.select(sel)))
for a in _s.select("a[href*='b_start']")[:5]:
    print("   b_start link:", a.get("href")[:120])
import re as _re
print(" 'b_start' in html:", len(_re.findall(r'b_start', html)))
print(" 'data-bs-target'/accordion tabs:", len(_re.findall(r'data-bs-target|tab-pane|carousel', html)))

# Dump the markup around the FIRST content image (a fugitive photo) to see the card DOM
imgs2 = [(mm.start(), mm.group(1)) for mm in re.finditer(r'<img[^>]+src="([^"]+)"', html, re.I)
         if "static" not in mm.group(1).lower() and "logo" not in mm.group(1).lower()]
if imgs2:
    pos = imgs2[0][0]
    block = re.sub(r"\s+", " ", html[max(0, pos - 1200):pos + 1200])
    print(" --- card DOM around first content image ---")
    print(block[:2400])
