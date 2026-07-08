"""
PASSO 1 — Capturar artistas favoritos via Spotify API (v3)
===========================================================
Usa o endpoint /me/top/artists que retorna os artistas
que você MAIS OUVIU em diferentes janelas de tempo:

  - short_term  → últimas 4 semanas
  - medium_term → últimos 6 meses
  - long_term   → último ano

A união das três janelas captura artistas que você:
  ✅ Está ouvindo agora
  ✅ Ouve na sua rotina
  ✅ Ama mas ouve em ciclos (John Mayer, Erykah Badu, etc.)

Muito mais preciso do que músicas curtidas.

Dependências:
    pip install spotipy python-dotenv

Como rodar:
    python passo1_spotify.py
"""

import os
import json
import time
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

# ─── Configurações ────────────────────────────────────────────────────────────

SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI  = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

# Quantos artistas buscar por janela de tempo (máx: 50)
TOP_POR_JANELA = 50

# Total máximo de artistas únicos no arquivo final
MAX_ARTISTAS = 100

OUTPUT_FILE = "artistas_favoritos.json"

# Janelas de tempo do Spotify
JANELAS = {
    "short_term":  "últimas 4 semanas",
    "medium_term": "últimos 6 meses",
    "long_term":   "último ano",
}


# ─── Autenticação ─────────────────────────────────────────────────────────────

def criar_cliente():
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise ValueError(
            "❌ SPOTIFY_CLIENT_ID ou SPOTIFY_CLIENT_SECRET não encontrados no .env"
        )

    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri=SPOTIFY_REDIRECT_URI,
            # user-top-read = acesso ao histórico de artistas mais ouvidos
            scope="user-top-read user-library-read",
            cache_path=".spotify_cache"
        )
    )


# ─── Busca de top artists ─────────────────────────────────────────────────────

def buscar_top_artists(sp, time_range, limit=50):
    """
    Busca os artistas mais ouvidos numa janela de tempo específica.
    time_range: "short_term" | "medium_term" | "long_term"
    """
    resultado = sp.current_user_top_artists(
        limit=limit,
        time_range=time_range
    )
    return resultado.get("items", [])


def consolidar_artistas(sp):
    """
    Busca artistas das três janelas de tempo e consolida numa lista única.
    Artistas que aparecem em múltiplas janelas recebem score mais alto.
    """
    # score: quantas janelas o artista aparece + posição (50 = 1º, 1 = 50º)
    scores = {}
    artistas_info = {}

    for time_range, descricao in JANELAS.items():
        print(f"  🎵 Buscando top artistas: {descricao}...")
        artistas = buscar_top_artists(sp, time_range, limit=TOP_POR_JANELA)

        for posicao, artista in enumerate(artistas):
            nome = artista.get("name", "").strip()
            if not nome:
                continue

            # Score baseado na posição (1º lugar = 50 pts, último = 1 pt)
            pts_posicao = TOP_POR_JANELA - posicao

            if nome not in scores:
                scores[nome] = 0
                artistas_info[nome] = {
                    "nome": nome,
                    "id_spotify": artista.get("id", ""),
                    "generos": artista.get("genres", []),
                    "popularidade": artista.get("popularity", 0),
                    "url_spotify": artista.get("external_urls", {}).get("spotify", ""),
                    "janelas": [],
                    "score": 0,
                }

            scores[nome] += pts_posicao
            artistas_info[nome]["score"] = scores[nome]

            # Registra em quais janelas o artista apareceu
            if descricao not in artistas_info[nome]["janelas"]:
                artistas_info[nome]["janelas"].append(descricao)

        print(f"     → {len(artistas)} artistas encontrados")
        time.sleep(0.3)

    return artistas_info, scores


# ─── Complemento: músicas curtidas ───────────────────────────────────────────

def buscar_artistas_de_curtidas(sp, total=200):
    """
    Complementa com artistas das músicas curtidas,
    para capturar artistas que você curte mas não ouve frequentemente.
    """
    print(f"\n  🎵 Buscando artistas de músicas curtidas (complemento)...")
    artistas_curtidas = {}
    offset = 0

    while offset < total:
        resultado = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = resultado.get("items", [])
        if not items:
            break

        for item in items:
            track = item.get("track")
            if not track:
                continue
            for artista in track.get("artists", []):
                nome = artista.get("name", "").strip()
                aid  = artista.get("id", "")
                if nome and nome not in artistas_curtidas:
                    artistas_curtidas[nome] = {
                        "nome": nome,
                        "id_spotify": aid,
                        "url_spotify": f"https://open.spotify.com/artist/{aid}" if aid else "",
                    }

        offset += len(items)
        print(f"     → {offset} músicas processadas...", end="\r")
        time.sleep(0.1)

    print(f"\n     → {len(artistas_curtidas)} artistas únicos nas curtidas")
    return artistas_curtidas


