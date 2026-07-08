"""
PASSO 2 (v2) — Descoberta multi-fonte de shows futuros em São Paulo
===================================================================
Objetivo desta versão: MAXIMIZAR o recall (achar o máximo de shows futuros
possível), corrigindo as duas falhas de recall da v1:

  1. NÃO filtra mais os resultados para uma whitelist fixa de sites.
     Qualquer site (Ingresse, casas de show, imprensa, etc.) pode entrar.
  2. NÃO descarta mais "páginas de artista" — elas normalmente contêm
     justamente a data do show.

Fontes:
  - DuckDuckGo (ddgs): busca ampla, sem chave, 100% grátis. Fonte principal.
  - Bandsintown API: OPCIONAL. Só é usada se BANDSINTOWN_APP_ID estiver
    definido (o endpoint público nega app_id arbitrário — precisa de um
    app_id registrado). Deixada pronta para quando você registrar.

IA de extração/validação (100% gratuita, escolhida automaticamente):
  - Google Gemini 2.5 Flash  (se GEMINI_API_KEY existir) — preferida:
    contexto grande (lê muitos resultados de uma vez) e melhor raciocínio
    de datas.
  - Groq llama-3.3-70b       (fallback, se só GROQ_API_KEY existir).

A IA extrai shows futuros em SP; uma trava em Python garante que o ano não
foi inventado/avançado (ancoragem do ano nas fontes).

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

# Resultados de busca por artista (mais = recall maior, porém mais lento e
# mais risco de rate limit no DuckDuckGo).
MAX_RESULTADOS_POR_ARTISTA = 24

# Janela máxima de antecedência aceita (evita datas empurradas pro futuro).
DIAS_MAX_FUTURO = 540    # ~18 meses

PAUSA_BUSCA = 3          # segundos entre buscas no DuckDuckGo
PAUSA_IA    = 4          # segundos entre chamadas de IA
MAX_RETRIES = 5

# ==================================================================

GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL       = "gemini-2.5-flash"
GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
GROQ_MODEL         = "llama-3.3-70b-versatile"
BANDSINTOWN_APP_ID = os.getenv("BANDSINTOWN_APP_ID")   # opcional

ARTISTAS_FILE = "artistas_favoritos.json"
OUTPUT_FILE   = "shows_encontrados.json"

HOJE      = date.today()
HOJE_STR  = HOJE.strftime("%d/%m/%Y")
ANO_ATUAL = HOJE.year
ANO_PROX  = ANO_ATUAL + 1

# Nomes amigáveis por domínio (apenas cosmético; NÃO filtra nada).
FONTES_CONHECIDAS = {
    "bileto.sympla.com.br": "Sympla",
    "sympla.com.br":        "Sympla",
    "eventim.com.br":       "Eventim",
    "ingresse.com":         "Ingresse",
    "ticket360.com.br":     "Ticket360",
    "ticketmaster.com.br":  "Ticketmaster",
    "clubedoingresso.com":  "Clube do Ingresso",
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

    # passo1 salva 'score' (histórico de reprodução) — ordena por ele.
    artistas = sorted(artistas, key=lambda x: x.get("score", 0), reverse=True)

    if MODO == "top_n":
        print(f"  Modo: top {TOP_N} artistas mais ouvidos")
        return [a["nome"] for a in artistas[:TOP_N]]

    print(f"  Modo: todos ({len(artistas)} artistas)")
    return [a["nome"] for a in artistas]


# --- FONTE 1: DuckDuckGo (ampla, sem whitelist) -------------------

def coletar_web_ddg(ddgs, nome):
    queries = [
        f'show "{nome}" "São Paulo" {ANO_ATUAL} ingresso',
        f'show "{nome}" "São Paulo" {ANO_PROX} ingresso',
        f'"{nome}" show São Paulo site:sympla.com.br',
        f'"{nome}" show São Paulo site:eventim.com.br',
        f'"{nome}" show São Paulo site:ingresse.com',
    ]

    candidatos = []
    vistos = set()

    for query in queries:
        if len(candidatos) >= MAX_RESULTADOS_POR_ARTISTA:
            break
        try:
            items = list(ddgs.text(query, max_results=8, region="br-pt"))
        except Exception as e:
            msg = str(e).lower()
            if "ratelimit" in msg or "429" in msg:
                print("(DDG 30s)", end="", flush=True)
                time.sleep(30)
            continue

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


# --- FONTE 2: Bandsintown (opcional) ------------------------------

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

    shows = []
    limite = HOJE + timedelta(days=DIAS_MAX_FUTURO)
    for ev in dados:
        venue = ev.get("venue", {}) or {}
        cidade = f"{venue.get('city', '')} {venue.get('region', '')}"
        if not eh_sao_paulo(cidade):
            continue
        try:
            d = datetime.fromisoformat(ev.get("datetime", "")).date()
        except Exception:
            continue
        if d <= HOJE or d > limite:
            continue
        offers = ev.get("offers", []) or []
        link = offers[0].get("url", "") if offers else ev.get("url", "")
        shows.append({
            "artista":   nome,
            "titulo":    ev.get("title", "") or f"{nome} em São Paulo",
            "data":      d.strftime("%d/%m/%Y"),
            "local":     f"{venue.get('name', '')}, São Paulo".strip(", "),
            "preco":     "",
            "link":      link,
            "fonte":     "Bandsintown",
            "_confiavel": True,   # data já vem estruturada e com ano correto
        })
    return shows


# --- IA: extração + validação -------------------------------------

def montar_prompt(nome, candidatos):
    contexto = ""
    for i, c in enumerate(candidatos, 1):
        contexto += (f"[{i}] Fonte: {c['fonte']}\n"
                     f"Título: {c['titulo']}\n"
                     f"Trecho: {c['snippet']}\n"
                     f"Link: {c['link']}\n\n")

    return f"""Você é um especialista em agenda de shows no Brasil.
