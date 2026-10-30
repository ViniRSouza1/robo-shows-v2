"""
Renova o acesso do robô ao Spotify
==================================
Desde 20/07/2026 o Spotify expira o acesso (refresh token) de apps a cada
6 meses. Quando o robô avisar no Telegram que o acesso está vencendo/venceu,
rode este script no seu computador, dentro da pasta robo-shows-v2:

    python renovar_spotify.py

O que ele faz:
  1. Apaga o cache antigo (.spotify_cache).
  2. Abre o navegador para você autorizar o app no Spotify.
  3. Atualiza no GitHub o secret SPOTIFY_CACHE e a variável SPOTIFY_TOKEN_DATA
     (usada para avisar antes do próximo vencimento).

Requisitos: arquivo .env com SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET /
SPOTIFY_REDIRECT_URI, e o GitHub CLI (gh) instalado e autenticado.
"""

import os
import shutil
import subprocess
import sys
from datetime import date

REPO       = "ViniRSouza1/robo-shows-v2"
CACHE_FILE = ".spotify_cache"


def achar_gh():
    gh = shutil.which("gh")
    if gh:
        return gh
    padrao = r"C:\Program Files\GitHub CLI\gh.exe"
    if os.path.exists(padrao):
        return padrao
    sys.exit("GitHub CLI (gh) não encontrado. Instale com: winget install GitHub.cli")


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.exists(".env"):
        sys.exit("Arquivo .env não encontrado nesta pasta (precisa das chaves do Spotify).")

    gh = achar_gh()

    # 1. Cache antigo fora, para forçar uma autorização nova
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)

    # 2. Autorização (abre o navegador)
    print("Abrindo o navegador para autorizar o app no Spotify...")
    from passo1_spotify import criar_cliente
    usuario = criar_cliente().current_user()
    print(f"Autorizado como: {usuario['display_name']}")

    with open(CACHE_FILE, encoding="utf-8") as f:
        cache = f.read().strip()

    # 3. Atualiza o GitHub
    subprocess.run([gh, "secret", "set", "SPOTIFY_CACHE", "--repo", REPO],
                   input=cache, text=True, check=True)
    hoje = date.today().isoformat()
    subprocess.run([gh, "variable", "set", "SPOTIFY_TOKEN_DATA", "--repo", REPO,
                    "--body", hoje], check=True)

    print(f"\nPronto! Secret SPOTIFY_CACHE atualizado e SPOTIFY_TOKEN_DATA={hoje}.")
    print("O próximo vencimento será em ~6 meses; o robô avisa no Telegram antes.")


if __name__ == "__main__":
    main()
