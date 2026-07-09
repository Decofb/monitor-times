// ─────────────────────────────────────────────────────────────
//   Monitor de notícias — Cloudflare Worker (cron a cada 1 min)
//
//   Substitui o monitor_cloud.py + GitHub Actions. Roda direto na
//   Cloudflare, guarda a memória anti-duplicação no KV (atômico) e
//   envia para o Telegram assim que sai notícia nova.
// ─────────────────────────────────────────────────────────────
import { XMLParser } from "fast-xml-parser";
import { parse as parseHTML } from "node-html-parser";

// ── Parâmetros ────────────────────────────────────────────────
const JANELA_HORAS  = 4;    // só envia notícias das últimas N horas
const MAX_POR_CICLO = 6;    // máximo de alertas por time por execução
const MAX_HIST      = 500;  // máximo de IDs guardados por time no KV
const TIMEOUT_MS    = 15000;

const HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
  "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
  Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
};

// ── Fontes por time ───────────────────────────────────────────
//   tipo "rss" | "html";  filtrar: exige palavra-chave no título;
//   lang "en" → traduz o título para PT antes de enviar.
const TIMES = [
  {
    nome: "AVAI FC",
    emoji: "⚽",
    palavras_chave: ["Avaí", "Avai"],
    exclui_palavras: [
      "feminino", "feminina", "sub-", "sub20", "sub17", "sub15", "sub13", "sub11",
      "categoria de base", "infantil", "juvenil",
      "women", "woman", "girl", "girls", "ladies", "WSL", "academy",
      "u21", "u20", "u18", "u17", "u16", "u15", "u14", "U-", "equipe B",
    ],
    exclui_urls: ["/feminino/", "/women/", "/sub-", "/base/", "/academy/"],
    fontes: [
      // ge.globo matou o RSS por time; a página HTML do time é a fonte ge viva.
      { url: "https://ndmais.com.br/tag/avai/feed/", tipo: "rss", filtrar: false },
      { url: "https://avai.com.br/noticias/feed/", tipo: "rss", filtrar: false },
      { url: "https://ge.globo.com/sc/futebol/times/avai/", tipo: "html", filtrar: true },
      { url: "https://avai.com.br/noticias/", tipo: "html", filtrar: true },
      { url: "https://ndmais.com.br/tag/avai/", tipo: "html", filtrar: true },
    ],
  },
  {
    nome: "CHELSEA FC",
    emoji: "🔵",
    palavras_chave: ["Chelsea"],
    exclui_palavras: [
      "feminino", "feminina", "sub-", "sub20", "sub17", "sub15", "sub13", "sub11",
      "categoria de base", "infantil", "juvenil",
      "women", "woman", "girl", "girls", "ladies", "WSL", "academy",
      "u21", "u20", "u18", "u17", "u16", "u15", "u14", "U-", "equipe B",
    ],
    exclui_urls: ["/feminino/", "/women/", "/sub-", "/base/", "/academy/", "women", "feminino"],
    exclui_categorias: ["Chelsea FC Women", "Academia"],
    fontes: [
      { url: "https://www.chelseafcbrasil.com/feed/", tipo: "rss", filtrar: false },
      { url: "https://www.chelseafcbrasil.com", tipo: "html", filtrar: true },
      // ogol tem proteção anti-bot e às vezes bloqueia (403); entra como bônus.
      { url: "https://www.ogol.com.br/equipe/chelsea/noticias", tipo: "html", filtrar: true },
    ],
  },
  {
    nome: "PITTSBURGH STEELERS",
    emoji: "🏈",
    palavras_chave: ["Steelers", "Pittsburgh"],
    fontes: [
      { url: "https://www.steelers.com/rss/news", tipo: "rss", filtrar: false, lang: "en" },
      { url: "https://steelersdepot.com/feed/", tipo: "rss", filtrar: false, lang: "en" },
      { url: "https://steelersnow.com/feed/", tipo: "rss", filtrar: false, lang: "en" },
      { url: "https://www.behindthesteelcurtain.com/rss/index.xml", tipo: "rss", filtrar: false, lang: "en" },
      { url: "https://www.espn.com.br/nfl/time/_/nome/pit/pittsburgh-steelers", tipo: "html", filtrar: true },
      { url: "https://www.nfl.com/teams/pittsburgh-steelers/", tipo: "html", filtrar: true, lang: "en" },
    ],
  },
  {
    nome: "LEGACY (CS2)",
    emoji: "🎮",
    palavras_chave: ["Legacy"],
    exclui_palavras: ["Hogwarts", "Tomb Raider", "Plague Tale", "Starfall", "Harry Potter"],
    fontes: [
      { url: "https://retakecs.com/noticias/feed/", tipo: "rss", filtrar: true },
      { url: "https://www.dust2.com.br/rss", tipo: "rss", filtrar: true },
      { url: "https://retakecs.com/noticias/", tipo: "html", filtrar: true },
      { url: "https://draft5.gg", tipo: "html", filtrar: true },
      { url: "https://www.dust2.com.br", tipo: "html", filtrar: true },
    ],
  },
];

