"""
Serviço de odds: leitura/escrita do cache manual (com whitelist) e
integração opcional com a The Odds API.

Regra de produto (não-negociável, ver DESIGN.md): NUNCA existe uma "% de
segurança" fixa ou inventada em lugar nenhum do sistema. A única
probabilidade usada em qualquer tela é `1 / odds_decimal` por perna
(probabilidade implícita), calculada a partir da odd real. Este módulo só
cuida da odd em si (`odds_decimal`) e da curadoria de ligas/mercados
permitidos; o cálculo de probabilidade combinada é feito por quem consome
esses dados (ver `app.services.generator_service`).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests
from sqlalchemy.orm import Session

from app.data.markets_whitelist import MARKET_TYPE_KEYS, WHITELIST_LEAGUES
from app.models import OddsCacheEntry
from app.schemas import OddsManualCreate

logger = logging.getLogger(__name__)

# Mapeamento liga (nome usado neste sistema) -> sport_key da The Odds API.
LEAGUE_TO_SPORT_KEY = {
    "Premier League": "soccer_epl",
    "La Liga": "soccer_spain_la_liga",
    "Serie A": "soccer_italy_serie_a",
    "Bundesliga": "soccer_germany_bundesliga",
    "Champions League": "soccer_uefa_champs_league",
    "Brasileirão Série A": "soccer_brazil_campeonato",
    "Libertadores": "soccer_conmebol_copa_libertadores",
}

ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
EVENT_ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds"
SPORTS_URL = "https://api.the-odds-api.com/v4/sports"

# Mercados "adicionais" da The Odds API só existem no endpoint por-jogo
# (bem mais caro em créditos que o endpoint em lote — ver
# `refresh_cache_from_api`), então só buscamos para os N jogos mais
# próximos de cada atualização, não para todos de uma vez.
EXTRA_MARKETS_PER_REFRESH = 8
EXTRA_MARKETS = "btts,double_chance,alternate_totals_corners,alternate_totals_cards"


class OddsApiNotConfigured(Exception):
    """Levantada quando `fetch_from_odds_api` é chamada sem `ODDS_API_KEY`
    definida no ambiente. Não é um erro grave: o sistema funciona 100% com
    entrada manual de odds sem essa variável configurada."""


def _parse_commence_time(raw: str | None) -> datetime | None:
    """Converte o `commence_time` (ISO 8601 com "Z") devolvido pela API para
    um `datetime`. Retorna `None` se `raw` for vazio ou inválido."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def get_remaining_credits(api_key: str) -> int | None:
    """Consulta quantos créditos ainda restam no plano da The Odds API.

    Usa o endpoint `GET /v4/sports`, que a documentação oficial afirma
    explicitamente NÃO contar no limite de uso — essa checagem é grátis.
    Retorna `None` se a consulta falhar por qualquer motivo (rede, chave
    inválida) — isso é só informativo para o usuário, nunca deve quebrar um
    refresh que já funcionou."""
    try:
        response = requests.get(SPORTS_URL, params={"apiKey": api_key}, timeout=10)
        return int(response.headers.get("x-requests-remaining"))
    except Exception:  # noqa: BLE001 - puramente informativo, nunca crítico
        return None


