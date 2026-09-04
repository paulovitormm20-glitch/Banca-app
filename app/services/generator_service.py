"""
Serviço de geração de múltiplas ("combos").

Regras de produto (não-negociáveis, ver DESIGN.md):
- NUNCA existe uma "% de segurança" fixa ou inventada em lugar nenhum deste
  módulo. A única probabilidade usada é `1 / odds_decimal` por perna
  (probabilidade implícita) e `combined_probability`, que é o PRODUTO das
  probabilidades implícitas das pernas escolhidas.
- Pernas do mesmo `event_name` nunca são combinadas juntas numa mesma
  múltipla (no máximo 1 perna por evento), porque pernas do mesmo jogo são
  estatisticamente correlacionadas e invalidariam o cálculo de probabilidade
  combinada por produto.
- Este serviço APENAS monta um preview em memória (`GeneratedComboOut`). Ele
  NUNCA salva nada no banco e NUNCA aposta em lugar nenhum — persistir a
  intenção do usuário depois que ele já apostou manualmente é responsabilidade
  exclusiva do endpoint `POST /generator/confirm` (`app/routers/generator.py`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import OddsCacheEntry
from app.schemas import GeneratedComboOut, GeneratedLegOut
from app.services.odds_service import get_cached_odds

# Tiers válidos de odd total alvo para uma múltipla.
VALID_TIERS = (25, 50, 100)


class InsufficientOddsError(Exception):
    """Levantada quando o pool de odds em cache (após filtro de whitelist e
    de no-máximo-1-perna-por-evento) não é suficiente para montar uma
    múltipla que atinja a odd total alvo (`tier`)."""


def generate_combo(
    db: Session,
    tier: int,
    stake: float,
    event_from: datetime | None = None,
    event_to: datetime | None = None,
) -> GeneratedComboOut:
    """
    Monta, de forma gulosa, um preview de múltipla ("combo") cuja odd total
    atinja (ou ultrapasse minimamente) o `tier` pedido, usando as odds
    disponíveis no cache (`app.services.odds_service.get_cached_odds`).

    Passo a passo (ver DESIGN.md, seção `app/services/generator_service.py`):
    1. `tier` precisa ser 25, 50 ou 100 — senão `ValueError`.
    2. `legs_pool = get_cached_odds(db)` — já filtrado pela whitelist de
       ligas/mercados e ordenado por `odds_decimal` ASC (menor odd primeiro,
       ou seja, maior probabilidade implícita primeiro).
    3. Seleciona gulosamente as pernas de menor odd primeiro, respeitando no
       máximo 1 leg por `event_name` (pula pernas de um evento já escolhido).
    4. Multiplica `total_odds` acumulando cada leg escolhida; para assim que
       `total_odds >= tier` (não precisa ultrapassar muito o alvo — para no
       primeiro momento em que atingir ou passar o alvo).
    5. Se esgotar o pool inteiro sem atingir o `tier`, levanta
       `InsufficientOddsError`.
    6. `combined_probability` = produto de `(1 / leg.odds_decimal)` de cada
       leg escolhida.
    7. `potential_return` = `stake * total_odds`.
    8. Retorna `GeneratedComboOut` com `tier`, `stake`, `legs`
       (`GeneratedLegOut`), `total_odds`, `combined_probability`,
       `potential_return`.

    NÃO salva nada no banco — é só preview. Salvar é responsabilidade do
    endpoint `/generator/confirm`.
    """
    if tier not in VALID_TIERS:
        raise ValueError(
            f"tier inválido: {tier}. Valores aceitos: {', '.join(str(t) for t in VALID_TIERS)}."
        )

    legs_pool: list[OddsCacheEntry] = get_cached_odds(db, event_from=event_from, event_to=event_to)

    chosen_legs: list[GeneratedLegOut] = []
    used_event_names: set[str] = set()
    total_odds = 1.0
    combined_probability = 1.0

    for entry in legs_pool:
        if entry.event_name in used_event_names:
            # No máximo 1 perna por event_name numa mesma múltipla — pernas
            # do mesmo jogo são correlacionadas e invalidariam o cálculo de
            # probabilidade combinada por produto.
            continue

        used_event_names.add(entry.event_name)
        implied_probability = 1.0 / entry.odds_decimal

        chosen_legs.append(
            GeneratedLegOut(
                league=entry.league,
                event_name=entry.event_name,
                market_type=entry.market_type,
                selection_description=entry.selection_description,
                odds_decimal=entry.odds_decimal,
                implied_probability=implied_probability,
                event_start=entry.event_start,
            )
        )
        total_odds *= entry.odds_decimal
        combined_probability *= implied_probability

        if total_odds >= tier:
            break
    else:
        # Esgotou o pool inteiro (loop terminou sem `break`) sem atingir o
        # tier pedido.
        date_hint = " para o dia selecionado" if (event_from is not None and event_to is not None) else ""
        raise InsufficientOddsError(
            f"Odds insuficientes no cache{date_hint} para montar uma múltipla de {tier}x. "
            "Adicione mais odds manualmente em /odds."
        )

    return GeneratedComboOut(
        tier=tier,
        stake=stake,
        legs=chosen_legs,
        total_odds=total_odds,
        combined_probability=combined_probability,
        potential_return=stake * total_odds,
    )