// ── Utilidades de tempo ───────────────────────────────────────
const agora = () => new Date();

function toDate(v) {
  if (!v) return null;
  if (v instanceof Date) return isNaN(v) ? null : v;
  const d = new Date(v);
  return isNaN(d) ? null : d;
}

function dentroDaJanela(data) {
  const d = toDate(data);
  if (!d) return false; // sem data → descarta (evita artigos antigos do scraping)
  return agora() - d <= JANELA_HORAS * 3600 * 1000;
}

function tempoRelativo(data) {
  const d = toDate(data);
  if (!d) return "recentemente";
  const seg = Math.floor((agora() - d) / 1000);
  if (seg < 60) return "agora mesmo";
  const m = Math.floor(seg / 60);
  if (m < 60) return `há ${m} minuto${m !== 1 ? "s" : ""}`;
  const h = Math.floor(m / 60);
  if (h < 24) return `há ${h} hora${h !== 1 ? "s" : ""}`;
  const dias = Math.floor(h / 24);
  return `há ${dias} dia${dias !== 1 ? "s" : ""}`;
}

// ── Fetch com timeout ─────────────────────────────────────────
async function baixar(url) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const resp = await fetch(url, { headers: HEADERS, redirect: "follow", signal: ctrl.signal });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.text();
  } finally {
    clearTimeout(t);
  }
}

// ── RSS ───────────────────────────────────────────────────────
const xml = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: "@_",
  trimValues: true,
});

function texto(v) {
  if (v == null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "object" && "#text" in v) return String(v["#text"]);
  return String(v);
}

function extrairLinkAtom(link) {
  // Atom: <link href="..." rel="alternate"/> (pode ser array)
  if (!link) return "";
  if (typeof link === "string") return link;
  if (Array.isArray(link)) {
    const alt = link.find((l) => (l["@_rel"] || "alternate") === "alternate") || link[0];
    return alt?.["@_href"] || "";
  }
  return link["@_href"] || texto(link);
}

async function buscarRss(url) {
  const body = await baixar(url);
  const obj = xml.parse(body);
  const artigos = [];

  const itens = [].concat(obj?.rss?.channel?.item || obj?.feed?.entry || []);
  for (const e of itens) {
    const isAtom = !e.pubDate && (e.published || e.updated || e.id);
    const link = isAtom ? extrairLinkAtom(e.link) : texto(e.link);
    const guid = texto(e.guid) || texto(e.id) || link;
    if (!guid) continue;

    const cats = [].concat(e.category || []).map((c) =>
      typeof c === "object" ? c["@_term"] || texto(c) : String(c)
    );

    artigos.push({
      id: guid,
      titulo: texto(e.title).trim(),
      link,
      data: e.pubDate || e.published || e.updated || null,
      categorias: cats,
    });
  }
  return artigos;
}

// ── HTML (scraping genérico) ──────────────────────────────────
const SKIP_EXT = [".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf"];
const SKIP_STR = ["javascript:", "mailto:", "tel:", "whatsapp", "facebook.com", "twitter.com",
  "instagram.com", "youtube.com", "t.co", "#"];
const CLASSE_CONTAINER = /(post|news|noticia|item|card|entry|story|feed|manchete)/i;
const CLASSE_DATA = /(date|time|quando|data|ago|update|posted|publish)/i;
const ISO_RE = /(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:?\d{2}|Z)?)/;
const DATE_RE = /(\d{4}-\d{2}-\d{2}|\d{2}\/\d{2}\/\d{4})/;

