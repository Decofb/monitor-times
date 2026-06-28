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

# Proxy Cloudflare Worker — contorna bloqueios de IP do GitHub Actions
PROXY_URL    = "https://monitor-proxy.brandes-andre1.workers.dev"
PROXY_SECRET = os.environ.get("PROXY_SECRET", "").strip()

JANELA_HORAS  = 4     # só envia notícias publicadas nas últimas 4 h
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
LOG_F     = BASE_DIR / "log.txt"

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
        "exclui_palavras": [
            "feminino", "feminina", "sub-", "sub20", "sub17", "sub15", "sub13", "sub11",
            "base", "categoria de base", "infantil", "juvenil", "junior", "júnior",
            "women", "woman", "girl", "girls", "ladies", "WSL", "academy",
            "u21", "u20", "u18", "u17", "u16", "u15", "u14", "U-", "equipe B", "reservas",
        ],
        "fontes": [
            # ── RSS (preferencial) ──────────────────────────
            {"url": "https://ge.globo.com/dynamo/globoesporte/futebol/times/rss20/avai.xml",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://ndmais.com.br/tag/avai/feed/",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://avai.com.br/noticias/feed/",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://ge.globo.com/futebol/transferencias/rss20/index.xml",
             "tipo": "rss",  "filtrar": True},
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
        "exclui_palavras": [
            "feminino", "feminina", "sub-", "sub20", "sub17", "sub15", "sub13", "sub11",
            "base", "categoria de base", "infantil", "juvenil", "junior", "júnior",
            "women", "woman", "girl", "girls", "ladies", "WSL", "academy",
            "u21", "u20", "u18", "u17", "u16", "u15", "u14", "U-", "equipe B", "reservas",
        ],
        "fontes": [
            {"url": "https://www.chelseafcbrasil.com/feed/",
             "tipo": "rss",  "filtrar": False},
            {"url": "https://ge.globo.com/futebol/transferencias/rss20/index.xml",
             "tipo": "rss",  "filtrar": True},
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
#   FETCH VIA PROXY (Cloudflare Worker)
# ─────────────────────────────────────────────────────────────
def _fetch(url: str) -> requests.Response:
    """Faz GET via Cloudflare Worker proxy se PROXY_SECRET estiver definido,
    ou diretamente caso contrário (útil para testes locais)."""
    if PROXY_SECRET:
        resp = requests.get(
            PROXY_URL,
            params={"url": url},
            headers={**HEADERS, "X-Proxy-Token": PROXY_SECRET},
            timeout=TIMEOUT,
        )
    else:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


# ─────────────────────────────────────────────────────────────
#   BUSCA RSS
# ─────────────────────────────────────────────────────────────
def buscar_rss(url: str) -> list[dict]:
    resp = _fetch(url)
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


_REL_NUM_RE = re.compile(r'(\d+)')


def _parse_relative_date(txt: str) -> datetime | None:
    """Parse datas relativas PT-BR/EN: '2 dias atrás', '3h ago', 'ontem', etc."""
    t = txt.lower().strip()
    now = agora_utc()
    m = re.search(r'(\d+)\s*dia', t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.search(r'(\d+)\s*hora', t)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    m = re.search(r'(\d+)\s*min', t)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    m = re.search(r'(\d+)\s*semana', t)
    if m:
        return now - timedelta(weeks=int(m.group(1)))
    if any(w in t for w in ('ontem', 'yesterday')):
        return now - timedelta(days=1)
    if any(w in t for w in ('days ago', 'day ago')):
        m = _REL_NUM_RE.search(t)
        if m:
            return now - timedelta(days=int(m.group(1)))
    if any(w in t for w in ('hours ago', 'hour ago')):
        m = _REL_NUM_RE.search(t)
        if m:
            return now - timedelta(hours=int(m.group(1)))
    # Fallback genérico: qualquer "N atrás" ou "N ago" → assume dias
    if 'ago' in t or 'atrás' in t or 'atras' in t:
        m = _REL_NUM_RE.search(t)
        if m:
            return now - timedelta(days=int(m.group(1)))
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
    # Última tentativa: data relativa em texto ("2 dias atrás", "3h ago", etc.)
    return _parse_relative_date(txt)


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
    resp = _fetch(url)

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
#   DEDUPLICAÇÃO POR TÍTULO
# ─────────────────────────────────────────────────────────────
_STOP_WORDS = {
    "o", "a", "de", "do", "da", "em", "no", "na", "e", "é", "os", "as",
    "dos", "das", "nos", "nas", "um", "uma", "por", "para", "com", "se",
    "que", "ao", "à", "ou", "the", "an", "of", "in", "to", "and", "for",
    "on", "at", "is", "are", "was", "were", "be", "been",
}


def _palavras(titulo: str) -> set:
    return {w for w in re.findall(r'\w+', titulo.lower())
            if w not in _STOP_WORDS and len(w) > 2}


def _similar(t1: str, t2: str, limiar: float = 0.8) -> bool:
    p1, p2 = _palavras(t1), _palavras(t2)
    if not p1 or not p2:
        return False
    return len(p1 & p2) / max(len(p1), len(p2)) >= limiar


def dedup_titulos(artigos: list) -> list:
    """Remove artigos cujo título é ≥80% similar a um já aceito no ciclo."""
    resultado = []
    for art in artigos:
        if not any(_similar(art["titulo"], prev["titulo"]) for prev in resultado):
            resultado.append(art)
    return resultado


# ─────────────────────────────────────────────────────────────
#   SCORE DE RELEVÂNCIA
# ─────────────────────────────────────────────────────────────
def calcular_score(artigo: dict, time_info: dict) -> int:
    """2 se o título menciona palavras_chave do time; 1 caso contrário."""
    titulo = artigo.get("titulo", "").lower()
    chaves = time_info.get("palavras_chave", [])
    return 2 if any(p.lower() in titulo for p in chaves) else 1


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
#   TRADUÇÃO AUTOMÁTICA
# ─────────────────────────────────────────────────────────────
def _traduzir_para_pt(titulo: str) -> str:
    """Detecta o idioma do título e traduz para PT se necessário."""
    try:
        from langdetect import detect, LangDetectException
        try:
            lang = detect(titulo)
        except LangDetectException:
            return titulo
        if lang in ("pt",):
            return titulo
        from deep_translator import GoogleTranslator
        traduzido = GoogleTranslator(source="auto", target="pt").translate(titulo)
        return traduzido or titulo
    except Exception as e:
        log.debug("Tradução falhou (%s) — usando original.", e)
        return titulo


# ─────────────────────────────────────────────────────────────
#   TELEGRAM
# ─────────────────────────────────────────────────────────────
def montar_mensagem(time_info: dict, artigo: dict) -> str:
    titulo_original = artigo["titulo"]
    titulo_exibido  = _traduzir_para_pt(titulo_original)
    if titulo_exibido.lower().strip() != titulo_original.lower().strip():
        titulo_linha = html_lib.escape(titulo_exibido)
    else:
        titulo_linha = html_lib.escape(titulo_original)
    return (
        f"{time_info['emoji']} <b>{html_lib.escape(time_info['nome'])}</b>\n"
        f"📰 {titulo_linha}\n"
        f"⏰ {tempo_relativo(artigo['data'])}"
    )


def enviar_telegram(texto: str, link: str = ""):
    teclado = json.dumps({
        "inline_keyboard": [[{"text": "📰 Abrir notícia", "url": link}]]
    }) if link else None

    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     texto,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    if teclado:
        payload["reply_markup"] = teclado

    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=payload,
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
    emoji      = time_info["emoji"]
    vistos     = memoria.setdefault(nome, [])
    vistos_set = set(vistos)
    encontrados: dict[str, dict] = {}
    total_brutos = 0

    for fonte in time_info["fontes"]:
        url              = fonte["url"]
        tipo             = fonte["tipo"]
        aplicar_palavras = fonte.get("filtrar", True)

        try:
            artigos = buscar_rss(url) if tipo == "rss" else buscar_html(url)
            total_brutos += len(artigos)
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
            art["score"] = calcular_score(art, time_info)
            encontrados[aid] = art

    # Alerta se todas as fontes falharam
    if total_brutos == 0:
        log.warning("  ⚠ %s %s: todas as fontes falharam neste ciclo.", emoji, nome)
        try:
            enviar_telegram(f"⚠️ {emoji} <b>{html_lib.escape(nome)}</b>: todas as fontes falharam neste ciclo.")
        except Exception as e:
            log.error("    ✗ Telegram (alerta falha): %s", e)
        return 0

    # Ordena por score desc, depois data desc
    novas = dedup_titulos(sorted(
        encontrados.values(),
        key=lambda a: (
            -(a.get("score", 1)),
            -(to_utc(a["data"]) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        ),
    ))

    enviadas = 0
    for art in novas:
        if enviadas >= MAX_POR_CICLO:
            log.info("    ↩ Limite %d atingido; %d ficam para o próximo ciclo.",
                     MAX_POR_CICLO, len(novas) - enviadas)
            break
        try:
            enviar_telegram(montar_mensagem(time_info, art), link=art.get("link", ""))
            vistos.append(art["id"])
            enviadas += 1
            time.sleep(1)
        except Exception as e:
            log.error("    ✗ Telegram: %s", e)

    log.info("  %s %s → %d nova(s), %d enviada(s).",
             emoji, nome, len(novas), enviadas)
    return enviadas


def rodar_ciclo(memoria: dict) -> dict:
    resultados = {}
    for time_info in TIMES:
        log.info("Verificando %s %s ...", time_info["emoji"], time_info["nome"])
        try:
            resultados[time_info["nome"]] = verificar_time(time_info, memoria)
        except Exception as e:
            log.exception("Erro em %s: %s", time_info["nome"], e)
            resultados[time_info["nome"]] = 0
    salvar_memoria(memoria)
    return resultados


def gravar_log(resultados: dict):
    """Acrescenta uma linha no log.txt com os totais do ciclo."""
    try:
        partes = " | ".join(
            f"{nome.split()[0].capitalize()}: {n}"
            for nome, n in resultados.items()
        )
        linha = f"{agora_utc().strftime('%Y-%m-%d %H:%M')} UTC | {partes}\n"
        with open(LOG_F, "a", encoding="utf-8") as f:
            f.write(linha)
    except Exception as e:
        log.warning("Não consegui gravar log.txt: %s", e)


# ─────────────────────────────────────────────────────────────
#   PONTO DE ENTRADA
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  MONITOR v3 — ciclo único (GitHub Actions)")
    log.info("  %s", agora_utc().strftime("%d/%m/%Y %H:%M:%S UTC"))
    log.info("  Janela: últimas %d horas", JANELA_HORAS)
    log.info("=" * 60)
    memoria    = carregar_memoria()
    resultados = rodar_ciclo(memoria)
    total      = sum(resultados.values())
    gravar_log(resultados)
    log.info("Ciclo concluído — %d notícia(s) enviada(s).", total)
