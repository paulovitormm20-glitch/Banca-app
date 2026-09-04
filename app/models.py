"""
Modelos SQLAlchemy do Gestor de Banca.

Regras de produto que estes modelos existem para sustentar (ver DESIGN.md):
- Nunca há uma "% de segurança" inventada: a probabilidade de cada perna é
  sempre `1 / odds_decimal` (campo `BetLeg.implied_probability`), e a
  probabilidade da múltipla é o produto dessas probabilidades
  (campo `Bet.combined_probability`). Nenhum outro número de "confiança" é
  armazenado ou exibido.
- O sistema nunca aposta sozinho: `Bet` representa apenas a intenção
  registrada pelo usuário depois que ele já apostou manualmente na casa de
  apostas (ver comentário em `app/routers/generator.py` e `history.py`).
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.database import Base


class Bankroll(Base):
    __tablename__ = "bankroll"

    id = Column(Integer, primary_key=True)
    initial_amount = Column(Float, nullable=False)
    current_amount = Column(Float, nullable=False)
    unit_percent = Column(Float, nullable=False, default=3.0)  # % da banca por unidade de aposta
    stop_daily_percent = Column(Float, nullable=True)  # ex.: 10.0 = para se perder 10% no dia
    stop_weekly_percent = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    logs = relationship("BankrollLog", back_populates="bankroll")


class BankrollLog(Base):
    __tablename__ = "bankroll_log"

    id = Column(Integer, primary_key=True)
    bankroll_id = Column(Integer, ForeignKey("bankroll.id"))
    change_amount = Column(Float, nullable=False)  # positivo (ganho/depósito) ou negativo (perda/saque)
    balance_after = Column(Float, nullable=False)
    reason = Column(String, nullable=False)  # "initial" | "bet_won" | "bet_lost" | "bet_void" | "manual_adjustment"
    bet_id = Column(Integer, ForeignKey("bets.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    bankroll = relationship("Bankroll", back_populates="logs")


class OddsCacheEntry(Base):
    __tablename__ = "odds_cache"

    id = Column(Integer, primary_key=True)
    league = Column(String, nullable=False)  # deve bater com um valor em WHITELIST_LEAGUES
    event_name = Column(String, nullable=False)  # "Time A vs Time B"
    market_type = Column(String, nullable=False)  # chave de MARKET_TYPES
    selection_description = Column(String, nullable=False)  # texto humano, ex. "Menos de 2.5 gols"
    odds_decimal = Column(Float, nullable=False)
    source = Column(String, nullable=False, default="manual")  # "manual" | "api"
    event_start = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)

    # Rastreio de movimento: quando a mesma perna (liga+evento+mercado+seleção)
    # é atualizada de novo (via refresh da API ou reentrada manual), guardamos
    # o valor anterior e a direção do movimento, em vez de duplicar a linha.
    # "down" = odd caiu (probabilidade implícita subiu); "up" = odd subiu
    # (probabilidade caiu); "same" = não mudou. None = nunca foi atualizada.
    previous_odds_decimal = Column(Float, nullable=True)
    movement = Column(String, nullable=True)  # "down" | "up" | "same" | None
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CompoundCycle(Base):
    """Um "ciclo de juros compostos": alternativa ao Gerador de Múltiplas de
    odd-alvo (25x/50x/100x). Em vez de uma única múltipla gigante tentando
    bater um multiplicador de odd de uma vez, um ciclo é uma SEQUÊNCIA de
    apostas simples (1 perna, odd baixa) onde o saldo TOTAL da banca é
    reinvestido a cada rodada vencida ("deixa rolar"), até acumular um
    lucro-alvo em reais (`target_profit`) ou perder uma rodada — já que
    100% do saldo está em jogo a cada rodada, uma perda encerra o ciclo.
    """
    __tablename__ = "compound_cycles"

    id = Column(Integer, primary_key=True)
    target_profit = Column(Float, nullable=False)  # lucro-alvo em R$ (ex.: 25, 50, 100)
    starting_stake = Column(Float, nullable=False)  # saldo da banca quando o ciclo começou
    status = Column(String, nullable=False, default="active")  # "active" | "completed" | "busted" | "cashed_out"
    created_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)

    rounds = relationship("Bet", back_populates="cycle")


class Bet(Base):
    __tablename__ = "bets"

    id = Column(Integer, primary_key=True)
    # None para rodadas de um CompoundCycle (não fazem parte do Gerador de
    # tier de odd-alvo — ver `cycle_id`/`cycle_round` abaixo).
    tier = Column(Integer, nullable=True)  # 25, 50 ou 100
    stake = Column(Float, nullable=False)
    total_odds = Column(Float, nullable=False)
    combined_probability = Column(Float, nullable=False)  # 0..1, produto das probabilidades das pernas
    potential_return = Column(Float, nullable=False)  # stake * total_odds
    status = Column(String, nullable=False, default="pending")  # "pending" | "won" | "lost" | "void"
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)

    # Preenchidos só quando este `Bet` é uma rodada de um CompoundCycle (uma
    # aposta simples, 1 perna) — None para múltiplas do Gerador normal.
    cycle_id = Column(Integer, ForeignKey("compound_cycles.id"), nullable=True)
    cycle_round = Column(Integer, nullable=True)  # 1, 2, 3... dentro do ciclo

    legs = relationship("BetLeg", back_populates="bet")
    cycle = relationship("CompoundCycle", back_populates="rounds")


class BetLeg(Base):
    __tablename__ = "bet_legs"

    id = Column(Integer, primary_key=True)
    bet_id = Column(Integer, ForeignKey("bets.id"), nullable=False)
    league = Column(String, nullable=False)
    event_name = Column(String, nullable=False)
    market_type = Column(String, nullable=False)
    selection_description = Column(String, nullable=False)
    odds_decimal = Column(Float, nullable=False)
    implied_probability = Column(Float, nullable=False)  # 1 / odds_decimal
    result = Column(String, nullable=False, default="pending")  # "pending" | "won" | "lost" | "void"
    event_start = Column(DateTime, nullable=True)  # data/hora do jogo, copiada da OddsCacheEntry na confirmação

    bet = relationship("Bet", back_populates="legs")