function parseDataStr(s) {
  if (!s) return null;
  s = String(s).trim();
  // dd/mm/yyyy → yyyy-mm-dd
  const br = s.match(/^(\d{2})\/(\d{2})\/(\d{4})/);
  if (br) return toDate(`${br[3]}-${br[2]}-${br[1]}`);
  return toDate(s);
}

function parseDataRelativa(txt) {
  const t = (txt || "").toLowerCase().trim();
  const now = agora();
  let m;
  if ((m = t.match(/(\d+)\s*dia/))) return new Date(now - m[1] * 86400000);
  if ((m = t.match(/(\d+)\s*hora/))) return new Date(now - m[1] * 3600000);
  if ((m = t.match(/(\d+)\s*min/))) return new Date(now - m[1] * 60000);
  if ((m = t.match(/(\d+)\s*semana/))) return new Date(now - m[1] * 7 * 86400000);
  if (/ontem|yesterday/.test(t)) return new Date(now - 86400000);
  if ((m = t.match(/(\d+)\s*days?\s*ago/))) return new Date(now - m[1] * 86400000);
  if ((m = t.match(/(\d+)\s*hours?\s*ago/))) return new Date(now - m[1] * 3600000);
  if (/ago|atrás|atras/.test(t) && (m = t.match(/(\d+)/))) return new Date(now - m[1] * 86400000);
  return null;
}

function dataDoElemento(el) {
  for (const attr of ["datetime", "data-date", "data-time", "data-published", "content", "data-created"]) {
    const v = el.getAttribute(attr);
    if (v) {
      const d = parseDataStr(v);
      if (d) return d;
    }
  }
  const txt = el.text || "";
  for (const pat of [ISO_RE, DATE_RE]) {
    const m = txt.match(pat);
    if (m) {
      const d = parseDataStr(m[1]);
      if (d) return d;
    }
  }
  return parseDataRelativa(txt);
}

function normalizarUrl(href, baseUrl, dominio) {
  if (!href) return null;
  if (SKIP_STR.some((s) => href.includes(s))) return null;
  if (href.startsWith("//")) href = "https:" + href;
  else if (href.startsWith("/")) href = dominio + href;
  else if (!href.startsWith("http")) {
    try { href = new URL(href, baseUrl).href; } catch { return null; }
  }
  let path;
  try { path = new URL(href).pathname; } catch { return null; }
  if (SKIP_EXT.some((ext) => path.toLowerCase().endsWith(ext))) return null;
  const baseHost = new URL(dominio).hostname.replace(/^www\./, "");
  let linkHost;
  try { linkHost = new URL(href).hostname.replace(/^www\./, ""); } catch { return null; }
  if (linkHost && !linkHost.endsWith(baseHost) && !baseHost.endsWith(linkHost)) return null;
  return href;
}

async function buscarHtml(url) {
  const body = await baixar(url);
  const root = parseHTML(body);
  const u = new URL(url);
  const dominio = `${u.protocol}//${u.host}`;

  let containers = root.querySelectorAll("article");
  if (!containers.length) {
    containers = root.querySelectorAll("[class]").filter((el) =>
      CLASSE_CONTAINER.test(el.getAttribute("class") || "")
    );
  }
  if (!containers.length) containers = [root.querySelector("body") || root];

  const artigos = [];
  const vistos = new Set();

  for (const c of containers) {
    const aTag = c.tagName === "A" && c.getAttribute("href") ? c : c.querySelector("a[href]");
    if (!aTag) continue;

    const href = normalizarUrl(aTag.getAttribute("href") || "", url, dominio);
    if (!href || vistos.has(href)) continue;
    let path;
    try { path = new URL(href).pathname; } catch { continue; }
    if (path.replace(/^\/|\/$/g, "").length < 5) continue;
    vistos.add(href);

    const heading = c.querySelector("h1, h2, h3, h4");
    const titulo = ((heading || aTag).text || "").replace(/\s+/g, " ").trim();
    if (titulo.length < 8) continue;

    let data = null;
    const timeEl = c.querySelector("time");
    if (timeEl) data = dataDoElemento(timeEl);
    if (!data) {
      for (const el of c.querySelectorAll("[class]")) {
        if (CLASSE_DATA.test(el.getAttribute("class") || "")) {
          data = dataDoElemento(el);
          if (data) break;
        }
      }
    }

    artigos.push({ id: href, titulo, link: href, data, categorias: [] });
  }
  return artigos;
}

