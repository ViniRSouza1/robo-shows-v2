"""
PASSO 3 — Enviar notificações de shows via Telegram
=====================================================
Regra de deduplicação:
  - Um show é identificado pelo par (artista + data)
  - Se esse par já foi notificado NA SEMANA ATUAL (seg-dom),
    não é enviado novamente
  - Na semana seguinte, pode ser notificado de novo
    (caso o show ainda seja futuro)

Dependências:
    pip install requests python-dotenv

Como rodar:
    python passo3_telegram.py
"""

import os
import json
import hashlib
import requests
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════
#  ✏️  CONFIGURAÇÃO
# ══════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

SHOWS_FILE    = "shows_encontrados.json"
ENVIADOS_FILE = "shows_notificados.json"

# ══════════════════════════════════════════════════════════════════


# ─── Semana atual ─────────────────────────────────────────────────────────────

def semana_atual():
    """
    Retorna uma string identificando a semana atual no formato 'AAAA-Wnn'.
    Ex: '2026-W15' para a 15ª semana de 2026.
    Usado como chave para agrupar notificações por semana.
    """
    hoje = date.today()
    ano, semana, _ = hoje.isocalendar()
    return f"{ano}-W{semana:02d}"


def inicio_fim_semana():
    """Retorna (segunda-feira, domingo) da semana atual para exibição."""
    hoje = date.today()
    segunda = hoje - timedelta(days=hoje.weekday())
    domingo = segunda + timedelta(days=6)
    return segunda, domingo


# ─── Memória de shows notificados ────────────────────────────────────────────

def carregar_enviados():
    """
    Carrega o histórico de shows notificados.
    Estrutura: {semana: [lista de chaves artista+data]}
    """
    if not os.path.exists(ENVIADOS_FILE):
        return {}
    with open(ENVIADOS_FILE, encoding="utf-8") as f:
        return json.load(f)


def salvar_enviados(enviados):
    """Salva o histórico atualizado."""
    with open(ENVIADOS_FILE, "w", encoding="utf-8") as f:
        json.dump(enviados, f, ensure_ascii=False, indent=2)


def chave_show(show):
    """
    Gera chave única para um show baseada em artista + data.
    Essa é a chave usada para deduplicação — independente do site ou link.
    """
    artista = show.get("artista", "").strip().lower()
    data    = show.get("data", "").strip()
    return hashlib.md5(f"{artista}|{data}".encode()).hexdigest()


def ja_notificado_esta_semana(chave, enviados):
    """Verifica se o show já foi notificado na semana atual."""
    semana = semana_atual()
    chaves_desta_semana = enviados.get(semana, [])
    return chave in chaves_desta_semana


def registrar_como_notificado(chave, enviados):
    """Adiciona o show ao registro da semana atual."""
    semana = semana_atual()
    if semana not in enviados:
        enviados[semana] = []
    if chave not in enviados[semana]:
        enviados[semana].append(chave)


def limpar_semanas_antigas(enviados, manter_semanas=4):
    """
    Remove semanas antigas do histórico para não crescer indefinidamente.
    Mantém apenas as últimas N semanas.
    """
    if len(enviados) <= manter_semanas:
        return enviados

    semanas_ordenadas = sorted(enviados.keys(), reverse=True)
    return {s: enviados[s] for s in semanas_ordenadas[:manter_semanas]}


# ─── Formatação das mensagens ─────────────────────────────────────────────────

def formatar_mensagem_show(show):
    """Monta a mensagem formatada de um show para o Telegram."""
    artista  = show.get("artista", "Artista desconhecido")
    titulo   = show.get("titulo", "")
    data     = show.get("data", "Data a confirmar")
    local    = show.get("local", "Local a confirmar")
    endereco = show.get("endereco", "")
    preco    = show.get("preco", "")
    link     = show.get("link", "")
    fonte    = show.get("fonte", "")

    linhas = [f"🎵 *{artista}*"]

    if titulo and titulo.strip() and normalizar(artista) not in normalizar(titulo):
        linhas.append(f"🎤 {titulo}")

    linhas.append(f"📅 {data}")
    linhas.append(f"📍 {local}")

    if endereco:
        linhas.append(f"🗺 {endereco}")

    if preco:
        linhas.append(f"💰 {preco}")

    if link:
        nome_site = fonte or "Ver ingressos"
        linhas.append(f"🎟 [{nome_site}]({link})")

    return "\n".join(linhas)


