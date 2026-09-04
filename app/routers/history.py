"""
Router de histórico, resolução (settle) e estatísticas de apostas do Gestor
de Banca.

Endpoints (sem prefixo comum, tag "history" — ver DESIGN.md):
- `GET /history`                  — lista todas as apostas (mais recentes
                                     primeiro), cada uma com suas pernas.
- `POST /bets/{bet_id}/settle`    — registra o resultado real de uma aposta
                                     pendente e atualiza o saldo da banca.
- `GET /history/stats`            — estatísticas agregadas (usadas no
                                     dashboard).
- `GET /history/stats/by-tier`    — taxa de acerto real vs. probabilidade
                                     combinada média estimada, por tier.

Regra de produto (ver DESIGN.md): este router nunca aposta em lugar do
usuário. `POST /bets/{bet_id}/settle` apenas registra o resultado que o
usuário observou depois de ter apostado manualmente na casa de apostas — o
sistema não tem (e nunca terá) nenhuma integração de login/sessão com casas
de apostas.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Bankroll, BankrollLog, Bet
from app.schemas import BankrollStats, BetOut, SettleRequest, TierStatOut
from app.services.compound_service import check_cycle_progress

router = APIRouter(tags=["history"])

TIERS = (25, 50, 100)


@router.get("/history", response_model=list[BetOut])
def list_history(db: Session = Depends(get_db)) -> list[Bet]:
    """Lista as apostas do Gerador de Múltiplas (mais recentes primeiro),
    cada uma com suas pernas (`BetLeg`s) incluídas via `BetOut.legs`.

    Exclui rodadas de Ciclo de Juros Compostos (`Bet.cycle_id` preenchido) —
    essas têm sua própria listagem em `GET /cycle/{id}/rounds`, já que são
    um tipo de aposta com formato e finalidade diferentes (1 perna, odd
    baixa, sem "tier")."""
    return (
        db.query(Bet)
        .filter(Bet.cycle_id.is_(None))
        .order_by(Bet.created_at.desc())
        .all()
    )


@router.post("/bets/{bet_id}/settle", response_model=BetOut)
def settle_bet(
    bet_id: int, payload: SettleRequest, db: Session = Depends(get_db)
) -> Bet:
    """Registra o resultado real (observado pelo usuário na casa de apostas)
    de uma aposta pendente e atualiza o saldo da banca de acordo.

    NOTA DE DESIGN (ver DESIGN.md): como o app NUNCA debita o `stake` da
    banca no momento de `POST /generator/confirm` — o usuário aposta sempre
    manualmente fora do sistema, e o stake nunca é "reservado"/subtraído
    aqui dentro nesse momento —, o saldo (`Bankroll.current_amount`) só é
    afetado agora, no settle. Isso é intencional:
      - "won":  soma o ganho líquido (`potential_return - stake`) ao saldo,
                já que o stake nunca havia sido debitado antes.
      - "lost": subtrai o `stake` do saldo, pois o dinheiro foi de fato
                perdido na casa de apostas.
      - "void": nenhuma mudança de saldo (a aposta foi anulada/cancelada
                pela casa), mas ainda assim registramos um `BankrollLog`
                com `change_amount=0` para manter o histórico completo de
                eventos da banca.
    """
    bet = db.query(Bet).filter(Bet.id == bet_id).first()
    if bet is None or bet.status != "pending":
        raise HTTPException(
            status_code=404, detail="Aposta não encontrada ou já resolvida"
        )

    if payload.result not in ("won", "lost", "void"):
        raise HTTPException(
            status_code=400, detail="result deve ser 'won', 'lost' ou 'void'"
        )

    # Atualiza o resultado das pernas individuais, se informado. Ignora
    # `leg_id`s que não pertencem a esta aposta.
    if payload.leg_results:
        legs_by_id = {leg.id: leg for leg in bet.legs}
        for item in payload.leg_results:
            leg = legs_by_id.get(item.get("leg_id"))
            leg_result = item.get("result")
            if leg is not None and leg_result is not None:
                leg.result = leg_result
                db.add(leg)

    bet.status = payload.result
    bet.settled_at = datetime.utcnow()

    # A aplicação administra uma única banca (uma única linha em `bankroll`).
    # Se ela ainda não foi configurada, a aposta ainda é resolvida
    # normalmente, apenas sem nenhum ajuste de saldo (não há saldo a ajustar).
    bankroll = db.query(Bankroll).first()
    if bankroll is not None:
        if payload.result == "won":
            change_amount = bet.potential_return - bet.stake
            reason = "bet_won"
        elif payload.result == "lost":
            change_amount = -bet.stake
            reason = "bet_lost"
        else:  # "void"
            change_amount = 0.0
            reason = "bet_void"

        bankroll.current_amount += change_amount
        db.add(bankroll)

        log = BankrollLog(
            bankroll_id=bankroll.id,
            change_amount=change_amount,
            balance_after=bankroll.current_amount,
            reason=reason,
            bet_id=bet.id,
        )
        db.add(log)

    db.add(bet)
    db.commit()
    db.refresh(bet)

    # Se esta aposta é uma rodada de um Ciclo de Juros Compostos, atualiza o
    # status do ciclo (encerra em "busted" se perdeu, ou "completed" se o
    # lucro-alvo foi atingido) — ver app.services.compound_service.
    if bet.cycle_id is not None:
        check_cycle_progress(db, bet.cycle_id)

    return bet


@router.get("/history/stats", response_model=BankrollStats)
def get_stats(db: Session = Depends(get_db)) -> BankrollStats:
    """Estatísticas agregadas usadas no dashboard: taxa de acerto geral e por
    tier (sobre apostas resolvidas won/lost — "void" nunca conta como acerto
    nem como erro), maior sequência consecutiva de derrotas, total de
    apostas e total de apostas já resolvidas (won/lost/void).

    Exclui rodadas de Ciclo de Juros Compostos (`Bet.cycle_id` preenchido):
    são apostas simples de odd baixa com um perfil de risco totalmente
    diferente do Gerador de Múltiplas, misturá-las aqui distorceria a taxa
    de acerto geral. Estatísticas do ciclo ficam na própria página do ciclo.
    """
    all_bets = db.query(Bet).filter(Bet.cycle_id.is_(None)).all()
    total_bets = len(all_bets)

    settled = [b for b in all_bets if b.status != "pending"]
    settled_bets = len(settled)

    # "void" não conta nem como acerto nem como erro para as taxas de acerto.
    decided = [b for b in settled if b.status in ("won", "lost")]
    wins = sum(1 for b in decided if b.status == "won")
    win_rate_overall = (wins / len(decided) * 100) if decided else 0.0

    win_rate_by_tier: dict[str, float] = {}
    for tier in TIERS:
        tier_decided = [b for b in decided if b.tier == tier]
        tier_wins = sum(1 for b in tier_decided if b.status == "won")
        win_rate_by_tier[str(tier)] = (
            (tier_wins / len(tier_decided) * 100) if tier_decided else 0.0
        )

    # Maior sequência consecutiva de "lost", ordenando as apostas resolvidas
    # (won/lost/void) por `settled_at`. Um "won" ou um "void" interrompe a
    # sequência em andamento.
    ordered_settled = sorted(settled, key=lambda b: b.settled_at or datetime.min)
    max_losing_streak = 0
    current_streak = 0
    for bet in ordered_settled:
        if bet.status == "lost":
            current_streak += 1
            max_losing_streak = max(max_losing_streak, current_streak)
        else:
            current_streak = 0

    return BankrollStats(
        win_rate_overall=win_rate_overall,
        win_rate_by_tier=win_rate_by_tier,
        max_losing_streak=max_losing_streak,
        total_bets=total_bets,
        settled_bets=settled_bets,
    )


@router.get("/history/stats/by-tier", response_model=list[TierStatOut])
def get_stats_by_tier(db: Session = Depends(get_db)) -> list[TierStatOut]:
    """Compara, para cada tier (25/50/100), a taxa de acerto REAL observada
    (`win_rate_real`, sobre apostas resolvidas — won/lost/void) com a
    probabilidade combinada média que o gerador havia estimado
    (`avg_estimated_probability`). Usado para calibrar a curadoria de
    mercados (módulo 4 do DESIGN.md)."""
    results: list[TierStatOut] = []
    for tier in TIERS:
        tier_bets = db.query(Bet).filter(Bet.tier == tier).all()
        total = len(tier_bets)

        settled_tier_bets = [b for b in tier_bets if b.status != "pending"]
        settled = len(settled_tier_bets)

        wins = sum(1 for b in settled_tier_bets if b.status == "won")
        win_rate_real = (wins / settled * 100) if settled else 0.0

        avg_estimated_probability = (
            sum(b.combined_probability for b in settled_tier_bets) / settled * 100
            if settled
            else 0.0
        )

        results.append(
            TierStatOut(
                tier=tier,
                total=total,
                settled=settled,
                wins=wins,
                win_rate_real=win_rate_real,
                avg_estimated_probability=avg_estimated_probability,
            )
        )
    return results
