"""
Listas de referência (whitelist) usadas em todo o sistema para validar e
filtrar ligas e tipos de mercado aceitos.

Regra de produto: o gerador de múltiplas SÓ pode usar odds cujo `league`
esteja em `WHITELIST_LEAGUES` e cujo `market_type` esteja em
`MARKET_TYPE_KEYS`. Isso mantém a curadoria de mercados "mais previsíveis"
(menos gols, dupla chance no favorito, etc.) e evita que qualquer odd
aleatória entre na composição de uma múltipla "segura".
"""

WHITELIST_LEAGUES = [
    "Brasileirão Série A",
    "Champions League",
    "Libertadores",
    "Premier League",
    "La Liga",
    "Serie A",
    "Bundesliga",
]

MARKET_TYPES = {
    "under_goals": "Menos de X.5 gols na partida",
    "double_chance_favorite": "Dupla chance no favorito claro",
    "under_corners": "Menos de X.5 cantos na partida",
    "under_cards": "Menos de X.5 cartões na partida",
    "btts_no": "Ambas marcam - Não",
    "team_not_lose": "Time não perde (dupla chance / empate anula)",
}

MARKET_TYPE_KEYS = list(MARKET_TYPES.keys())
