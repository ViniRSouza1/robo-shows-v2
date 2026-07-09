"""
PASSO 2 (v2) — Descoberta multi-fonte de shows futuros em São Paulo
===================================================================
Objetivo: MAXIMIZAR o recall (achar o máximo de shows futuros possível) sem
travar por rate limit / timeout.

Fontes de busca (em ordem de preferência):
  1. Google Custom Search JSON API  -> se GOOGLE_API_KEY + GOOGLE_CSE_ID
     estiverem definidos. Retorna resultados REAIS do Google (a mesma
     cobertura que voce ve no navegador), 100 buscas/dia gratis, sem cartao.
     E a fonte mais confiavel para achar eventos pequenos (ex.: Sympla).
  2. DuckDuckGo/Bing/Brave (biblioteca ddgs) -> fallback automatico, sem chave,
     usado quando o CSE nao esta configurado ou estoura a cota diaria.

IA de extração/validação (100% gratuita, escolhida automaticamente):
  - Google Gemini (se GEMINI_API_KEY existir) — preferida.
  - Groq llama-3.3-70b (fallback, se só GROQ_API_KEY existir).

Blindagens contra timeout (o que quebrou a execucao anterior):
  - Backoff de rate limit curto e com desistencia (nao trava 5 min por artista).
  - Orcamento GLOBAL de tempo: ao se aproximar do limite, encerra a busca e
    ainda assim salva o que encontrou (garante que o Passo 3 rode e notifique).
  - O arquivo de saida e sempre gravado, mesmo com resultado parcial.

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

# Resultados de busca por artista (mais = recall maior).
MAX_RESULTADOS_POR_ARTISTA = 20

# Janela máxima de antecedência aceita (evita datas empurradas pro futuro).
DIAS_MAX_FUTURO = 540    # ~18 meses

# Orçamento GLOBAL de tempo. Ao ultrapassar, encerra a busca e salva o que
# encontrou (o job do GitHub tem timeout de 60 min; ficamos com folga).
TEMPO_MAX_SEGUNDOS = 45 * 60

# Cota diária do Google CSE (free tier = 100 buscas/dia).
CSE_ORCAMENTO_DIARIO = 100

# Estratégia híbrida por prioridade: os N primeiros artistas (os mais ouvidos)
# usam o Google (mais assertivo), trazendo os CSE_RESULTADOS melhores resultados.
# Os demais artistas usam o ddgs. Assim priorizamos os favoritos e poupamos cota.
NUM_ARTISTAS_GOOGLE = 20
CSE_RESULTADOS      = 3

PAUSA_BUSCA      = 3     # segundos entre buscas no ddgs (fallback)
MIN_INTERVALO_IA = 5     # segundos entre chamadas de IA (respeita free tier)
MAX_RETRIES_IA   = 3     # tentativas por artista antes de desistir (sem travar)

# ==================================================================

def _env(nome):
    """Lê variável de ambiente removendo espaços/quebras de linha acidentais
    (secrets colados com newline quebram URLs e invalidam chaves de API)."""
    return (os.getenv(nome) or "").strip()

GOOGLE_API_KEY = _env("GOOGLE_API_KEY")
GOOGLE_CSE_ID  = _env("GOOGLE_CSE_ID")

GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL   = "gemini-2.5-flash-lite"   # limites free maiores que o flash
GROQ_API_KEY   = _env("GROQ_API_KEY")
GROQ_MODEL     = "llama-3.3-70b-versatile"

BANDSINTOWN_APP_ID = _env("BANDSINTOWN_APP_ID")   # opcional

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


# --- FONTE 1: Google Custom Search (resultados reais do Google) ----

def coletar_google_cse(nome, estado):
    """
    Busca via Google Custom Search JSON API.
    Retorna lista de candidatos, ou None se a cota/credencial falhar
    (nesse caso o chamador desliga o CSE e usa o ddgs).
    A query espelha a busca que o usuario faz no Google e traz os
    CSE_RESULTADOS melhores resultados.
    """
    queries = [f'{nome} show sp']
    candidatos, vistos = [], set()

    for q in queries:
        if estado["budget"] <= 0:
            break
        try:
            r = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": GOOGLE_API_KEY, "cx": GOOGLE_CSE_ID, "q": q,
                    "num": CSE_RESULTADOS, "gl": "br", "hl": "pt-BR",
                },
                timeout=20,
            )
            estado["budget"] -= 1
            if r.status_code != 200:   # cota, credencial invalida, API desativada...
                try:
                    motivo = r.json().get("error", {}).get("message", "")[:180]
                except Exception:
                    motivo = r.text[:180]
                print(f"(CSE HTTP {r.status_code}: {motivo} -> ddgs)", end="", flush=True)
                return None
            items = r.json().get("items", []) or []
        except Exception:
            continue

        for it in items:
            link = it.get("link", "")
            if not link or link in vistos:
                continue
            vistos.add(link)
            candidatos.append({
                "titulo":  it.get("title", ""),
                "snippet": it.get("snippet", ""),
                "link":    link,
                "fonte":   nome_fonte(link),
            })

    return candidatos[:MAX_RESULTADOS_POR_ARTISTA]


# --- FONTE 2: ddgs (fallback sem chave) ---------------------------

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
        for backend in ("duckduckgo", "bing", "brave"):
            try:
                items = list(ddgs.text(query, max_results=8,
                                       backend=backend, region="br-pt"))
                break   # deu certo com este backend
            except Exception as e:
                msg = str(e).lower()
                if "ratelimit" in msg or "429" in msg:
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


def coletar_candidatos(nome, indice, ddgs, estado):
    """
    Estratégia híbrida por prioridade:
      - os NUM_ARTISTAS_GOOGLE primeiros (mais ouvidos) usam o Google (CSE);
      - os demais usam o ddgs.
    Se o CSE falhar/estourar a cota, cai no ddgs automaticamente.
    Retorna (candidatos, motor_usado).
    """
    usa_google = (indice <= NUM_ARTISTAS_GOOGLE
                  and estado["usar_cse"] and estado["budget"] > 0)
    if usa_google:
        cand = coletar_google_cse(nome, estado)
        if cand is not None:
            return cand, "Google"
        estado["usar_cse"] = False   # CSE falhou -> daqui pra frente usa ddgs
    return coletar_web_ddg(ddgs, nome), "ddgs"


# --- FONTE 3: Bandsintown (opcional) ------------------------------

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
4. ARTISTA PRESENTE: inclua shows em que "{nome}" realmente toca — como atração
   principal, em dupla/trio/coletivo, OU dividindo a noite com outros artistas.
   Exemplo que DEVE ser incluído: "Fulano, Ciclano & {nome} no Fino da Bossa".
   Descarte SOMENTE: banda cover/tributo tocando repertório de OUTRO artista sem
   a presença de "{nome}", ou "{nome}" citado por acaso sem tocar no evento.
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
    """Extrai shows via IA. Backoff CURTO e desiste sem travar a execucao."""
    if not candidatos:
        return []
    prompt = montar_prompt(nome, candidatos)
    usar_gemini = bool(GEMINI_API_KEY)
    texto = ""

    for tentativa in range(1, MAX_RETRIES_IA + 1):
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
                if tentativa >= MAX_RETRIES_IA:
                    print("(IA rate limit: pulando)", end="", flush=True)
                    return []          # desiste do artista, NAO trava a execucao
                time.sleep(8 * tentativa)   # 8s, 16s — curto
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
    usar_cse = bool(GOOGLE_API_KEY and GOOGLE_CSE_ID)
    print("=" * 64)
    print("  ROBO DE SHOWS v2 — PASSO 2: descoberta multi-fonte")
    print(f"  Somente shows futuros a partir de {HOJE_STR}")
    ia = "Gemini (%s)" % GEMINI_MODEL if GEMINI_API_KEY else ("Groq llama-3.3" if GROQ_API_KEY else "NENHUMA")
    print(f"  IA: {ia}")
    if usar_cse:
        print(f"  Busca: Google (top {NUM_ARTISTAS_GOOGLE} artistas) + ddgs (demais)")
    else:
        print("  Busca: ddgs (Google CSE nao configurado)")
    print(f"  Bandsintown: {'ativada' if BANDSINTOWN_APP_ID else 'desativada'}")
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

    estado = {"usar_cse": usar_cse, "budget": CSE_ORCAMENTO_DIARIO}
    todos = []
    inicio = time.monotonic()
    interrompido = False

    with DDGS() as ddgs:
        for i, nome in enumerate(artistas, 1):
            # Orçamento global de tempo: encerra e salva o que já tem.
            if time.monotonic() - inicio > TEMPO_MAX_SEGUNDOS:
                print(f"\n  ⏱ Tempo máximo atingido — encerrando busca "
                      f"(processados {i-1}/{len(artistas)}). Salvando o que foi encontrado.")
                interrompido = True
                break

            print(f"  [{i:2}/{len(artistas)}] {nome}...", end=" ", flush=True)

            shows_bt          = coletar_bandsintown(nome)
            candidatos, motor = coletar_candidatos(nome, i, ddgs, estado)
            corpus = " ".join(f"{c['titulo']} {c['snippet']} {c['link']}" for c in candidatos)

            shows_web = validar(extrair_shows_ia(nome, candidatos), corpus) if candidatos else []
            shows_bt  = validar(shows_bt, corpus)

            achados = deduplicar(shows_bt + shows_web)
            if achados:
                print(f"-> [{motor}] {len(achados)} show(s)")
                for s in achados:
                    print(f"        - {s.get('data', '?')} | {s.get('local', '')} | {s.get('fonte', '')}")
            else:
                print(f"-> [{motor}] -")

            todos.extend(achados)
            time.sleep(MIN_INTERVALO_IA)   # ritmo que respeita o free tier da IA

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
