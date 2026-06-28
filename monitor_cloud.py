#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de notícias — versão cloud (GitHub Actions).

Roda UM ciclo e sai. O GitHub Actions chama este script a cada 15 minutos via
cron trigger. O arquivo já_visto.json fica versionado no repositório e é
atualizado via git commit no final de cada execução pelo workflow.

Configuração: defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID como GitHub Secrets
(Settings → Secrets and variables → Actions → New repository secret).
"""

import os
import sys
import json
import time
import html
import logging
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote_plus

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

try:
    import feedparser
except ImportError:
    sys.exit("feedparser não instalado. Rode: pip install feedparser requests")
try:
    import requests
except ImportError:
    sys.exit("requests não instalado. Rode: pip install feedparser requests")

# ============================================================
#   CONFIG — lê das variáveis de ambiente (GitHub Secrets)
# ============================================================
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    sys.exit("TELEGRAM_TOKEN e TELEGRAM_CHAT_ID precisam estar definidos como GitHub Secrets.")

# ============================================================
#   TIMES MONITORADOS (idêntico ao monitor.py local)
# ============================================================
TIMES = [
    {
        "nome": "AVAÍ FC",
        "emoji": "⚽",
        "buscas": [
            ("Avaí Futebol Clube", "pt"),
            ("Avaí FC", "en"),
        ],
    },
    {
        "nome": "CHELSEA FC",
        "emoji": "🔵",
        "buscas": [
            ("Chelsea FC", "pt"),
            ("Chelsea FC", "en"),
        ],
    },
    {
        "nome": "PITTSBURGH STEELERS",
        "emoji": "🏈",
        "buscas": [
            ("Pittsburgh Steelers", "pt"),
            ("Pittsburgh Steelers", "en"),
        ],
    },
    {
        "nome": "LEGACY (CS2)",
        "emoji": "🎮",
        "buscas": [
            ("Legacy CS2 Counter-Strike", "pt"),
            ('"Legacy" CS2', "en"),
        ],
        "exige_no_titulo": ["Legacy"],
        "exclui_no_titulo": ["Hogwarts", "Tomb Raider", "Plague Tale", "Starfall"],
    },
]

JANELA_NOTICIAS        = "2d"
MAX_IDADE_HORAS        = 24
MAX_NOTICIAS_POR_CICLO = 6
MAX_HISTORICO_POR_TIME = 400
TIMEOUT                = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

BASE_DIR        = Path(__file__).resolve().parent
ARQUIVO_MEMORIA = BASE_DIR / "já_visto.json"

# ============================================================
#   LOG — só console (GitHub Actions captura o stdout)
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("monitor_cloud")

# ============================================================
#   MEMÓRIA
# ============================================================
def carregar_memoria():
    if ARQUIVO_MEMORIA.exists():
        try:
            with open(ARQUIVO_MEMORIA, "r", encoding="utf-8") as f:
                dados = json.load(f)
            return {nome: list(ids) for nome, ids in dados.items()}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Não consegui ler %s (%s). Começando memória vazia.", ARQUIVO_MEMORIA.name, e)
    return {}


def salvar_memoria(memoria):
    try:
        dados = {nome: ids[-MAX_HISTORICO_POR_TIME:] for nome, ids in memoria.items()}
        temporario = ARQUIVO_MEMORIA.with_name(ARQUIVO_MEMORIA.name + ".tmp")
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        temporario.replace(ARQUIVO_MEMORIA)
    except OSError as e:
        log.error("Falha ao salvar a memória: %s", e)

# ============================================================
#   BUSCA DE NOTÍCIAS
# ============================================================
def google_news_url(query, lang):
    if JANELA_NOTICIAS:
        query = f"{query} when:{JANELA_NOTICIAS}"
    q = quote_plus(query)
    if lang == "en":
        return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"


def buscar_noticias(query, lang):
    url = google_news_url(query, lang)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    noticias = []
    for entry in feed.entries:
        link  = entry.get("link", "")
        ident = entry.get("id") or link
        if not ident:
            continue
        noticias.append({
            "id":    ident,
            "titulo": entry.get("title", "(sem título)"),
            "link":   link,
            "data":   entry.get("published_parsed"),
        })
    return noticias

# ============================================================
#   MENSAGEM DO TELEGRAM
# ============================================================
def tempo_relativo(published_parsed):
    if not published_parsed:
        return "recentemente"
    try:
        publicado = datetime(*published_parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "recentemente"
    segundos = int((datetime.now(timezone.utc) - publicado).total_seconds())
    if segundos < 60:
        return "agora mesmo"
    minutos = segundos // 60
    if minutos < 60:
        return f"há {minutos} minuto" + ("s" if minutos != 1 else "")
    horas = minutos // 60
    if horas < 24:
        return f"há {horas} hora" + ("s" if horas != 1 else "")
    dias = horas // 24
    return f"há {dias} dia" + ("s" if dias != 1 else "")


def montar_mensagem(time_info, noticia):
    return (
        f"{time_info['emoji']} <b>{html.escape(time_info['nome'])}</b>\n"
        f"📰 {html.escape(noticia['titulo'])}\n"
        f"🔗 {html.escape(noticia['link'])}\n"
        f"⏰ Publicado {tempo_relativo(noticia['data'])}"
    )


def enviar_telegram(texto, disable_preview=True):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text":    texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    resp  = requests.post(url, data=payload, timeout=TIMEOUT)
    dados = resp.json()
    if not dados.get("ok"):
        raise RuntimeError(f"Telegram recusou: {dados.get('description')}")
    return True

# ============================================================
#   CICLO
# ============================================================
def passa_no_filtro(noticia, time_info):
    titulo = noticia["titulo"].lower()
    exige  = time_info.get("exige_no_titulo")
    if exige and not any(t.lower() in titulo for t in exige):
        return False
    exclui = time_info.get("exclui_no_titulo")
    if exclui and any(t.lower() in titulo for t in exclui):
        return False
    return True


def verificar_time(time_info, memoria):
    nome = time_info["nome"]
    primeira_vez_do_time = nome not in memoria
    vistos     = memoria.setdefault(nome, [])
    vistos_set = set(vistos)

    encontradas = {}
    for query, lang in time_info["buscas"]:
        try:
            for n in buscar_noticias(query, lang):
                encontradas.setdefault(n["id"], n)
        except requests.RequestException as e:
            log.warning("    busca '%s' (%s) falhou: %s", query, lang, e)
        except Exception as e:
            log.warning("    erro inesperado na busca '%s' (%s): %s", query, lang, e)

    agora = datetime.now(timezone.utc)
    def recente(n):
        if not n["data"]:
            return True
        try:
            pub = datetime(*n["data"][:6], tzinfo=timezone.utc)
            return (agora - pub).total_seconds() <= MAX_IDADE_HORAS * 3600
        except (TypeError, ValueError):
            return True

    novas = [n for nid, n in encontradas.items()
             if nid not in vistos_set and passa_no_filtro(n, time_info) and recente(n)]
    novas.sort(key=lambda n: n["data"] or time.gmtime(0))

    if primeira_vez_do_time:
        for n in novas:
            vistos.append(n["id"])
        log.info("    %s %s: %d notícia(s) registradas (linha de base, sem enviar)",
                 time_info["emoji"], nome, len(novas))
        return 0

    enviadas = 0
    para_proximo = 0
    for n in novas:
        if enviadas >= MAX_NOTICIAS_POR_CICLO:
            para_proximo += 1
            continue
        try:
            enviar_telegram(montar_mensagem(time_info, n))
            vistos.append(n["id"])
            enviadas += 1
            time.sleep(1)
        except Exception as e:
            log.error("    não consegui enviar '%s...': %s", n["titulo"][:50], e)

    resumo = f"    {time_info['emoji']} {nome}: {len(novas)} nova(s)"
    if enviadas:
        resumo += f", {enviadas} enviada(s)"
    if para_proximo:
        resumo += f", {para_proximo} para o próximo ciclo"
    if not novas:
        resumo += " — nada novo"
    log.info(resumo)
    return enviadas


def rodar_ciclo(memoria):
    total = 0
    for time_info in TIMES:
        log.info("  Verificando %s %s ...", time_info["emoji"], time_info["nome"])
        try:
            total += verificar_time(time_info, memoria)
        except Exception as e:
            log.exception("  Erro ao verificar %s: %s", time_info["nome"], e)
    salvar_memoria(memoria)
    return total

# ============================================================
#   PONTO DE ENTRADA — um ciclo e sai
# ============================================================
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  MONITOR — ciclo único (GitHub Actions)")
    log.info("  %s", datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC"))
    log.info("=" * 60)

    memoria = carregar_memoria()
    primeira_execucao = not memoria

    if primeira_execucao:
        log.info("Primeira execução: registrando notícias atuais como já vistas.")
        log.info("A partir do próximo ciclo, só novidades serão enviadas.")

    total = rodar_ciclo(memoria)
    log.info("Ciclo concluído — %d notícia(s) enviada(s).", total)
