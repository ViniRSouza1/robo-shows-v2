"""
PASSO 2 (v2) — Descoberta de shows futuros em São Paulo
========================================================
Estrategia:
  - Top N artistas (os mais ouvidos): GEMINI COM GROUNDING (Google Search).
    O Gemini consulta a busca real do Google e extrai os shows numa unica
    chamada. E o que encontra eventos pequenos que o scraping perde
    (ex.: shows no Sympla). Usa so a chave do Gemini (sem CSE).
  - Demais artistas: ddgs (DuckDuckGo/Bing/Brave) + extracao por IA.

Observacao: o Google descontinuou o Custom Search JSON API para projetos
novos, por isso o grounding do Gemini substitui aquela ideia.

IA (extracao/validacao dos que usam ddgs): Gemini -> fallback Groq.

Blindagens contra timeout:
  - Backoff curto e desistencia por artista (nao trava a execucao).
  - Fallback: se o grounding do Gemini estourar a cota, o artista cai no ddgs.
  - Orcamento GLOBAL de tempo: encerra e salva o que encontrou (garante o envio).
  - Arquivo de saida sempre gravado.

Dependências:
    pip install ddgs requests groq python-dotenv

Como rodar:
    python passo2_buscar_shows.py
"""

import os
import re
import json
import time
import unicodedata
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

# ==================================================================
#   CONFIGURAÇÃO — ajuste aqui
# ==================================================================

MODO  = "top_n"          # "top_n" | "manual" | "todos"
TOP_N = 50

ARTISTAS_MANUAIS = ["Thiago Espírito Santo", "Fresno", "Terno Rei"]

# Os N primeiros artistas (mais ouvidos) usam o Gemini com grounding (Google).
# Os demais usam o ddgs. Assim priorizamos os favoritos e poupamos cota.
NUM_ARTISTAS_GROUNDING = 20

# Resultados de busca por artista no caminho ddgs.
MAX_RESULTADOS_POR_ARTISTA = 20

# Janela máxima de antecedência aceita (evita datas empurradas pro futuro).
DIAS_MAX_FUTURO = 540    # ~18 meses

# Orçamento GLOBAL de tempo (o job do GitHub tem timeout de 60 min).
TEMPO_MAX_SEGUNDOS = 45 * 60

PAUSA_BUSCA      = 3     # segundos entre buscas no ddgs
MIN_INTERVALO_IA = 5     # segundos entre chamadas de IA (respeita free tier)
MAX_RETRIES_IA   = 3     # tentativas por artista antes de desistir (sem travar)

# ==================================================================

GEMINI_API_KEY          = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_GROUNDING  = "gemini-2.5-flash"        # suporta Google Search
GEMINI_MODEL_EXTRACAO   = "gemini-2.5-flash-lite"   # extracao dos resultados ddgs
GROQ_API_KEY            = os.getenv("GROQ_API_KEY")
GROQ_MODEL              = "llama-3.3-70b-versatile"

BANDSINTOWN_APP_ID = os.getenv("BANDSINTOWN_APP_ID")   # opcional

ARTISTAS_FILE = "artistas_favoritos.json"
OUTPUT_FILE   = "shows_encontrados.json"

HOJE      = date.today()
HOJE_STR  = HOJE.strftime("%d/%m/%Y")
ANO_ATUAL = HOJE.year
ANO_PROX  = ANO_ATUAL + 1

FONTES_CONHECIDAS = {
    "bileto.sympla.com.br": "Sympla",
    "sympla.com.br":        "Sympla",
    "eventim.com.br":       "Eventim",
    "ingresse.com":         "Ingresse",
    "ticket360.com.br":     "Ticket360",
    "ticketmaster.com.br":  "Ticketmaster",
    "clubedoingresso.com":  "Clube do Ingresso",
    "uhuu.com":             "Uhuu",
    "bandsintown.com":      "Bandsintown",
    "songkick.com":         "Songkick",
}


# --- Helpers ------------------------------------------------------

def normalizar(texto):
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.lower().strip()