// ── Deduplicação por título ───────────────────────────────────
const STOP_WORDS = new Set([
  "o", "a", "de", "do", "da", "em", "no", "na", "e", "é", "os", "as",
  "dos", "das", "nos", "nas", "um", "uma", "por", "para", "com", "se",
  "que", "ao", "à", "ou", "the", "an", "of", "in", "to", "and", "for",
  "on", "at", "is", "are", "was", "were", "be", "been",
]);

function palavras(titulo) {
  const set = new Set();
  for (const w of (titulo || "").toLowerCase().match(/\w+/g) || []) {
    if (!STOP_WORDS.has(w) && w.length > 2) set.add(w);
  }
  return set;
}

function similar(t1, t2, limiar = 0.8) {
  const p1 = palavras(t1), p2 = palavras(t2);
  if (!p1.size || !p2.size) return false;
  let inter = 0;
  for (const w of p1) if (p2.has(w)) inter++;
  return inter / Math.max(p1.size, p2.size) >= limiar;
}

function dedupTitulos(artigos) {
  const out = [];
  for (const art of artigos) {
    if (!out.some((prev) => similar(art.titulo, prev.titulo))) out.push(art);
  }
  return out;
}

// ── Score e filtros ───────────────────────────────────────────
function calcularScore(art, time) {
  const t = (art.titulo || "").toLowerCase();
  return (time.palavras_chave || []).some((p) => t.includes(p.toLowerCase())) ? 2 : 1;
}

function passaFiltros(art, time, aplicarPalavras) {
  const titulo = (art.titulo || "").toLowerCase();
  const link = (art.link || "").toLowerCase();
  const cats = (art.categorias || []).map((c) => c.toLowerCase());

  if (aplicarPalavras) {
    const chaves = time.palavras_chave || [];
    if (chaves.length && !chaves.some((p) => titulo.includes(p.toLowerCase()))) return false;
  }
  for (const exc of time.exclui_palavras || []) if (titulo.includes(exc.toLowerCase())) return false;
  for (const exc of time.exclui_urls || []) if (link.includes(exc.toLowerCase())) return false;
  for (const exc of time.exclui_categorias || [])
    if (cats.some((cat) => cat.includes(exc.toLowerCase()))) return false;
  if (!dentroDaJanela(art.data)) return false;
  return true;
}

// ── Tradução (Google Translate público) ───────────────────────
async function traduzirParaPt(titulo) {
  try {
    const url =
      "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=pt&dt=t&q=" +
      encodeURIComponent(titulo);
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch(url, { signal: ctrl.signal });
    clearTimeout(t);
    if (!r.ok) return titulo;
    const data = await r.json();
    const traduzido = (data[0] || []).map((seg) => seg[0]).join("");
    return traduzido || titulo;
  } catch {
    return titulo;
  }
}

// ── Telegram ──────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

async function montarMensagem(time, art, precisaTraduzir) {
  const original = art.titulo;
  let exibido = original;
  if (precisaTraduzir) exibido = await traduzirParaPt(original);
  const linha =
    exibido.toLowerCase().trim() !== original.toLowerCase().trim()
      ? escapeHtml(exibido)
      : escapeHtml(original);
  return (
    `${time.emoji} <b>${escapeHtml(time.nome)}</b>\n` +
    `📰 ${linha}\n` +
    `⏰ ${tempoRelativo(art.data)}`
  );
}

async function enviarTelegram(env, texto, link) {
  const payload = {
    chat_id: env.TELEGRAM_CHAT_ID,
    text: texto,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  };
  if (link) {
    payload.reply_markup = JSON.stringify({
      inline_keyboard: [[{ text: "📰 Abrir notícia", url: link }]],
    });
  }
  for (let tentativa = 0; tentativa < 2; tentativa++) {
    const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const dados = await r.json();
    if (dados.ok) return;
    if (r.status === 429) {
      const espera = (dados.parameters?.retry_after || 2) + 1;
      await new Promise((res) => setTimeout(res, espera * 1000));
      continue;
    }
    throw new Error(`Telegram rejeitou: ${dados.description}`);
  }
  throw new Error("Telegram rejeitou após retry (429)");
}

// ── Memória (KV) ──────────────────────────────────────────────
async function carregarMemoria(env) {
  const raw = await env.MEMORIA.get("ja_visto");
  if (!raw) return {};
  try {
    const o = JSON.parse(raw);
    const m = {};
    for (const k in o) m[k] = Array.from(o[k]);
    return m;
  } catch {
    return {};
  }
}