def get_cached_odds(
    db: Session,
    leagues: list[str] | None = None,
    event_from: datetime | None = None,
    event_to: datetime | None = None,
) -> list[OddsCacheEntry]:
    """
    Retorna entradas de `odds_cache` cujo `league` esteja na whitelist do
    sistema (`WHITELIST_LEAGUES`) — e também em `leagues`, se essa lista for
    informada — e cujo `market_type` esteja entre os mercados curados
    (`MARKET_TYPE_KEYS`).

    Se `event_from`/`event_to` forem informados, mantém só as pernas cujo
    `event_start` caia nesse intervalo (`event_from <= event_start <
    event_to`) — usado para filtrar por "dia do jogo" (hoje, amanhã, um dia
    específico). Pernas sem `event_start` definido são excluídas quando esse
    filtro está ativo, já que não dá pra confirmar em que dia elas caem. O
    filtro é feito em Python (não em SQL) de propósito: o volume de odds em
    cache de um usuário único é pequeno, e comparar instantes já resolvidos
    (em vez de "datas" soltas) evita qualquer ambiguidade de fuso horário
    entre o que o navegador do usuário considera "hoje" e o que fica
    armazenado no banco.

    Ordenadas por `odds_decimal` ASC: menor odd primeiro, ou seja, maior
    probabilidade implícita (1 / odds_decimal) primeiro. O gerador de
    múltiplas (`generator_service.generate_combo`) depende dessa ordenação
    para escolher gulosamente as pernas mais prováveis primeiro.
    """
    allowed_leagues = set(WHITELIST_LEAGUES)
    if leagues:
        allowed_leagues &= set(leagues)

    if not allowed_leagues:
        return []

    entries = (
        db.query(OddsCacheEntry)
        .filter(OddsCacheEntry.league.in_(allowed_leagues))
        .filter(OddsCacheEntry.market_type.in_(MARKET_TYPE_KEYS))
        .order_by(OddsCacheEntry.odds_decimal.asc())
        .all()
    )

    if event_from is not None and event_to is not None:
        # `event_from`/`event_to` chegam do FastAPI/Pydantic como datetimes
        # "aware" (o front sempre manda com sufixo "Z"), mas `event_start`
        # salvo no banco é "naive" (SQLite não guarda tzinfo) — comparar os
        # dois direto levanta TypeError. Como o "Z" já garante que ambos os
        # lados representam o mesmo instante em UTC, basta descartar o
        # tzinfo depois de normalizar para UTC.
        naive_from = event_from.astimezone(timezone.utc).replace(tzinfo=None) if event_from.tzinfo else event_from
        naive_to = event_to.astimezone(timezone.utc).replace(tzinfo=None) if event_to.tzinfo else event_to
        entries = [
            e for e in entries
            if e.event_start is not None and naive_from <= e.event_start < naive_to
        ]

    return entries


def _upsert_odds_entry(
    db: Session,
    *,
    league: str,
    event_name: str,
    market_type: str,
    selection_description: str,
    odds_decimal: float,
    source: str,
    event_start: datetime | None = None,
    commit: bool = True,
) -> tuple[OddsCacheEntry, bool]:
    """
    Cria ou atualiza uma entrada de `odds_cache`, identificada pela chave
    (league, event_name, market_type, selection_description).

    Isso é o que permite mostrar "odds ao vivo com movimento" no painel: em
    vez de duplicar uma linha toda vez que o usuário reconsulta a mesma perna
    (manualmente ou via `/odds/refresh`), a linha existente é atualizada e o
    valor antigo vira `previous_odds_decimal`, com `movement` calculado por
    comparação direta das duas odds decimais — nunca um palpite ou uma
    "confiança" inventada, só aritmética sobre valores reais.

    Retorna `(entry, created)`, onde `created=True` significa que era uma
    perna nova (primeira vez que essa combinação aparece no cache).

    Levanta `ValueError` se `odds_decimal` for menor que 1.0 — uma odd
    decimal abaixo de 1.0 é matematicamente impossível (nenhuma casa de
    apostas oferece isso; implicaria probabilidade acima de 100%). Esta é a
    última barreira antes de qualquer odd chegar ao banco, venha ela de
    entrada manual, do endpoint em lote ou de um mercado derivado — é o que
    impede um erro de cálculo em qualquer uma dessas origens de virar um
    número exibido como se fosse real.
    """
    if odds_decimal < 1.0:
        raise ValueError(
            f"Odd inválida ({odds_decimal:.4f}): uma odd decimal nunca pode ser menor que 1.0."
        )

    existing = (
        db.query(OddsCacheEntry)
        .filter_by(
            league=league,
            event_name=event_name,
            market_type=market_type,
            selection_description=selection_description,
        )
        .first()
    )

    if existing is not None:
        old_odds = existing.odds_decimal
        if odds_decimal < old_odds:
            movement = "down"
        elif odds_decimal > old_odds:
            movement = "up"
        else:
            movement = "same"
        existing.previous_odds_decimal = old_odds
        existing.movement = movement
        existing.odds_decimal = odds_decimal
        existing.source = source
        if event_start is not None:
            existing.event_start = event_start
        existing.fetched_at = datetime.utcnow()
        entry, created = existing, False
    else:
        entry = OddsCacheEntry(
            league=league,
            event_name=event_name,
            market_type=market_type,
            selection_description=selection_description,
            odds_decimal=odds_decimal,
            source=source,
            event_start=event_start,
            previous_odds_decimal=None,
            movement=None,
        )
        db.add(entry)
        created = True

    # `SessionLocal` é configurada com autoflush=False (ver app/database.py),
    # então sem este flush explícito, chamadas seguidas desta função dentro
    # da MESMA transação (como o loop de `refresh_cache_from_api`, que só
    # comita no final) não enxergam as linhas já adicionadas por chamadas
    # anteriores — cada casa de apostas oferecendo a mesma perna criaria uma
    # linha nova em vez de atualizar a existente. O flush resolve isso sem
    # precisar commitar a cada chamada.
    db.flush()
    if commit:
        db.commit()
        db.refresh(entry)

    return entry, created