def nome_fonte(url):
    u = (url or "").lower()
    for dominio, nome in FONTES_CONHECIDAS.items():
        if dominio in u:
            return nome
    m = re.search(r"https?://([^/]+)", u)
    if m:
        return m.group(1).replace("www.", "")
    return "Web"


def eh_sao_paulo(texto):
    n = normalizar(texto)
    return "sao paulo" in n or ", sp" in n or n.endswith(" sp")


def _parse_saida_ia(texto):
    """Converte a resposta da IA em lista de shows, tolerando ruido/markdown."""
    texto = (texto or "").strip()
    if texto.startswith("```"):
        texto = "\n".join(texto.split("\n")[1:-1]).strip()
    if not texto or texto == "[]":
        return []
    try:
        shows = json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", texto, re.DOTALL)
        if not m:
            return []
        try:
            shows = json.loads(m.group())
        except Exception:
            return []
    return shows if isinstance(shows, list) else []


def _eh_rate_limit(e):
    if isinstance(e, requests.HTTPError) and e.response is not None:
        if e.response.status_code == 429:
            return True
    msg = str(e).lower()
    return "429" in msg or "rate" in msg or "quota" in msg or "resource_exhausted" in msg


# --- Seleção de artistas ------------------------------------------

def carregar_artistas():
    if MODO == "manual":
        if not ARTISTAS_MANUAIS:
            raise ValueError("MODO='manual' mas ARTISTAS_MANUAIS está vazio.")
        print(f"  Modo: manual ({len(ARTISTAS_MANUAIS)} artistas)")
        return ARTISTAS_MANUAIS

    if not os.path.exists(ARTISTAS_FILE):
        raise FileNotFoundError(
            f"Arquivo '{ARTISTAS_FILE}' não encontrado. Rode o passo1_spotify.py primeiro."
        )

    with open(ARTISTAS_FILE, encoding="utf-8") as f:
        artistas = json.load(f)

    artistas = sorted(artistas, key=lambda x: x.get("score", 0), reverse=True)

    if MODO == "top_n":
        print(f"  Modo: top {TOP_N} artistas mais ouvidos")
        return [a["nome"] for a in artistas[:TOP_N]]

    print(f"  Modo: todos ({len(artistas)} artistas)")
    return [a["nome"] for a in artistas]


# --- FONTE PRINCIPAL: Gemini com grounding (Google Search) --------

def montar_prompt_grounding(nome):
    return f"""Você é um assistente que consulta a busca do Google em tempo real.
Hoje é {HOJE_STR}. Encontre os PRÓXIMOS shows do artista "{nome}" na CIDADE de
São Paulo (capital), com data estritamente posterior a {HOJE_STR}.

Regras:
- Use a busca do Google para obter dados ATUAIS e reais.
- Use SOMENTE o ano que aparece explicitamente nas fontes; NUNCA invente nem
  avance o ano de um show.
- Inclua shows em que "{nome}" realmente toca — inclusive em dupla/trio/coletivo
  ou dividindo a noite (ex.: "Fulano, Ciclano & {nome}"). Descarte apenas
  cover/tributo tocando repertório de OUTRO artista sem a presença dele.
- Somente a cidade de São Paulo (capital).

Responda APENAS com um array JSON (nada de texto fora dele), no formato:
[{{"artista":"{nome}","titulo":"nome do show","data":"DD/MM/AAAA","local":"local, São Paulo","preco":"R$ valor ou vazio","link":"url de ingresso ou vazio","fonte":"site"}}]
Se não houver shows futuros, responda exatamente: []"""


