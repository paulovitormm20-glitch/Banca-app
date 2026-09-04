"""
Router de gestão da banca (bankroll) do Gestor de Banca.

Endpoints (prefix "/bankroll", tag "bankroll"):
- `POST /bankroll/init`        — cria a (única) banca.
- `GET /bankroll`               — consulta a banca atual.
- `POST /bankroll/adjust`      — ajuste manual de saldo (depósito/saque/correção).
- `GET /bankroll/stop-status`  — verifica se os limites de stop diário/semanal
                                  (definidos pelo próprio usuário) foram atingidos.

Regra de produto (ver DESIGN.md): este router nunca aposta em lugar do
usuário e nunca calcula nenhuma "% de segurança" inventada — ele apenas
administra o saldo da banca e os limites de stop que o próprio usuário
configurou.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Bankroll, BankrollLog
from app.schemas import (
    BankrollAdjust,
    BankrollInit,
    BankrollLogOut,
    BankrollOut,
    StopStatusOut,
)

router = APIRouter(prefix="/bankroll", tags=["bankroll"])


def _get_bankroll(db: Session) -> Bankroll | None:
    """A aplicação administra uma única banca (uma única linha em `bankroll`)."""
    return db.query(Bankroll).first()


def _to_bankroll_out(bankroll: Bankroll) -> BankrollOut:
    """Monta `BankrollOut`, incluindo os campos calculados `profit_loss` e
    `roi_percent` que não existem como colunas no modelo `Bankroll`."""
    profit_loss = bankroll.current_amount - bankroll.initial_amount
    roi_percent = (
        (profit_loss / bankroll.initial_amount * 100)
        if bankroll.initial_amount
        else 0.0
    )
    return BankrollOut(
        id=bankroll.id,
        initial_amount=bankroll.initial_amount,
        current_amount=bankroll.current_amount,
        unit_percent=bankroll.unit_percent,
        stop_daily_percent=bankroll.stop_daily_percent,
        stop_weekly_percent=bankroll.stop_weekly_percent,
        created_at=bankroll.created_at,
        profit_loss=profit_loss,
        roi_percent=roi_percent,
    )


@router.post("/init", response_model=BankrollOut)
def init_bankroll(payload: BankrollInit, db: Session = Depends(get_db)) -> BankrollOut:
    """Cria a banca inicial, se ainda não existir. Só pode haver uma banca."""
    if _get_bankroll(db) is not None:
        raise HTTPException(status_code=400, detail="Banca já inicializada")

    bankroll = Bankroll(
        initial_amount=payload.initial_amount,
        current_amount=payload.initial_amount,
        unit_percent=payload.unit_percent,
        stop_daily_percent=payload.stop_daily_percent,
        stop_weekly_percent=payload.stop_weekly_percent,
    )
    db.add(bankroll)
    db.commit()
    db.refresh(bankroll)

    log = BankrollLog(
        bankroll_id=bankroll.id,
        change_amount=payload.initial_amount,
        balance_after=payload.initial_amount,
        reason="initial",
    )
    db.add(log)
    db.commit()

    return _to_bankroll_out(bankroll)


@router.get("", response_model=BankrollOut)
def get_bankroll(db: Session = Depends(get_db)) -> BankrollOut:
    """Retorna a banca única existente, ou 404 se ainda não foi configurada."""
    bankroll = _get_bankroll(db)
    if bankroll is None:
        raise HTTPException(status_code=404, detail="Banca não encontrada")
    return _to_bankroll_out(bankroll)


@router.post("/adjust", response_model=BankrollOut)
def adjust_bankroll(
    payload: BankrollAdjust, db: Session = Depends(get_db)
) -> BankrollOut:
    """Ajuste manual de saldo (depósito, saque ou correção). `amount` pode ser
    negativo. `note` é apenas o motivo informado pelo usuário na requisição —
    o modelo `BankrollLog` não tem uma coluna dedicada para ela, então ela não
    é persistida (o `reason` fixo "manual_adjustment" é o que fica no log,
    conforme DESIGN.md)."""
    bankroll = _get_bankroll(db)
    if bankroll is None:
        raise HTTPException(status_code=404, detail="Banca não encontrada")

    bankroll.current_amount += payload.amount
    db.add(bankroll)
    db.commit()
    db.refresh(bankroll)

    log = BankrollLog(
        bankroll_id=bankroll.id,
        change_amount=payload.amount,
        balance_after=bankroll.current_amount,
        reason="manual_adjustment",
    )
    db.add(log)
    db.commit()

    return _to_bankroll_out(bankroll)


@router.get("/history", response_model=list[BankrollLogOut])
def get_bankroll_history(db: Session = Depends(get_db)) -> list[BankrollLog]:
    """Retorna o histórico de saldo (`BankrollLog`), mais antigo primeiro —
    usado para desenhar o gráfico de evolução da banca no painel. Cada linha
    já tem `balance_after`, então o front não precisa recalcular nada."""
    bankroll = _get_bankroll(db)
    if bankroll is None:
        raise HTTPException(status_code=404, detail="Banca não encontrada")

    return (
        db.query(BankrollLog)
        .filter(BankrollLog.bankroll_id == bankroll.id)
        .order_by(BankrollLog.created_at.asc())
        .all()
    )


@router.get("/stop-status", response_model=StopStatusOut)
def get_stop_status(db: Session = Depends(get_db)) -> StopStatusOut:
    """Calcula a perda percentual (sobre `initial_amount`) acumulada desde o
    início do dia UTC (daily) e desde 7 dias atrás (weekly), somando apenas os
    `change_amount` negativos de `BankrollLog`, e compara com os limites de
    stop configurados pelo usuário. Se um limite for `None`, seu `limit_hit`
    correspondente é sempre `False`."""
    bankroll = _get_bankroll(db)
    if bankroll is None:
        raise HTTPException(status_code=404, detail="Banca não encontrada")

    now = datetime.utcnow()
    start_of_day = datetime(now.year, now.month, now.day)
    start_of_week = now - timedelta(days=7)

    daily_negative_logs = (
        db.query(BankrollLog)
        .filter(
            BankrollLog.bankroll_id == bankroll.id,
            BankrollLog.change_amount < 0,
            BankrollLog.created_at >= start_of_day,
        )
        .all()
    )
    weekly_negative_logs = (
        db.query(BankrollLog)
        .filter(
            BankrollLog.bankroll_id == bankroll.id,
            BankrollLog.change_amount < 0,
            BankrollLog.created_at >= start_of_week,
        )
        .all()
    )

    daily_loss_sum = sum(log.change_amount for log in daily_negative_logs)
    weekly_loss_sum = sum(log.change_amount for log in weekly_negative_logs)

    if bankroll.initial_amount:
        daily_loss_percent = abs(daily_loss_sum) / bankroll.initial_amount * 100
        weekly_loss_percent = abs(weekly_loss_sum) / bankroll.initial_amount * 100
    else:
        daily_loss_percent = 0.0
        weekly_loss_percent = 0.0

    daily_limit_hit = (
        bankroll.stop_daily_percent is not None
        and daily_loss_percent >= bankroll.stop_daily_percent
    )
    weekly_limit_hit = (
        bankroll.stop_weekly_percent is not None
        and weekly_loss_percent >= bankroll.stop_weekly_percent
    )

    return StopStatusOut(
        daily_loss_percent=daily_loss_percent,
        weekly_loss_percent=weekly_loss_percent,
        daily_limit_hit=daily_limit_hit,
        weekly_limit_hit=weekly_limit_hit,
        stop_daily_percent=bankroll.stop_daily_percent,
        stop_weekly_percent=bankroll.stop_weekly_percent,
    )
