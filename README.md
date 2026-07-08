# Robo de Shows v2

Robo que descobre os proximos shows em Sao Paulo dos artistas que voce mais
ouve no Spotify e envia um resumo diario no Telegram. 100% gratuito.

## O que mudou da v1 para a v2

A v2 foca em **recall**: encontrar o maximo de shows futuros possivel. As duas
causas de shows perdidos na v1 foram corrigidas:

1. A v1 so aceitava resultados de 5 sites de ingresso (whitelist). Shows
   anunciados em outros lugares (Ingresse, casas de show, imprensa) eram
   descartados antes mesmo da IA ver. A v2 **nao usa mais whitelist**.
2. A v1 descartava "paginas de artista" — justamente onde a data costuma
   estar. A v2 **considera essas paginas**.

Alem disso, a IA de validacao passou a ser o **Google Gemini 2.5 Flash**
(contexto maior + melhor raciocinio de datas), com **fallback automatico para
o Groq**. A trava de ancoragem de ano da v1 (impede o robo de avancar o ano de
um show passado) foi mantida.

## Arquitetura

    passo1_spotify.py        -> lista os artistas mais ouvidos (Spotify API)
    passo2_buscar_shows.py   -> descoberta multi-fonte + extracao/validacao por IA
    passo3_telegram.py       -> envia as notificacoes (dedup por semana)

### Fontes de busca (passo 2)
- **DuckDuckGo** (biblioteca `ddgs`): sem chave, gratis. Fonte principal.
- **Bandsintown** (opcional): so liga se `BANDSINTOWN_APP_ID` estiver definido.
  O endpoint publico exige um app_id registrado (parceria) — por isso vem
  desligado por padrao, pronto para ligar no futuro.

### IA (passo 2) - escolhida automaticamente
- **Gemini 2.5 Flash** se `GEMINI_API_KEY` existir (recomendado).
- **Groq llama-3.3** como fallback se so houver `GROQ_API_KEY`.

## Configuracao

1. Copie `.env.example` para `.env` e preencha as chaves.
2. Chave gratuita do Gemini (sem cartao): https://aistudio.google.com/app/apikey
3. Instale as dependencias:

       pip install -r requirements.txt

4. Rode em ordem:

       python passo1_spotify.py
       python passo2_buscar_shows.py
       python passo3_telegram.py

## Automacao (GitHub Actions)

O workflow `.github/workflows/robo.yml` roda todo dia as 11h UTC (8h em Brasilia).
Configure os secrets no repositorio: `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`,
`SPOTIFY_REDIRECT_URI`, `SPOTIFY_CACHE`, `GEMINI_API_KEY`, `GROQ_API_KEY`,
`BANDSINTOWN_APP_ID` (opcional), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Ajustes rapidos (topo do passo2_buscar_shows.py)
- `TOP_N`: quantos artistas mais ouvidos processar.
- `MAX_RESULTADOS_POR_ARTISTA`: mais resultados = mais recall, porem mais lento.
- `DIAS_MAX_FUTURO`: janela maxima de antecedencia aceita (~18 meses).
- `PAUSA_BUSCA` / `PAUSA_IA`: pausas para evitar rate limit.