def add_manual_odds(db: Session, entry: OddsManualCreate) -> OddsCacheEntry:
    """
    Valida e persiste uma odd inserida manualmente pelo usuário (coletada à
    mão em Betano/Bet365/etc. e digitada no /generator-page ou /odds).

    NUNCA acessa nenhuma casa de apostas nem faz login em lugar nenhum — é
    apenas o registro, feito pelo próprio usuário, de uma odd que ele viu. Se
    o usuário reconsultar a mesma perna (mesma liga+evento+mercado+seleção) e
    reenviar com uma odd diferente, a linha existente é atualizada (não
    duplicada) e o movimento (subiu/caiu/igual) fica registrado — é assim que
    o usuário consegue acompanhar "odds ao vivo" mesmo sem `ODDS_API_KEY`
    configurada.

    Levanta `ValueError` (mensagem clara) se `league` não estiver em
    `WHITELIST_LEAGUES` ou `market_type` não estiver em `MARKET_TYPE_KEYS`.
    """
    if entry.league not in WHITELIST_LEAGUES:
        raise ValueError(
            f"Liga '{entry.league}' não está na whitelist do sistema. "
            f"Ligas permitidas: {', '.join(WHITELIST_LEAGUES)}."
        )
    if entry.market_type not in MARKET_TYPE_KEYS:
        raise ValueError(
            f"Tipo de mercado '{entry.market_type}' não é reconhecido. "
            f"Tipos permitidos: {', '.join(MARKET_TYPE_KEYS)}."
        )

    db_entry, _created = _upsert_odds_entry(
        db,
        league=entry.league,
        event_name=entry.event_name,
        market_type=entry.market_type,
        selection_description=entry.selection_description,
        odds_decimal=entry.odds_decimal,
        source="manual",
        event_start=entry.event_start,
    )
    return db_entry


def fetch_from_odds_api(leagues: list[str]) -> list[dict]:
    """
    Busca odds cruas na The Odds API (https://the-odds-api.com/) para cada
    liga informada, uma requisição por liga.

    Só deve ser chamada se a env var `ODDS_API_KEY` estiver definida; caso
    contrário levanta `OddsApiNotConfigured` (quem chamou — normalmente
    `refresh_cache_from_api` — decide a mensagem amigável para o usuário).
    Se estiver definida, faz `requests.get` em
    `https://api.the-odds-api.com/v4/sports/{sport_key}/odds` usando
    `LEAGUE_TO_SPORT_KEY` para mapear liga -> sport_key, com params
    `apiKey`, `regions=eu`, `markets=h2h,totals`.

    Cada liga é envolvida em seu próprio try/except: se a requisição falhar
    (rede, chave inválida, liga sem mercado disponível no plano atual) ou a
    liga não tiver `sport_key` mapeado, isso é logado e a liga é pulada —
    NUNCA quebra a função inteira. Retorna a lista de dicts crus devolvidos
    pela API (cada dict recebe uma chave extra `_league` com o nome da liga
    no nosso sistema, para facilitar o parsing em `refresh_cache_from_api`).

    COBERTURA DE MERCADOS (ver `refresh_cache_from_api` para o parsing):
    - "under_goals": direto do mercado `totals` (outcome "Under").
    - "double_chance_favorite": NÃO existe pronto na API — é DERIVADO a
      partir do mercado `h2h` (1x2), combinando a odd do lado favorito
      (menor odd entre mandante/visitante) com a odd do empate via a mesma
      aritmética de probabilidade implícita usada no resto do sistema
      (1/odd por perna, depois invertendo a soma) — não é um número
      inventado, é derivado das odds reais devolvidas pela casa.
    - "btts_no": tentado via mercado `btts` (outcome "No"), mas a
      disponibilidade desse mercado específico varia por plano/região da
      API e por casa de apostas — pode simplesmente não vir na resposta.
    - "under_corners" e "team_not_lose": SEM cobertura automática. Cantos
      não são oferecidos por nenhuma API de odds gratuita/de baixo custo de
      forma confiável (é um mercado de nicho, normalmente só em provedores
      pagos especializados) — continuam dependendo de entrada manual.
    """
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        raise OddsApiNotConfigured(
            "ODDS_API_KEY não está definida no ambiente. Configure-a no "
            "arquivo .env para usar a integração com a The Odds API, ou "
            "continue usando a entrada manual de odds em /odds."
        )

    raw_events: list[dict] = []
    for league in leagues:
        sport_key = LEAGUE_TO_SPORT_KEY.get(league)
        if sport_key is None:
            logger.warning(
                "Liga '%s' não tem sport_key mapeado para a The Odds API — pulando.",
                league,
            )
            continue
        try:
            url = ODDS_API_BASE_URL.format(sport_key=sport_key)
            response = requests.get(
                url,
                params={
                    "apiKey": api_key,
                    "regions": "eu",
                    "markets": "h2h,totals",
                },
                timeout=10,
            )
            response.raise_for_status()
            events = response.json()
            if isinstance(events, list):
                for event in events:
                    if isinstance(event, dict):
                        event["_league"] = league
                raw_events.extend(events)
        except Exception:  # noqa: BLE001 - uma liga com problema nunca derruba as outras
            logger.exception(
                "Falha ao buscar odds da The Odds API para a liga '%s' "
                "(rede, chave inválida ou mercado indisponível no plano "
                "atual). Pulando essa liga.",
                league,
            )
            continue

    return raw_events