Hoje é {HOJE_STR} (ano {ANO_ATUAL}). Artista buscado: "{nome}".

Abaixo há resultados de busca sobre POSSÍVEIS shows deste artista em São Paulo.
Extraia TODOS os shows FUTUROS distintos na CIDADE de São Paulo. Seja abrangente:
uma página de artista/agenda também vale se o trecho revelar uma data concreta.

Retorne APENAS um JSON válido (sem markdown, sem texto extra).

REGRAS OBRIGATÓRIAS:
1. ANO CORRETO (a mais importante): use SOMENTE o ano que aparece EXPLÍCITO no
   título, no trecho ou no link. É PROIBIDO adivinhar, assumir ou AVANÇAR o ano.
   Exemplo de ERRO a evitar: a fonte diz "10/07/2025" e você responde "10/07/2026".
   Se a data não tiver ANO explícito, DESCARTE o show.
2. FUTURO: inclua só shows com data estritamente posterior a {HOJE_STR}.
3. CIDADE: só São Paulo capital. Descarte outras cidades (mesmo no estado de SP).
4. ARTISTA CERTO: shows do próprio "{nome}" (ou tributo onde ele é o homenageado).
   Descarte banda cover tocando repertório de outro artista, ou ele só como abertura.
5. DUPLICATAS: um show por entrada.

Resultados:
{contexto}

Saída (JSON puro):
[{{"artista":"{nome}","titulo":"nome do show","data":"DD/MM/AAAA","local":"local, São Paulo","preco":"R$ valor ou vazio","link":"url","fonte":"site"}}]

Se nada passar, retorne exatamente: []"""


def chamar_gemini(prompt):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    r = requests.post(url, json=payload, timeout=40)
    r.raise_for_status()
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


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


def extrair_shows_ia(nome, candidatos):
    if not candidatos:
        return []
    prompt = montar_prompt(nome, candidatos)
    usar_gemini = bool(GEMINI_API_KEY)
    texto = ""

    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            texto = chamar_gemini(prompt) if usar_gemini else chamar_groq(prompt)
            texto = (texto or "").strip()
            if texto.startswith("```"):
                texto = "\n".join(texto.split("\n")[1:-1]).strip()
            if not texto or texto == "[]":
                return []
            shows = json.loads(texto)
            return shows if isinstance(shows, list) else []

        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 429:
                espera = 20 * tentativa
                print(f"(IA {espera}s)", end="", flush=True)
                time.sleep(espera)
                continue
            if usar_gemini and GROQ_API_KEY:   # Gemini falhou -> cai pro Groq
                usar_gemini = False
                continue
            return []

        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", texto, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    return []
            return []

        except Exception:
            if usar_gemini and GROQ_API_KEY:
                usar_gemini = False
                continue
            return []

    return []


# --- Trava de segurança em Python ---------------------------------

def validar(shows, corpus):
    """Garante datas futuras, plausíveis e com ano ancorado nas fontes."""
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
        # Ancoragem do ano — pulada para fontes estruturadas confiáveis (Bandsintown)
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


# --- Main ---------------------------------------------------------

def main():
    print("=" * 64)
    print("  ROBO DE SHOWS v2 — PASSO 2: descoberta multi-fonte")
    print(f"  Somente shows futuros a partir de {HOJE_STR}")
    ia = "Gemini 2.5 Flash" if GEMINI_API_KEY else ("Groq llama-3.3" if GROQ_API_KEY else "NENHUMA")
    print(f"  IA: {ia}")
    bt = "ativada" if BANDSINTOWN_APP_ID else "desativada (defina BANDSINTOWN_APP_ID)"
    print(f"  Bandsintown: {bt}")
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

    todos = []
    with DDGS() as ddgs:
        for i, nome in enumerate(artistas, 1):
            print(f"  [{i:2}/{len(artistas)}] {nome}...", end=" ", flush=True)

            shows_bt = coletar_bandsintown(nome)          # fonte estruturada (opcional)
            candidatos = coletar_web_ddg(ddgs, nome)       # fonte web ampla
            corpus = " ".join(f"{c['titulo']} {c['snippet']} {c['link']}" for c in candidatos)

            shows_web = []
            if candidatos:
                shows_web = validar(extrair_shows_ia(nome, candidatos), corpus)

            shows_bt = validar(shows_bt, corpus)

            achados = deduplicar(shows_bt + shows_web)
            if achados:
                print(f"-> {len(achados)} show(s)")
                for s in achados:
                    print(f"        - {s.get('data', '?')} | {s.get('local', '')} | {s.get('fonte', '')}")
            else:
                print("-> -")

            todos.extend(achados)
            time.sleep(PAUSA_IA)

    todos = deduplicar(todos)

    def chave_ordem(s):
        try:
            return datetime.strptime(s.get("data", ""), "%d/%m/%Y")
        except Exception:
            return datetime.max
    todos.sort(key=chave_ordem)

    print(f"\n{'-' * 64}")
    print(f"{len(todos)} shows futuros unicos em Sao Paulo")
    print(f"{'-' * 64}")
    for s in todos:
        print(f"  {s.get('artista', ''):<25} | {s.get('data', ''):<12} | {s.get('local', '')}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=2)
    print(f"\nSalvo em {OUTPUT_FILE}\nPasso 2 concluido.\n")


if __name__ == "__main__":
    main()
