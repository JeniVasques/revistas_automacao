import requests
from bs4 import BeautifulSoup

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"
})

url = "https://ojs.upf.br/index.php/rhdt/announcement"
r = session.get(url, timeout=30)
sopa = BeautifulSoup(r.content, 'html.parser')

texto = sopa.get_text(" ", strip=True)
print(f"Tamanho do texto: {len(texto)}")
print(texto[:500])