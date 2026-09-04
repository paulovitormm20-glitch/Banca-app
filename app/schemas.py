"""
Schemas Pydantic v2 do Gestor de Banca.

Espelham os modelos SQLAlchemy em `app.models`. Schemas de leitura (que serão
construídos a partir de instâncias ORM) usam `model_config =
ConfigDict(from_attributes=True)`.

Lembrete de regra de produto: nenhum schema carrega um campo de "% de
segurança" inventado — a única probabilidade que existe é
`implied_probability` (1 / odds_decimal) por perna e `combined_probability`
(produto das pernas) na múltipla.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Bankroll
# ---------------------------------------------------------------------------


class BankrollInit(BaseModel):
    initial_amount: float
    unit_percent: float = 3.0
    stop_daily_percent: float | None = None
    stop_weekly_percent: float | None = None


class BankrollAdjust(BaseModel):
    amount: float
    note: str


class BankrollOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    initial_amount: float
    current_amount: float
    unit_percent: float
    stop_daily_percent: float | None
    stop_weekly_percent: float | None
    created_at: datetime
    profit_loss: float  # current_amount - initial_amount
    roi_percent: float  # (current_amount - initial_amount) / initial_amount * 100


class BankrollStats(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    win_rate_overall: float  # 0..100
    win_rate_by_tier: dict[str, float]  # chaves "25","50","100"
    max_losing_streak: int
    total_bets: int
    settled_bets: int


class BankrollLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    change_amount: float
    balance_after: float
    reason: str
    bet_id: int | None
    created_at: datetime


class StopStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    daily_loss_percent: float
    weekly_loss_percent: float
    daily_limit_hit: bool
    weekly_limit_hit: bool
    stop_daily_percent: float | None
    stop_weekly_percent: float | None


# ---------------------------------------------------------------------------
# Odds
# ---------------------------------------------------------------------------


class OddsManualCreate(BaseModel):
    league: str
    event_name: str
    market_type: str
    selection_description: str
    odds_decimal: float
    event_start: datetime | None = None


class OddsCacheOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    league: str
    event_name: str
    market_type: str
    selection_description: str
    odds_decimal: float
    source: str
    event_start: datetime | None
    fetched_at: datetime
    previous_odds_decimal: float | None
    movement: str | None  # "down" | "up" | "same" | None (nunca atualizada)
    updated_at: datetime


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class GeneratorRequest(BaseModel):
    tier: int  # deve ser 25, 50 ou 100
    stake: float
    # Filtro opcional de "dia do jogo": um intervalo de instantes (calculado no
    # front a partir do dia de calendário LOCAL escolhido pelo usuário — meia-
    # noite local até a meia-noite local seguinte, já convertido para o mesmo
    # referencial usado ao salvar `event_start`). Isso evita ambiguidade de
    # fuso: em vez de comparar "datas" soltas, comparamos instantes reais.
    # Se ambos vierem None, o gerador considera pernas de qualquer dia (ou
    # sem dia definido) — comportamento padrão, sem filtro.
    event_date_from: datetime | None = None
    event_date_to: datetime | None = None


class GeneratedLegOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    league: str
    event_name: str
    market_type: str
    selection_description: str
    odds_decimal: float
    implied_probability: float
    event_start: datetime | None = None


class GeneratedComboOut(BaseModel):
    """Resposta do preview do gerador. NÃO é salva no banco nesse momento —
    é apenas o que será exibido ao usuário e, se ele confirmar, reenviado
    como `GeneratorConfirm` para persistência."""

    model_config = ConfigDict(from_attributes=True)

    tier: int
    stake: float
    legs: list[GeneratedLegOut]
    total_odds: float
    combined_probability: float
    potential_return: float


class GeneratorConfirm(BaseModel):
    """Mesmo shape de `GeneratedComboOut` — o front reenvia o preview exato
    que o usuário decidiu apostar, para ser persistido como `Bet` + `BetLeg`s."""

    tier: int
    stake: float
    legs: list[GeneratedLegOut]
    total_odds: float
    combined_probability: float
    potential_return: float


# ---------------------------------------------------------------------------
# Bets / história
# ---------------------------------------------------------------------------


class BetLegOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bet_id: int
    league: str
    event_name: str
    market_type: str
    selection_description: str
    odds_decimal: float
    implied_probability: float
    result: str
    event_start: datetime | None


class BetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tier: int | None  # None para rodadas de CompoundCycle (ver cycle_id/cycle_round)
    stake: float
    total_odds: float
    combined_probability: float
    potential_return: float
    status: str
    created_at: datetime
    settled_at: datetime | None
    legs: list[BetLegOut]
    cycle_id: int | None = None
    cycle_round: int | None = None


# ---------------------------------------------------------------------------
# Ciclo de juros compostos
# ---------------------------------------------------------------------------


class CycleStartRequest(BaseModel):
    target_profit: float  # lucro-alvo em R$ (ex.: 25, 50, 100 ou um valor livre)


class CycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    target_profit: float
    starting_stake: float
    status: str  # "active" | "completed" | "busted" | "cashed_out"
    created_at: datetime
    ended_at: datetime | None


class CycleRoundCreate(BaseModel):
    """Mesmo shape de uma perna escolhida do cache de odds (`OddsCacheOut`) —
    o front reenvia a perna exata que o usuário escolheu para a rodada."""

    league: str
    event_name: str
    market_type: str
    selection_description: str
    odds_decimal: float
    event_start: datetime | None = None


class SettleRequest(BaseModel):
    result: str  # "won" | "lost" | "void"
    leg_results: list[dict] | None = None  # cada item {"leg_id": int, "result": str}


class TierStatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tier: int
    total: int
    settled: int
    wins: int
    win_rate_real: float  # wins/settled*100, 0 se settled=0
    avg_estimated_probability: float  # média de combined_probability das apostas settled desse tier, em %