def _record_api_odds(
    db: Session,
    summary: dict,
    *,
    league: str,
    event_name: str,
    market_type: str,
    selection_description: str,
    odds_decimal: float,
    event_start: datetime | None,
) -> None:
    """Faz upsert de uma perna vinda da API e atualiza o resumo de
    novas/atualizadas/movimento — pequeno helper pra não repetir essa
    contabilidade em cada um dos três mercados tratados abaixo.

    Se `odds_decimal` for inválida (< 1.0 — ver `_upsert_odds_entry`), loga e
    ignora só essa perna, sem derrubar o resto do refresh."""
    try:
        entry, created = _upsert_odds_entry(
            db,
            league=league,
            event_name=event_name,
            market_type=market_type,
            selection_description=selection_description,
            odds_decimal=odds_decimal,
            source="api",
            event_start=event_start,
            commit=False,
        )
    except ValueError:
        logger.warning(
            "Odd inválida (%.4f) ignorada para '%s' / %s / %s.",
            odds_decimal, event_name, market_type, selection_description,
        )
        return
    if created:
        summary["new"] += 1
    else:
        summary["updated"] += 1
        summary[entry.movement] += 1


def _apply_extra_markets(
    event: dict,
    api_key: str,
    keep_best_fn,
) -> None:
    """
    Busca os mercados "adicionais" (`EXTRA_MARKETS`: ambas marcam, dupla
    chance real, cantos e cartões) para UM evento específico, via
    `EVENT_ODDS_URL` — o endpoint por-jogo, bem mais caro em créditos que o
    endpoint em lote (cada chamada custa ~1 crédito por mercado retornado).
    Por isso só é chamada para os `EXTRA_MARKETS_PER_REFRESH` jogos mais
    próximos (ver `refresh_cache_from_api`), nunca para todos de uma vez.

    Envolvida em try/except: falha de rede ou evento sem mercados extras
    disponíveis é logada e ignorada, nunca derruba o restante do refresh.
    """
    sport_key = event.get("sport_key")
    event_id = event.get("id")
    league = event.get("_league")
    home_team = event.get("home_team")
    away_team = event.get("away_team")
    if not (sport_key and event_id and home_team and away_team):
        return
    event_name = f"{home_team} vs {away_team}"
    event_start = _parse_commence_time(event.get("commence_time"))

    try:
        response = requests.get(
            EVENT_ODDS_URL.format(sport_key=sport_key, event_id=event_id),
            params={"apiKey": api_key, "regions": "eu", "markets": EXTRA_MARKETS},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:  # noqa: BLE001 - um evento com problema nunca derruba os outros
        logger.exception(
            "Falha ao buscar mercados extras (cantos/cartões/ambas marcam) "
            "para o evento '%s' — pulando.",
            event_name,
        )
        return

    for bookmaker in data.get("bookmakers") or []:
        markets_by_key = {m.get("key"): m for m in (bookmaker.get("markets") or [])}

        btts_market = markets_by_key.get("btts")
        if btts_market:
            for outcome in btts_market.get("outcomes") or []:
                if outcome.get("name") != "No":
                    continue
                price = outcome.get("price")
                if price is not None:
                    keep_best_fn(
                        (league, event_name, "btts_no", "Ambas marcam - Não"),
                        float(price), event_start,
                    )

        # Mercado "double_chance" de verdade (não a aproximação via h2h) —
        # quando disponível para este jogo, a melhor odd entre esta e a
        # derivada do h2h vence naturalmente (`keep_best_fn` sempre guarda o
        # menor valor visto).
        dc_market = markets_by_key.get("double_chance")
        if dc_market:
            home_dc = away_dc = None
            for outcome in dc_market.get("outcomes") or []:
                name, price = outcome.get("name"), outcome.get("price")
                if name == f"{home_team} or Draw":
                    home_dc = price
                elif name == f"{away_team} or Draw":
                    away_dc = price
            if home_dc and away_dc:
                if home_dc <= away_dc:
                    price, label = home_dc, home_team
                else:
                    price, label = away_dc, away_team
                keep_best_fn(
                    (league, event_name, "double_chance_favorite", f"{label} ou empate"),
                    float(price), event_start,
                )

        corners_market = markets_by_key.get("alternate_totals_corners")
        if corners_market:
            for outcome in corners_market.get("outcomes") or []:
                if outcome.get("name") != "Under":
                    continue
                point, price = outcome.get("point"), outcome.get("price")
                if point is not None and price is not None:
                    keep_best_fn(
                        (league, event_name, "under_corners", f"Menos de {point} cantos na partida"),
                        float(price), event_start,
                    )

        cards_market = markets_by_key.get("alternate_totals_cards")
        if cards_market:
            for outcome in cards_market.get("outcomes") or []:
                if outcome.get("name") != "Under":
                    continue
                point, price = outcome.get("point"), outcome.get("price")
                if point is not None and price is not None:
                    keep_best_fn(
                        (league, event_name, "under_cards", f"Menos de {point} cartões na partida"),
                        float(price), event_start,
                    )


def refresh_cache_from_api(db: Session, leagues: list[str] | None = None) -> dict:
    """
    Atualiza o cache de odds a partir da The Odds API, quando configurada.

    Duas fases, por causa do custo de créditos:

    1. BARATA (endpoint em lote, `fetch_from_odds_api`, mercados
       `h2h,totals`): para TODOS os eventos das ligas pedidas, extrai
       "under_goals" direto de `totals`, e DERIVA "double_chance_favorite"
       do `h2h` (menor odd entre mandante/visitante, combinada com a odd do
       empate via `1 / (1/odd_favorito + 1/odd_empate)` — a mesma
       aritmética de probabilidade implícita usada no resto do sistema, não
       um valor inventado).
    2. CARA (endpoint por-jogo, `_apply_extra_markets`, ~1 crédito por
       mercado por jogo): só para os `EXTRA_MARKETS_PER_REFRESH` jogos mais
       próximos no tempo (não todos — buscar cantos/cartões/ambas marcam
       para uma liga inteira de uma vez consumiria a maior parte da cota
       mensal gratuita numa única atualização). Traz "btts_no",
       "under_corners", "under_cards" e uma versão real (não derivada) de
       "double_chance_favorite".

    "team_not_lose" continua sem cobertura automática — só via entrada
    manual.

    IMPORTANTE: um mesmo evento normalmente vem com várias casas de apostas
    oferecendo a mesma perna a preços diferentes. Em vez de gravar "a última
    casa que a API listou" (arbitrário), esta função agrega e guarda a
    MELHOR odd (menor valor = maior probabilidade implícita) encontrada
    entre todas as casas e ambas as fases, antes de gravar no banco.

    Cada perna (já com a melhor odd escolhida) é gravada via
    `_upsert_odds_entry` (`source="api"`): se já existia no cache de uma
    atualização anterior, a linha é atualizada e o movimento (odd caiu/
    subiu/igual) é calculado, em vez de criar uma linha duplicada.

    Retorna um resumo `{"new": int, "updated": int, "down": int, "up": int,
    "same": int, "requests_remaining": int|None}` para alimentar o
    indicador de "odds ao vivo" do painel e deixar claro quanto sobrou da
    cota mensal.

    Se `ODDS_API_KEY` não estiver configurada, retorna o resumo zerado
    silenciosamente (sem levantar exceção) — o endpoint `/odds/refresh` é
    quem decide a mensagem amigável a mostrar ao usuário nesse caso.
    """
    summary = {"new": 0, "updated": 0, "down": 0, "up": 0, "same": 0, "requests_remaining": None}
    target_leagues = leagues if leagues else list(WHITELIST_LEAGUES)
    api_key = os.environ.get("ODDS_API_KEY")

    try:
        raw_events = fetch_from_odds_api(target_leagues)
    except OddsApiNotConfigured:
        return summary

    # key = (league, event_name, market_type, selection_description)
    best: dict[tuple[str, str, str, str], dict] = {}

    def _keep_best(key: tuple[str, str, str, str], price: float, event_start: datetime | None) -> None:
        current = best.get(key)
        if current is None or price < current["price"]:
            best[key] = {"price": price, "event_start": event_start}

    for event in raw_events:
        league = event.get("_league")
        if league not in WHITELIST_LEAGUES:
            continue

        home_team = event.get("home_team")
        away_team = event.get("away_team")
        if not home_team or not away_team:
            continue
        event_name = f"{home_team} vs {away_team}"
        event_start = _parse_commence_time(event.get("commence_time"))

        for bookmaker in event.get("bookmakers") or []:
            markets_by_key = {m.get("key"): m for m in (bookmaker.get("markets") or [])}

            totals_market = markets_by_key.get("totals")
            if totals_market:
                for outcome in totals_market.get("outcomes") or []:
                    if outcome.get("name") != "Under":
                        continue
                    point = outcome.get("point")
                    price = outcome.get("price")
                    if point is None or price is None:
                        continue
                    key = (league, event_name, "under_goals", f"Menos de {point} gols na partida")
                    _keep_best(key, float(price), event_start)

            h2h_market = markets_by_key.get("h2h")
            if h2h_market:
                home_price = draw_price = away_price = None
                for outcome in h2h_market.get("outcomes") or []:
                    name, price = outcome.get("name"), outcome.get("price")
                    if name == home_team:
                        home_price = price
                    elif name == away_team:
                        away_price = price
                    elif name == "Draw":
                        draw_price = price
                if home_price and draw_price and away_price:
                    if home_price <= away_price:
                        favorite_price, favorite_label = home_price, home_team
                    else:
                        favorite_price, favorite_label = away_price, away_team
                    # Somar direto (1/odd_favorito + 1/odd_empate) pode gerar
                    # uma odd combinada MENOR que 1.0 (impossível — nenhuma
                    # casa oferece isso) quando o favorito é muito forte: a
                    # margem da casa (overround) fica concentrada nesses dois
                    # resultados em vez de distribuída nos três. A correção é
                    # normalizar pelas probabilidades implícitas dos TRÊS
                    # resultados (removendo a margem proporcionalmente) antes
                    # de somar só os dois que interessam — assim a odd
                    # combinada nunca fica abaixo de 1.0.
                    raw_home = 1.0 / home_price
                    raw_draw = 1.0 / draw_price
                    raw_away = 1.0 / away_price
                    total_implied = raw_home + raw_draw + raw_away
                    if total_implied > 0:
                        favorite_raw = raw_home if favorite_price == home_price else raw_away
                        fair_combined_prob = (favorite_raw + raw_draw) / total_implied
                        if 0 < fair_combined_prob < 1:
                            key = (league, event_name, "double_chance_favorite", f"{favorite_label} ou empate")
                            _keep_best(key, 1.0 / fair_combined_prob, event_start)

    if api_key:
        upcoming = [
            e for e in raw_events
            if e.get("_league") in WHITELIST_LEAGUES and e.get("commence_time")
        ]
        upcoming.sort(key=lambda e: e["commence_time"])
        for event in upcoming[:EXTRA_MARKETS_PER_REFRESH]:
            _apply_extra_markets(event, api_key, _keep_best)

    touched = False
    for (league, event_name, market_type, selection_description), data in best.items():
        _record_api_odds(
            db, summary,
            league=league, event_name=event_name,
            market_type=market_type, selection_description=selection_description,
            odds_decimal=data["price"], event_start=data["event_start"],
        )
        touched = True

    if touched:
        db.commit()

    if api_key:
        summary["requests_remaining"] = get_remaining_credits(api_key)

    return summary