# ─── Consolidação final ───────────────────────────────────────────────────────

def montar_lista_final(artistas_info, scores, artistas_curtidas, max_artistas=100):
    """
    Monta a lista final priorizando artistas do histórico de reprodução.
    Adiciona artistas das curtidas que não estão no histórico como complemento.
    """
    # Ordena por score (histórico de reprodução)
    top_historico = sorted(
        artistas_info.values(),
        key=lambda x: x["score"],
        reverse=True
    )

    lista_final = []
    nomes_incluidos = set()

    # 1. Adiciona artistas do histórico de reprodução
    for a in top_historico:
        if len(lista_final) >= max_artistas:
            break
        lista_final.append({
            "nome":        a["nome"],
            "id_spotify":  a["id_spotify"],
            "generos":     a["generos"],
            "popularidade": a["popularidade"],
            "url_spotify": a["url_spotify"],
            "janelas":     a["janelas"],
            "score":       a["score"],
            "origem":      "historico",
        })
        nomes_incluidos.add(a["nome"])

    # 2. Complementa com artistas das curtidas (que não estão no histórico)
    vagas_restantes = max_artistas - len(lista_final)
    complementos = 0
    for nome, info in artistas_curtidas.items():
        if vagas_restantes <= 0:
            break
        if nome in nomes_incluidos:
            continue
        lista_final.append({
            "nome":        nome,
            "id_spotify":  info["id_spotify"],
            "generos":     [],
            "popularidade": 0,
            "url_spotify": info["url_spotify"],
            "janelas":     ["curtidas"],
            "score":       0,
            "origem":      "curtidas",
        })
        nomes_incluidos.add(nome)
        vagas_restantes -= 1
        complementos += 1

    return lista_final, complementos


# ─── Exibição do resumo ───────────────────────────────────────────────────────

def exibir_resumo(artistas):
    print("\n" + "─" * 55)
    print(f"  {'ARTISTA':<28} {'JANELAS':<25} SCORE")
    print("─" * 55)

    for i, a in enumerate(artistas[:30], 1):
        janelas = ", ".join(a.get("janelas", []))
        score   = a.get("score", 0)
        origem  = "★" if a.get("origem") == "historico" else " "
        print(f"  {i:2}. {origem} {a['nome']:<26} {janelas:<25} {score}")

    if len(artistas) > 30:
        print(f"  ... e mais {len(artistas) - 30} artistas salvos no arquivo.")
    print("─" * 55)
    print("  ★ = artista do histórico de reprodução")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  🤖 ROBÔ DE SHOWS — PASSO 1: Spotify (v3)")
    print("  📊 Fonte: histórico de reprodução + curtidas")
    print("=" * 55)
    print()

    sp = criar_cliente()

    usuario = sp.current_user()
    print(f"✅ Conectado como: {usuario['display_name']} ({usuario['id']})\n")

    # Busca artistas do histórico (3 janelas de tempo)
    artistas_info, scores = consolidar_artistas(sp)
    print(f"\n  → {len(artistas_info)} artistas únicos no histórico de reprodução")

    # Complementa com curtidas
    artistas_curtidas = buscar_artistas_de_curtidas(sp, total=200)

    # Monta lista final
    lista_final, complementos = montar_lista_final(
        artistas_info, scores, artistas_curtidas, max_artistas=MAX_ARTISTAS
    )

    print(f"\n  📊 Composição da lista final:")
    historico = sum(1 for a in lista_final if a["origem"] == "historico")
    print(f"     • {historico} artistas do histórico de reprodução")
    print(f"     • {complementos} artistas das músicas curtidas (complemento)")
    print(f"     • {len(lista_final)} total")

    exibir_resumo(lista_final)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(lista_final, f, ensure_ascii=False, indent=2)

    print(f"\n💾 Artistas salvos em: {OUTPUT_FILE}")
    print("\n✅ Passo 1 concluído!")
    print("   Próximo: rode o passo2_ia.py para buscar shows.\n")


if __name__ == "__main__":
    main()