def chamar_gemini_grounded(prompt):
    """Chama o Gemini com a ferramenta de Google Search. Retorna (texto, uris)."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL_GROUNDING}:generateContent?key={GEMINI_API_KEY}")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0},
    }
    r = requests.post(url, json=payload, timeout=60)
    r.raise_for_status()
    cand = (r.json().get("candidates") or [{}])[0]
    partes = cand.get("content", {}).get("parts", []) or []
    texto = " ".join(p.get("text", "") for p in partes if "text" in p)

    uris = []
    for ch in (cand.get("groundingMetadata", {}) or {}).get("groundingChunks", []) or []:
        u = (ch.get("web") or {}).get("uri")
        if u:
            uris.append(u)
    return texto, uris


def coletar_shows_grounding(nome, estado_ia):
    """
    Busca shows via Gemini+grounding. Retorna lista de shows (ja 'extraidos'),
    ou None se a cota do grounding acabar (o chamador cai no ddgs).
    """
    prompt = montar_prompt_grounding(nome)
    for tentativa in range(1, MAX_RETRIES_IA + 1):
        try:
            texto, uris = chamar_gemini_grounded(prompt)
            shows = _parse_saida_ia(texto)
            out = []
            for s in shows:
                if not isinstance(s, dict):
                    continue
                s["artista"] = s.get("artista") or nome
                if not s.get("link") and uris:
                    s["link"] = uris[0]           # fonte do grounding como fallback
                if not s.get("fonte"):
                    s["fonte"] = nome_fonte(s.get("link", "")) if s.get("link") else "Google"
                s["_confiavel"] = True            # dado do Google em tempo real
                out.append(s)
            return out
        except Exception as e:
            if _eh_rate_limit(e):
                estado_ia["grounding_ok"] = False   # cota do dia -> nao insiste
                print("(grounding sem cota -> ddgs)", end="", flush=True)
                return None
            if tentativa < MAX_RETRIES_IA:
                time.sleep(4)
                continue
            return None
    return None


# --- FONTE SECUNDÁRIA: ddgs + extração por IA ---------------------

def coletar_web_ddg(ddgs, nome):
    queries = [
        f'show "{nome}" "São Paulo" {ANO_ATUAL} ingresso',
        f'"{nome}" show São Paulo site:sympla.com.br',
        f'"{nome}" show São Paulo site:eventim.com.br',
    ]
    candidatos, vistos = [], set()

    for query in queries:
        if len(candidatos) >= MAX_RESULTADOS_POR_ARTISTA:
            break
        items = []
        for backend in ("duckduckgo", "bing", "brave"):
            try:
                items = list(ddgs.text(query, max_results=8,
                                       backend=backend, region="br-pt"))
                break
            except Exception as e:
                if "ratelimit" in str(e).lower() or "429" in str(e):
                    time.sleep(5)
                items = []
        for item in items:
            link = item.get("href", "")
            if not link or link in vistos:
                continue
            vistos.add(link)
            candidatos.append({
                "titulo":  item.get("title", ""),
                "snippet": item.get("body", ""),
                "link":    link,
                "fonte":   nome_fonte(link),
            })
        time.sleep(PAUSA_BUSCA)

    return candidatos[:MAX_RESULTADOS_POR_ARTISTA]


def montar_prompt_extracao(nome, candidatos):
    contexto = ""
    for i, c in enumerate(candidatos, 1):
        contexto += (f"[{i}] Fonte: {c['fonte']}\n"
                     f"Título: {c['titulo']}\n"
                     f"Trecho: {c['snippet']}\n"
                     f"Link: {c['link']}\n\n")

    return f"""Você é um especialista em agenda de shows no Brasil.
Hoje é {HOJE_STR} (ano {ANO_ATUAL}). Artista buscado: "{nome}".
Abaixo há resultados de busca sobre POSSÍVEIS shows deste artista em São Paulo.
Extraia TODOS os shows FUTUROS distintos na CIDADE de São Paulo.

Retorne APENAS um JSON válido (sem markdown, sem texto extra).

REGRAS:
1. ANO CORRETO: use SOMENTE o ano explícito no título/trecho/link. NUNCA invente
   ou avance o ano. Se a data não tiver ano explícito, DESCARTE.
2. FUTURO: só shows com data estritamente posterior a {HOJE_STR}.
3. CIDADE: só São Paulo capital.
4. ARTISTA PRESENTE: inclua shows em que "{nome}" toca (inclusive em dupla/trio/
   coletivo). Descarte cover/tributo de OUTRO artista sem a presença dele.
5. DUPLICATAS: um show por entrada.

Resultados:
{contexto}

