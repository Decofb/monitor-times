#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de notícias v2 — GitHub Actions.

Mudanças em relação à v1:
• Fontes específicas por time (lista curada) em vez do Google News RSS
• Prioriza RSS dos sites; faz scraping HTML como complemento
• Filtro de 24 h: só notícias publicadas no último dia são enviadas
• Filtro de palavras-chave em portais genéricos
"""

import os, sys, json, time, html as html_lib, logging, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

try:
    import feedparser
except ImportError:
    sys.exit("feedparser não instalado. Rode: pip install -r requirements.txt")
try:
    import requests
except ImportError:
    sys.exit("requests não instalado. Rode: pip install -r requirements.txt")
try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("beautifulsoup4 não instalado. Rode: pip install -r requirements.txt")

# ─────────────────────────────────────────────────────────────
#   CONFIGURAÇÃO
# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    sys.exit("Defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID como GitHub Secrets.")

JANELA_HORAS  = 24    # só envia notícias publicadas nas últimas 24 h
MAX_POR_CICLO = 6     # máximo de alertas por time por execução
MAX_HIST      = 500   # máximo de IDs guardados por time no já_visto.json
TIMEOUT       = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

BASE_DIR  = Path(__file__).resolve().parent
MEMORIA_F = BASE_DIR / "já_visto.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("monitor")

# ─────────────────────────────────────────────────────────────
#   FONTES POR TIME
#
#   tipo    "rss"  → parse com feedparser (mais confiável)
#           "html" → scraping genérico com BeautifulSoup
#   filtrar True   → só inclui artigos cujo título contenha
#                    palavras_chave do time (usado em portais
#                    que cobrem vários assuntos)
# ─────────────────────────────────────────────────────────────
TIMES = [
    {
        "nome": "AVAI FC",
        "emoji": "⚽",
        "palavras_chave": ["Avaí", "Avai"],
        "fontes": [
            # ── RSS (preferencial) ──────────────────────────
            {"url": "https://ge.globo.com/dynamo/globoesporte/futebol/times/rss20/avai.xml",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://ndmais.com.br/tag/avai/feed/",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://avai.com.br/noticias/feed/",
             "tipo": "rss",  "filtrar": False},
            # ── HTML (complemento) ──────────────────────────
            {"url": "https://ge.globo.com/sc/futebol/times/avai/",
             "tipo": "html", "filtrar": True},
            {"url": "https://avai.com.br/noticias/",
             "tipo": "html", "filtrar": True},
            {"url": "https://ndmais.com.br/tag/avai/",
             "tipo": "html", "filtrar": True},
        ],
    },
    {
        "nome": "CHELSEA FC",
        "emoji": "🔵",
        "palavras_chave": ["Chelsea"],
        "fontes": [
            {"url": "https://www.chelseafcbrasil.com/feed/",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://www.chelseafcbrasil.com",
             "tipo": "html", "filtrar": True},
            {"url": "https://www.ogol.com.br/equipe/chelsea/noticias",
             "tipo": "html", "filtrar": True},
        ],
    },
    {
        "nome": "PITTSBURGH STEELERS",
        "emoji": "🏈",
        "palavras_chave": ["Steelers", "Pittsburgh"],
        "fontes": [
            {"url": "https://www.steelers.com/rss/news_feed.rss",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://steelersdepot.com/feed/",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://steelersnow.com/feed/",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://www.behindthesteelcurtain.com/rss/current",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://www.espn.com.br/nfl/time/_/nome/pit/pittsburgh-steelers",
             "tipo": "html", "filtrar": True},
            {"url": "https://www.nfl.com/teams/pittsburgh-steelers/",
             "tipo": "html", "filtrar": True},
        ],
    },
    {
        "nome": "LEGACY (CS2)",
        "emoji": "🎮",
        "palavras_chave": ["Legacy"],
        "exclui_palavras": [
            "Hogwarts", "Tomb Raider", "Plague Tale",
            "Starfall", "Harry Potter",
        ],
        "fontes": [
            {"url": "https://retakecs.com/noticias/feed/",
             "tipo": "rss",  "filtrar": True},
            {"url": "https://www.dust2.com.br/feed/",
             "tipo": "rss",  "filtrar": True},
            {"url": "https://retakecs.com/noticias/",
             "tipo": "html", "filtrar": True},
            {"url": "https://draft5.gg",
             "tipo": "html", "filtrar": True},
            {"url": "https://www.dust2.com.br",
             "tipo": "html", "filtrar": True},
        ],
    },
]

# ─────────────────────────────────────────────────────────────
#   UTILITÁRIOS DE TEMPO
# ─────────────────────────────────────────────────────────────
def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(data) -> datetime | None:
    """Converte time.struct_time (feedparser) ou datetime para datetime UTC."""
    if data is None:
        return None
    if isinstance(data, datetime):
        return data if data.tzinfo else data.replace(tzinfo=timezone.utc)
    try:
        return datetime(*data[:6], tzinfo=timezone.utc)
    except Exception:
        return None


def dentro_da_janela(data) -> bool:
    """True se a data está dentro das últimas JANELA_HORAS horas."""
    dt = to_utc(data)
    if dt is None:
        return True     # sem data → inclui (não descarta por precaução)
    return (agora_utc() - dt) <= timedelta(hours=JANELA_HORAS)


def tempo_relativo(data) -> str:
    dt = to_utc(data)
    if dt is None:
        return "recentemente"
    seg = int((agora_utc() - dt).total_seconds())
    if seg < 60:  return "agora mesmo"
    m = seg // 60
    if m < 60:    return f"há {m} minuto{'s' if m != 1 else ''}"
    h = m // 60
    if h < 24:    return f"há {h} hora{'s' if h != 1 else ''}"
    d = h // 24
    return f"há {d} dia{'s' if d != 1 else ''}"


# ─────────────────────────────────────────────────────────────
#   BUSCA RSS
# ─────────────────────────────────────────────────────────────
def buscar_rss(url: str) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    artigos = []
    for e in feed.entries:
        ident = e.get("id") or e.get("link", "")
        if not ident:
            continue
        artigos.append({
            "id":     ident,
            "titulo": e.get("title", "").strip(),
            "link":   e.get("link", ""),
            "data":   e.get("published_parsed"),
        })
    return artigos


# ─────────────────────────────────────────────────────────────
#   BUSCA HTML — scraping genérico
# ─────────────────────────────────────────────────────────────
_ISO_RE  = re.compile(
    r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:?\d{2}|Z)?)'
)
_DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})')
_SKIP_EXT = {".css", ".js", ".png", ".jpg", ".jpeg", ".gif",
             ".svg", ".ico", ".woff", ".woff2", ".ttf"}
_SKIP_STR = (
    "javascript:", "mailto:", "tel:", "whatsapp",
    "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "t.co", "#",
)


def _parse_date_str(s: str) -> datetime | None:
    s = s.strip().replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z",
        "%Y-%m-%d %H:%M:%S",   "%Y-%m-%d",
        "%d/%m/%Y",
    ):
        try:
            dt = datetime.strptime(s[:25], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _data_do_elemento(el) -> datetime | None:
    for attr in ("datetime", "data-date", "data-time",
                 "data-published", "content", "data-created"):
        v = el.get(attr, "")
        if v:
            dt = _parse_date_str(v)
            if dt:
                return dt
    txt = el.get_text(" ", strip=True)
    for pat in (_ISO_RE, _DATE_RE):
        m = pat.search(txt)
        if m:
            dt = _parse_date_str(m.group(1))
            if dt:
                return dt
    return None


def _normalizar_url(href: str, base_url: str, dominio: str) -> str | None:
    if not href:
        return None
    if any(s in href for s in _SKIP_STR):
        return None
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = dominio + href
    elif not href.startswith("http"):
        href = urljoin(base_url, href)
    path = urlparse(href).path
    if any(path.lower().endswith(ext) for ext in _SKIP_EXT):
        return None
    # Mantém só URLs do mesmo domínio
    base_host = urlparse(dominio).netloc.lstrip("www.")
    link_host = urlparse(href).netloc.lstrip("www.")
    if link_host and not link_host.endswith(base_host) and not base_host.endswith(link_host):
        return None
    return href


def buscar_html(url: str) -> list[dict]:
    """Scraping genérico: extrai artigos (título + link + data opcional)."""
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()

    try:
        soup = BeautifulSoup(resp.content, "lxml")
    except Exception:
        soup = BeautifulSoup(resp.content, "html.parser")

    dominio = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    artigos, vistos = [], set()

    # 1) Containers semânticos
    containers = soup.find_all("article") or soup.find_all(
        attrs={"class": re.compile(
            r"(post|news|noticia|item|card|entry|story|feed|manchete)", re.I
        )}
    )
    # 2) Fallback geral
    if not containers:
        containers = [soup.body or soup]

    for container in containers:
        a_tag = (
            container
            if container.name == "a" and container.get("href")
            else container.find("a", href=True)
        )
        if not a_tag:
            continue

        href = _normalizar_url(a_tag.get("href", ""), url, dominio)
        if not href or href in vistos:
            continue
        if len(urlparse(href).path.strip("/")) < 5:
            continue
        vistos.add(href)

        heading = container.find(["h1", "h2", "h3", "h4"])
        titulo  = (heading or a_tag).get_text(" ", strip=True)
        if len(titulo) < 8:
            continue

        data = None
        time_el = container.find("time")
        if time_el:
            data = _data_do_elemento(time_el)
        if data is None:
            for el in container.find_all(
                ["span", "p", "div", "meta", "time"],
                attrs={"class": re.compile(
                    r"(date|time|quando|data|ago|update|posted|publish)", re.I
                )},
            ):
                data = _data_do_elemento(el)
                if data:
                    break

        artigos.append({"id": href, "titulo": titulo, "link": href, "data": data})

    return artigos


# ─────────────────────────────────────────────────────────────
#   FILTROS
# ─────────────────────────────────────────────────────────────
def passa_filtros(artigo: dict, time_info: dict, aplicar_palavras: bool) -> bool:
    titulo = artigo.get("titulo", "").lower()

    if aplicar_palavras:
        chaves = time_info.get("palavras_chave", [])
        if chaves and not any(p.lower() in titulo for p in chaves):
            return False

    for exc in time_info.get("exclui_palavras", []):
        if exc.lower() in titulo:
            return False

    if not dentro_da_janela(artigo.get("data")):
        return False

    return True


# ─────────────────────────────────────────────────────────────
#   TELEGRAM
# ─────────────────────────────────────────────────────────────
def montar_mensagem(time_info: dict, artigo: dict) -> str:
    return (
        f"{time_info['emoji']} <b>{html_lib.escape(time_info['nome'])}</b>\n"
        f"📰 {html_lib.escape(artigo['titulo'])}\n"
        f"🔗 {html_lib.escape(artigo['link'])}\n"
        f"⏰ {tempo_relativo(artigo['data'])}"
    )


def enviar_telegram(texto: str):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     texto,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )
    dados = r.json()
    if not dados.get("ok"):
        raise RuntimeError(f"Telegram rejeitou: {dados.get('description')}")


# ─────────────────────────────────────────────────────────────
#   MEMÓRIA
# ─────────────────────────────────────────────────────────────
def carregar_memoria() -> dict:
    if MEMORIA_F.exists():
        try:
            with open(MEMORIA_F, encoding="utf-8") as f:
                return {k: list(v) for k, v in json.load(f).items()}
        except Exception as e:
            log.warning("Memória corrompida (%s) — começando do zero.", e)
    return {}


def salvar_memoria(mem: dict):
    dados = {k: v[-MAX_HIST:] for k, v in mem.items()}
    tmp = MEMORIA_F.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    tmp.replace(MEMORIA_F)


# ─────────────────────────────────────────────────────────────
#   CICLO PRINCIPAL
# ─────────────────────────────────────────────────────────────
def verificar_time(time_info: dict, memoria: dict) -> int:
    nome       = time_info["nome"]
    vistos     = memoria.setdefault(nome, [])
    vistos_set = set(vistos)
    encontrados: dict[str, dict] = {}

    for fonte in time_info["fontes"]:
        url              = fonte["url"]
        tipo             = fonte["tipo"]
        aplicar_palavras = fonte.get("filtrar", True)

        try:
            artigos = buscar_rss(url) if tipo == "rss" else buscar_html(url)
            log.info("    %s → %d artigo(s) bruto(s)", url, len(artigos))
        except requests.HTTPError as e:
            code = e.response.status_code if e.response else "?"
            log.warning("    ✗ %s [HTTP %s]", url, code)
            continue
        except requests.RequestException as e:
            log.warning("    ✗ %s [%s]", url, type(e).__name__)
            continue
        except Exception as e:
            log.warning("    ✗ %s [%s: %s]", url, type(e).__name__, e)
            continue

        for art in artigos:
            aid = art["id"]
            if aid in vistos_set or aid in encontrados:
                continue
            if not passa_filtros(art, time_info, aplicar_palavras):
                continue
            encontrados[aid] = art

    # Mais recente primeiro
    novas = sorted(
        encontrados.values(),
        key=lambda a: to_utc(a["data"]) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    enviadas = 0
    for art in novas:
        if enviadas >= MAX_POR_CICLO:
            log.info("    ↩ Limite %d atingido; %d ficam para o próximo ciclo.",
                     MAX_POR_CICLO, len(novas) - enviadas)
            break
        try:
            enviar_telegram(montar_mensagem(time_info, art))
            vistos.append(art["id"])
            enviadas += 1
            time.sleep(1)
        except Exception as e:
            log.error("    ✗ Telegram: %s", e)

    log.info("  %s %s → %d nova(s), %d enviada(s).",
             time_info["emoji"], nome, len(novas), enviadas)
    return enviadas


def rodar_ciclo(memoria: dict) -> int:
    total = 0
    for time_info in TIMES:
        log.info("Verificando %s %s ...", time_info["emoji"], time_info["nome"])
        try:
            total += verificar_time(time_info, memoria)
        except Exception as e:
            log.exception("Erro em %s: %s", time_info["nome"], e)
    salvar_memoria(memoria)
    return total


# ─────────────────────────────────────────────────────────────
#   PONTO DE ENTRADA
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  MONITOR v2 — ciclo único (GitHub Actions)")
    log.info("  %s", agora_utc().strftime("%d/%m/%Y %H:%M:%S UTC"))
    log.info("  Janela: últimas %d horas", JANELA_HORAS)
    log.info("=" * 60)
    memoria = carregar_memoria()
    total   = rodar_ciclo(memoria)
    log.info("Ciclo concluído — %d notícia(s) enviada(s).", total)