async function salvarMemoria(env, mem) {
  const dados = {};
  for (const k in mem) dados[k] = mem[k].slice(-MAX_HIST);
  await env.MEMORIA.put("ja_visto", JSON.stringify(dados));
}

// ── Verificação de um time ────────────────────────────────────
async function verificarTime(time, memoria, env, dryRun) {
  const vistos = memoria[time.nome] || (memoria[time.nome] = []);
  const vistosSet = new Set(vistos);
  const encontrados = new Map();
  let totalBrutos = 0;

  const resultados = await Promise.all(
    time.fontes.map(async (fonte) => {
      try {
        const artigos = fonte.tipo === "rss" ? await buscarRss(fonte.url) : await buscarHtml(fonte.url);
        console.log(`  ✓ ${fonte.url} → ${artigos.length}`);
        return { fonte, artigos };
      } catch (e) {
        console.warn(`  ✗ ${fonte.url} [${e.message}]`);
        return { fonte, artigos: [] };
      }
    })
  );

  for (const { fonte, artigos } of resultados) {
    totalBrutos += artigos.length;
    const aplicarPalavras = fonte.filtrar !== false;
    for (const art of artigos) {
      if (vistosSet.has(art.id) || encontrados.has(art.id)) continue;
      if (!passaFiltros(art, time, aplicarPalavras)) continue;
      art.score = calcularScore(art, time);
      art.lang = fonte.lang || "pt";
      encontrados.set(art.id, art);
    }
  }

  if (totalBrutos === 0) {
    console.warn(`  ⚠ ${time.emoji} ${time.nome}: todas as fontes falharam neste ciclo.`);
    return 0;
  }

  const novas = dedupTitulos(
    [...encontrados.values()].sort((a, b) => {
      const sc = (b.score || 1) - (a.score || 1);
      if (sc) return sc;
      return (toDate(b.data)?.getTime() || 0) - (toDate(a.data)?.getTime() || 0);
    })
  );

  let enviadas = 0;
  for (const art of novas) {
    if (enviadas >= MAX_POR_CICLO) break;
    try {
      const msg = await montarMensagem(time, art, art.lang === "en");
      if (dryRun) {
        console.log(`  [DRY_RUN] enviaria: ${time.nome} — ${art.titulo}`);
      } else {
        await enviarTelegram(env, msg, art.link || "");
      }
      vistos.push(art.id); // só marca como visto após enviar com sucesso
      enviadas++;
    } catch (e) {
      console.error(`  ✗ Telegram: ${e.message}`);
    }
  }

  console.log(`  ${time.emoji} ${time.nome} → ${novas.length} nova(s), ${enviadas} ${dryRun ? "(dry)" : "enviada(s)"}.`);
  return enviadas;
}

// ── Ciclo principal ───────────────────────────────────────────
async function rodarCiclo(env) {
  const dryRun = String(env.DRY_RUN || "").toLowerCase() === "true";
  const memoria = await carregarMemoria(env);
  let total = 0;
  for (const time of TIMES) {
    try {
      total += await verificarTime(time, memoria, env, dryRun);
    } catch (e) {
      console.error(`Erro em ${time.nome}: ${e.message}`);
    }
  }
  await salvarMemoria(env, memoria);
  console.log(`Ciclo concluído — ${total} notícia(s) ${dryRun ? "(dry-run)" : "enviada(s)"}.`);
  return total;
}

// ── Entradas ──────────────────────────────────────────────────
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(rodarCiclo(env));
  },

  // GET para checar status, testar envio (?test=1) ou rodar um ciclo (?run=1).
  async fetch(request, env) {
    const { searchParams } = new URL(request.url);
    if (searchParams.get("test") === "1") {
      await enviarTelegram(
        env,
        "✅ <b>Teste do monitor-news</b>\nSe você recebeu isto, o envio pela Cloudflare está funcionando.",
        ""
      );
      return new Response("mensagem de teste enviada\n", { status: 200 });
    }
    if (searchParams.get("run") === "1") {
      const total = await rodarCiclo(env);
      return new Response(`ok — ${total} enviada(s)\n`, { status: 200 });
    }
    return new Response("monitor-news no ar. Use ?run=1 (ciclo) ou ?test=1 (teste de envio).\n", { status: 200 });
  },
};
