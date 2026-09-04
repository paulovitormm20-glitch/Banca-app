"""
Serviço do "Ciclo de Juros Compostos".

Alternativa ao Gerador de Múltiplas de odd-alvo (25x/50x/100x,
`app.services.generator_service`): em vez de uma única múltipla gigante
tentando bater um multiplicador de odd de uma vez (probabilidade real baixa,
ver DESIGN.md e os testes do gerador), um ciclo é uma SEQUÊNCIA de apostas
SIMPLES (1 perna, odd baixa) onde o saldo TOTAL da banca é reinvestido —
"deixa rolar" — a cada rodada vencida, até acumular um lucro-alvo em reais
(`target_profit`) ou até perder uma rodada.

Como 100% do saldo está em jogo em cada rodada, uma única perda encerra o
ciclo inteiro (`status="busted"`) — isso é bem mais arriscado que a unidade
normal (2-3% da banca) usada no Gerador e no ajuste manual, e a interface
precisa deixar isso visível, nunca escondido.

Regra de produto (não-negociável, ver DESIGN.md): exatamente como o resto do
sistema, isso NUNCA aposta sozinho. Cada rodada é registrada como um `Bet`
comum (1 perna) via `create_round`, e o usuário aposta manualmente na casa de
apostas e volta para confirmar o resultado via `POST /bets/{id}/settle`
(endpoint já existente em `app.routers.history`, reaproveitado sem
modificação da lógica de atualização de banca — só ganha uma chamada extra a
`check_cycle_progress` depois de liquidar, para atualizar o status do ciclo).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Bankroll, Bet, BetLeg, CompoundCycle
from app.schemas import CycleRoundCreate


def get_active_cycle(db: Session) -> CompoundCycle | None:
    """A aplicação permite no máximo um ciclo ativo por vez — evita
    ambiguidade sobre qual ciclo "dono" do saldo da banca a cada rodada."""
    return db.query(CompoundCycle).filter(CompoundCycle.status == "active").first()


def start_cycle(db: Session, target_profit: float) -> CompoundCycle:
    """Inicia um novo ciclo, usando o saldo ATUAL da banca como o
    `starting_stake` (a base sobre a qual o lucro-alvo é medido).

    Levanta `ValueError` (mensagem clara) se já existir um ciclo ativo, se a
    banca ainda não tiver sido configurada, se o saldo atual for <= 0, ou se
    `target_profit` não for positivo.
    """
    if target_profit <= 0:
        raise ValueError("O lucro-alvo precisa ser maior que zero.")
    if get_active_cycle(db) is not None:
        raise ValueError(
            "Já existe um ciclo ativo. Encerre-o (ganhando, perdendo, ou "
            "guardando o lucro) antes de iniciar um novo."
        )
    bankroll = db.query(Bankroll).first()
    if bankroll is None:
        raise ValueError("Configure sua banca antes de iniciar um ciclo.")
    if bankroll.current_amount <= 0:
        raise ValueError("O saldo atual da banca precisa ser maior que zero para iniciar um ciclo.")

    cycle = CompoundCycle(
        target_profit=target_profit,
        starting_stake=bankroll.current_amount,
        status="active",
    )
    db.add(cycle)
    db.commit()
    db.refresh(cycle)
    return cycle


def create_round(db: Session, cycle: CompoundCycle, leg: CycleRoundCreate) -> Bet:
    """Registra uma nova rodada do ciclo: uma aposta de UMA perna só, com
    `stake` igual ao saldo ATUAL da banca (reinveste 100% — é isso que torna
    o crescimento composto). NÃO aposta em lugar nenhum: apenas persiste a
    intenção, exatamente como `app.routers.generator.confirm_combo`.

    Levanta `ValueError` se o ciclo não estiver mais ativo, se já houver uma
    rodada pendente (uma de cada vez), ou se a banca não existir mais.
    """
    if cycle.status != "active":
        raise ValueError("Este ciclo não está mais ativo.")

    pending_round = (
        db.query(Bet)
        .filter(Bet.cycle_id == cycle.id, Bet.status == "pending")
        .first()
    )
    if pending_round is not None:
        raise ValueError(
            "Já existe uma rodada pendente neste ciclo. Registre o "
            "resultado dela antes de iniciar a próxima."
        )

    bankroll = db.query(Bankroll).first()
    if bankroll is None:
        raise ValueError("Banca não encontrada.")

    stake = bankroll.current_amount
    implied_probability = 1.0 / leg.odds_decimal
    round_number = db.query(Bet).filter(Bet.cycle_id == cycle.id).count() + 1

    bet = Bet(
        tier=None,
        stake=stake,
        total_odds=leg.odds_decimal,
        combined_probability=implied_probability,
        potential_return=stake * leg.odds_decimal,
        status="pending",
        cycle_id=cycle.id,
        cycle_round=round_number,
    )
    db.add(bet)
    db.flush()  # garante bet.id antes de criar a leg

    db.add(
        BetLeg(
            bet_id=bet.id,
            league=leg.league,
            event_name=leg.event_name,
            market_type=leg.market_type,
            selection_description=leg.selection_description,
            odds_decimal=leg.odds_decimal,
            implied_probability=implied_probability,
            result="pending",
            event_start=leg.event_start,
        )
    )
    db.commit()
    db.refresh(bet)
    return bet


def check_cycle_progress(db: Session, cycle_id: int) -> None:
    """Chamada por `app.routers.history.settle_bet` logo depois de liquidar
    uma rodada. Atualiza o status do ciclo:
    - "busted" se a rodada recém-liquidada foi perdida (100% do saldo
      estava em jogo, então perder uma rodada encerra o ciclo).
    - "completed" se o lucro acumulado no ciclo (saldo atual da banca menos
      `starting_stake`) atingiu ou passou `target_profit`.
    Não faz nada se o ciclo já não estiver mais ativo (evita sobrescrever um
    "cashed_out" manual, por exemplo).
    """
    cycle = db.query(CompoundCycle).filter(CompoundCycle.id == cycle_id).first()
    if cycle is None or cycle.status != "active":
        return

    last_round = (
        db.query(Bet)
        .filter(Bet.cycle_id == cycle.id)
        .order_by(Bet.cycle_round.desc())
        .first()
    )
    if last_round is None or last_round.status == "pending":
        return

    if last_round.status == "lost":
        cycle.status = "busted"
        cycle.ended_at = datetime.utcnow()
        db.add(cycle)
        db.commit()
        return

    # "won" ou "void": checa se o lucro acumulado já bateu a meta. Uma
    # rodada "void" não muda o saldo, então só interrompe o ciclo se ele já
    # tivesse atingido a meta antes dela (caso raro, mas trata igual).
    bankroll = db.query(Bankroll).first()
    if bankroll is None:
        return
    profit_so_far = bankroll.current_amount - cycle.starting_stake
    if profit_so_far >= cycle.target_profit:
        cycle.status = "completed"
        cycle.ended_at = datetime.utcnow()
        db.add(cycle)
        db.commit()


def cash_out(db: Session, cycle: CompoundCycle) -> CompoundCycle:
    """Encerra um ciclo ativo manualmente, guardando o que já foi ganho até
    agora — é essa flexibilidade (poder parar a qualquer momento) que
    diferencia o ciclo de uma múltipla única "tudo ou nada". NÃO mexe no
    saldo da banca (ele já reflete o que foi ganho nas rodadas anteriores);
    só marca o ciclo como encerrado.

    Levanta `ValueError` se o ciclo já não estiver ativo, ou se houver uma
    rodada pendente (é preciso liquidar ou aguardar antes de encerrar).
    """
    if cycle.status != "active":
        raise ValueError("Este ciclo não está mais ativo.")
    pending_round = (
        db.query(Bet)
        .filter(Bet.cycle_id == cycle.id, Bet.status == "pending")
        .first()
    )
    if pending_round is not None:
        raise ValueError(
            "Existe uma rodada pendente neste ciclo. Registre o resultado "
            "dela antes de encerrar o ciclo."
        )
    cycle.status = "cashed_out"
    cycle.ended_at = datetime.utcnow()
    db.add(cycle)
    db.commit()
    db.refresh(cycle)
    return cycle