def normalizar(texto):
    """Normaliza texto para comparação."""
    import unicodedata
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.lower().strip()


# ─── Envio via Telegram ───────────────────────────────────────────────────────

def enviar_mensagem(texto, parse_mode="Markdown"):
    """Envia uma mensagem via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       texto,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def enviar_shows(shows_novos):
    """Envia as notificações dos shows novos."""
    if not shows_novos:
        return

    hoje = datetime.now().strftime("%d/%m/%Y")
    segunda, domingo = inicio_fim_semana()

    if len(shows_novos) > 1:
        resumo = (
            f"🤖 *Robô de Shows SP* — {hoje}\n"
            f"{'─' * 28}\n"
            f"Encontrei *{len(shows_novos)} show(s) novo(s)* para você esta semana!\n\n"
        )
        enviar_mensagem(resumo)

    for show in shows_novos:
        msg = formatar_mensagem_show(show)
        enviar_mensagem(msg)
        print(f"   ✅ Enviado: {show.get('artista')} — {show.get('data', '')}")

    if len(shows_novos) > 1:
        enviar_mensagem("🎉 Corre lá garantir os ingressos antes que esgotem!")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  🤖 ROBÔ DE SHOWS — PASSO 3: Telegram")
    print("=" * 55)
    print()

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID não encontrados no .env")
        return

    if not os.path.exists(SHOWS_FILE):
        print(f"❌ Arquivo '{SHOWS_FILE}' não encontrado.")
        print("   Rode o passo2_buscar_shows.py primeiro.")
        return

    with open(SHOWS_FILE, encoding="utf-8") as f:
        todos_shows = json.load(f)

    print(f"📋 {len(todos_shows)} show(s) carregados")

    # Trava de segurança: nunca notifica show com data passada, mesmo que o
    # arquivo esteja desatualizado ou tenha escapado da validação do passo 2.
    hoje_date  = date.today()
    futuros    = []
    descartados = 0
    for show in todos_shows:
        data_str = show.get("data", "").strip()
        try:
            d = datetime.strptime(data_str, "%d/%m/%Y").date()
        except (ValueError, AttributeError):
            # Sem data válida: mantém (pode ser "a confirmar") para não perder show real.
            futuros.append(show)
            continue
        if d > hoje_date:
            futuros.append(show)
        else:
            descartados += 1
            print(f"   🚫 Ignorado (data passada): "
                  f"{show.get('artista')} — {data_str}")

    if descartados:
        print(f"   ({descartados} show(s) com data passada descartados)")

    todos_shows = futuros

    if not todos_shows:
        print("ℹ  Nenhum show encontrado para notificar.")
        return

    # Carrega histórico e identifica semana atual
    enviados = carregar_enviados()
    semana   = semana_atual()
    segunda, domingo = inicio_fim_semana()

    print(f"📅 Semana atual: {semana} "
          f"({segunda.strftime('%d/%m')} a {domingo.strftime('%d/%m/%Y')})")

    # Filtra shows não notificados esta semana
    shows_novos  = []
    shows_ja_vis = 0

    for show in todos_shows:
        chave = chave_show(show)
        if ja_notificado_esta_semana(chave, enviados):
            shows_ja_vis += 1
            print(f"   ⏭ Já notificado esta semana: "
                  f"{show.get('artista')} — {show.get('data', '')}")
        else:
            shows_novos.append(show)

    print(f"\n🔔 {len(shows_novos)} show(s) novo(s) para notificar")
    print(f"   ({shows_ja_vis} já notificados esta semana — ignorados)\n")

    if not shows_novos:
        print("✅ Nenhuma novidade esta semana. Nada enviado.")
        return

    # Envia notificações
    print("📨 Enviando para o Telegram...\n")
    try:
        enviar_shows(shows_novos)

        # Registra os shows enviados nesta semana
        for show in shows_novos:
            registrar_como_notificado(chave_show(show), enviados)

        # Limpa semanas antigas e salva
        enviados = limpar_semanas_antigas(enviados, manter_semanas=4)
        salvar_enviados(enviados)

        print(f"\n💾 {len(shows_novos)} show(s) registrados para a semana {semana}")
        print("\n✅ Passo 3 concluído! Verifique o Telegram.")

    except requests.exceptions.HTTPError as e:
        print(f"\n❌ Erro ao enviar mensagem: {e}")
        print("   Verifique TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env")

    except requests.exceptions.ConnectionError:
        print("\n❌ Sem conexão com a internet. Tente novamente.")


if __name__ == "__main__":
    main()