Saída (JSON puro):
[{{"artista":"{nome}","titulo":"nome do show","data":"DD/MM/AAAA","local":"local, São Paulo","preco":"R$ valor ou vazio","link":"url","fonte":"site"}}]
Se nada passar, retorne exatamente: []"""


def chamar_gemini_json(prompt):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL_EXTRACAO}:generateContent?key={GEMINI_API_KEY}")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    r = requests.post(url, json=payload, timeout=40)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def chamar_groq(prompt):
    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY)
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=1024,
    )
    return resp.choices[0].message.content


def extrair_shows_ia(nome, candidatos, estado_ia):
    """Extrai shows dos resultados ddgs. Gemini -> fallback Groq. Nunca trava."""
    if not candidatos:
        return []
    prompt = montar_prompt_extracao(nome, candidatos)

    provedores = []
    if estado_ia.get("gemini_ok") and GEMINI_API_KEY:
        provedores.append("gemini")
    if GROQ_API_KEY:
        provedores.append("groq")
    if not provedores and GEMINI_API_KEY:
        provedores.append("gemini")

    for prov in provedores:
        for tentativa in range(1, MAX_RETRIES_IA + 1):
            try:
                texto = chamar_gemini_json(prompt) if prov == "gemini" else chamar_groq(prompt)
                return _parse_saida_ia(texto)
            except Exception as e:
                if _eh_rate_limit(e):
                    if prov == "gemini":
                        estado_ia["gemini_ok"] = False
                        print("(Gemini sem cota -> Groq)", end="", flush=True)
                        break
                    if tentativa < MAX_RETRIES_IA:
                        time.sleep(6 * tentativa)
                        continue
                    print("(Groq rate limit: pulando)", end="", flush=True)
                    return []
                break
    return []


# --- FONTE OPCIONAL: Bandsintown -----------------------------------

def coletar_bandsintown(nome):
    if not BANDSINTOWN_APP_ID:
        return []
    url = (f"https://rest.bandsintown.com/artists/{quote(nome, safe='')}"
           f"/events?app_id={BANDSINTOWN_APP_ID}&date=upcoming")
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return []
        dados = r.json()
    except Exception:
        return []
    if not isinstance(dados, list):
        return []

    shows, limite = [], HOJE + timedelta(days=DIAS_MAX_FUTURO)
    for ev in dados:
        venue = ev.get("venue", {}) or {}
        if not eh_sao_paulo(f"{venue.get('city', '')} {venue.get('region', '')}"):
            continue
        try:
            d = datetime.fromisoformat(ev.get("datetime", "")).date()
        except Exception:
            continue
        if d <= HOJE or d > limite:
            continue
        offers = ev.get("offers", []) or []
        shows.append({
            "artista":   nome,
            "titulo":    ev.get("title", "") or f"{nome} em São Paulo",
            "data":      d.strftime("%d/%m/%Y"),
            "local":     f"{venue.get('name', '')}, São Paulo".strip(", "),
            "preco":     "",
            "link":      offers[0].get("url", "") if offers else ev.get("url", ""),
            "fonte":     "Bandsintown",
            "_confiavel": True,
        })
    return shows


# --- Trava de segurança em Python ---------------------------------

def validar(shows, corpus):
    """Garante datas futuras, plausíveis e (quando aplicável) ano ancorado."""
    anos_fontes = set(re.findall(r"\b(20\d{2})\b", corpus))
    limite = HOJE + timedelta(days=DIAS_MAX_FUTURO)
    validos = []
    for s in shows:
        if not isinstance(s, dict):
            continue
        try:
            d = datetime.strptime((s.get("data") or "").strip(), "%d/%m/%Y").date()
        except ValueError:
            continue
        if d <= HOJE or d > limite:
            continue
        # Ancoragem do ano — pulada para fontes confiaveis (grounding/Bandsintown)
        if not s.get("_confiavel") and anos_fontes and str(d.year) not in anos_fontes:
            continue
        s.pop("_confiavel", None)
        validos.append(s)
    return validos


def deduplicar(shows):
    vistos, unicos = set(), []
    for s in shows:
        chave = normalizar(s.get("artista", "")) + "|" + (s.get("data", "") or "").strip()
        if chave in vistos:
            continue
        vistos.add(chave)
        unicos.append(s)
    return unicos


def salvar(shows):
    def chave_ordem(s):
        try:
            return datetime.strptime(s.get("data", ""), "%d/%m/%Y")
        except Exception:
            return datetime.max
    shows = sorted(shows, key=chave_ordem)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(shows, f, ensure_ascii=False, indent=2)
    return shows


# --- Main ---------------------------------------------------------

def main():
    tem_gemini = bool(GEMINI_API_KEY)
    print("=" * 64)
    print("  ROBO DE SHOWS v2 — PASSO 2: descoberta de shows")
    print(f"  Somente shows futuros a partir de {HOJE_STR}")
    if tem_gemini and GROQ_API_KEY:
        print(f"  IA: Gemini (grounding + extracao) com fallback Groq")
    elif tem_gemini:
        print(f"  IA: Gemini (grounding + extracao)")
    elif GROQ_API_KEY:
        print(f"  IA: Groq (so caminho ddgs; sem grounding)")
    else:
        print("  IA: NENHUMA")
    if tem_gemini:
        print(f"  Busca: Gemini+Google (top {NUM_ARTISTAS_GROUNDING}) + ddgs (demais)")
    else:
        print("  Busca: ddgs (sem GEMINI_API_KEY nao ha grounding)")
    print("=" * 64)
    print()

    if not GEMINI_API_KEY and not GROQ_API_KEY:
        print("Defina GEMINI_API_KEY (recomendado) ou GROQ_API_KEY no .env")
        return

    try:
        from ddgs import DDGS
    except ImportError:
        print("Falta a biblioteca de busca. Rode: pip install ddgs")
        return

    try:
        artistas = carregar_artistas()
    except (FileNotFoundError, ValueError) as e:
        print(f"{e}")
        return

    print(f"  {len(artistas)} artistas na fila\n")

    estado_ia = {"gemini_ok": tem_gemini, "grounding_ok": tem_gemini}
    todos, inicio, interrompido = [], time.monotonic(), False

    with DDGS() as ddgs:
        for i, nome in enumerate(artistas, 1):
            if time.monotonic() - inicio > TEMPO_MAX_SEGUNDOS:
                print(f"\n  ⏱ Tempo máximo atingido (processados {i-1}/{len(artistas)}). Salvando parcial.")
                interrompido = True
                break

            print(f"  [{i:2}/{len(artistas)}] {nome}...", end=" ", flush=True)
            shows_bt = coletar_bandsintown(nome)

            usa_grounding = (i <= NUM_ARTISTAS_GROUNDING and estado_ia["grounding_ok"])
            shows_web, motor = [], "ddgs"

            if usa_grounding:
                g = coletar_shows_grounding(nome, estado_ia)
                if g is not None:
                    motor = "Gemini+Google"
                    shows_web = validar(g, "")
                else:
                    usa_grounding = False   # caiu a cota -> ddgs abaixo

            if not usa_grounding:
                candidatos = coletar_web_ddg(ddgs, nome)
                corpus = " ".join(f"{c['titulo']} {c['snippet']} {c['link']}" for c in candidatos)
                shows_web = validar(extrair_shows_ia(nome, candidatos, estado_ia), corpus)

            shows_bt = validar(shows_bt, "")
            achados = deduplicar(shows_bt + shows_web)

            if achados:
                print(f"-> [{motor}] {len(achados)} show(s)")
                for s in achados:
                    print(f"        - {s.get('data', '?')} | {s.get('local', '')} | {s.get('fonte', '')}")
            else:
                print(f"-> [{motor}] -")

            todos.extend(achados)
            time.sleep(MIN_INTERVALO_IA)

    todos = deduplicar(todos)
    todos = salvar(todos)

    print(f"\n{'-' * 64}")
    status = "PARCIAL (interrompido por tempo)" if interrompido else "completo"
    print(f"{len(todos)} shows futuros unicos em Sao Paulo — resultado {status}")
    print(f"{'-' * 64}")
    for s in todos:
        print(f"  {s.get('artista', ''):<25} | {s.get('data', ''):<12} | {s.get('local', '')}")

    print(f"\nSalvo em {OUTPUT_FILE}\nPasso 2 concluido.\n")


if __name__ == "__main__":
    main()
