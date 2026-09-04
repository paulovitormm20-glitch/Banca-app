"""
Router do Ciclo de Juros Compostos — ver `app.services.compound_service`
para a explicação completa do mecanismo (apostas simples de odd baixa,
saldo total reinvestido a cada rodada, até bater um lucro-alvo em R$ ou
perder uma rodada).

Regra de produto (não-negociável, ver DESIGN.md): este router NUNCA aposta
em lugar nenhum — `POST /cycle/{id}/rounds` apenas registra a intenção do
usuário depois que ele decidir apostar manualmente na casa de apostas.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Bet, CompoundCycle
from app.schemas import BetOut, CycleOut, CycleRoundCreate, CycleStartRequest
from app.services.compound_service import cash_out, create_round, get_active_cycle, start_cycle

router = APIRouter(prefix="/cycle", tags=["cycle"])


def _get_cycle_or_404(cycle_id: int, db: Session) -> CompoundCycle:
    cycle = db.query(CompoundCycle).filter(CompoundCycle.id == cycle_id).first()
    if cycle is None:
        raise HTTPException(status_code=404, detail="Ciclo não encontrado")
    return cycle


@router.post("/start", response_model=CycleOut)
def start_cycle_endpoint(payload: CycleStartRequest, db: Session = Depends(get_db)):
    """Inicia um novo ciclo com o saldo atual da banca como base. 400 se já
    houver um ciclo ativo, se a banca não existir, ou se o saldo for <= 0."""
    try:
        return start_cycle(db, payload.target_profit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/active", response_model=CycleOut)
def get_active_cycle_endpoint(db: Session = Depends(get_db)):
    """Retorna o ciclo ativo, se houver. 404 se não houver nenhum ciclo
    ativo no momento (não é um erro — é o estado normal fora de um ciclo)."""
    cycle = get_active_cycle(db)
    if cycle is None:
        raise HTTPException(status_code=404, detail="Nenhum ciclo ativo no momento")
    return cycle


@router.get("/history", response_model=list[CycleOut])
def list_cycles(db: Session = Depends(get_db)):
    """Lista todos os ciclos (ativos e encerrados), mais recentes primeiro."""
    return db.query(CompoundCycle).order_by(CompoundCycle.created_at.desc()).all()


@router.get("/{cycle_id}", response_model=CycleOut)
def get_cycle(cycle_id: int, db: Session = Depends(get_db)):
    """Retorna um ciclo específico independente do status — ao contrário de
    `/cycle/active`, funciona também para ciclos já encerrados. Usado pelo
    front para mostrar o resultado final (meta atingida / perdido /
    encerrado manualmente) logo após a última rodada, já que nesse momento
    o ciclo acabou de sair de `/cycle/active` (que só lista status="active").
    """
    return _get_cycle_or_404(cycle_id, db)


@router.get("/{cycle_id}/rounds", response_model=list[BetOut])
def list_rounds(cycle_id: int, db: Session = Depends(get_db)):
    """Lista as rodadas (cada uma um `Bet` de 1 perna) de um ciclo, em
    ordem crescente de número de rodada."""
    _get_cycle_or_404(cycle_id, db)
    return (
        db.query(Bet)
        .filter(Bet.cycle_id == cycle_id)
        .order_by(Bet.cycle_round.asc())
        .all()
    )


@router.post("/{cycle_id}/rounds", response_model=BetOut)
def create_round_endpoint(cycle_id: int, payload: CycleRoundCreate, db: Session = Depends(get_db)):
    """Registra uma nova rodada (aposta de 1 perna, stake = saldo atual da
    banca). 400 se o ciclo não estiver ativo ou já houver rodada pendente."""
    cycle = _get_cycle_or_404(cycle_id, db)
    try:
        return create_round(db, cycle, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{cycle_id}/cash-out", response_model=CycleOut)
def cash_out_endpoint(cycle_id: int, db: Session = Depends(get_db)):
    """Encerra um ciclo ativo manualmente, guardando o lucro acumulado até
    agora. 400 se o ciclo não estiver ativo ou houver rodada pendente."""
    cycle = _get_cycle_or_404(cycle_id, db)
    try:
        return cash_out(db, cycle)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
