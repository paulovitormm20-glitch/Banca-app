"""
Router do gerador de múltiplas.

Regras de produto (não-negociáveis, ver DESIGN.md):
- Este router NUNCA calcula, inventa ou expõe qualquer "% de segurança".
  A única probabilidade que existe é `implied_probability` (1 / odds_decimal)
  por perna e `combined_probability` (produto das pernas escolhidas), ambas
  já calculadas por `app.services.generator_service.generate_combo`.
- `POST /generator/preview` apenas monta um preview em memória — NÃO salva
  nada no banco.
- `POST /generator/confirm` só registra a INTENÇÃO do usuário depois que ele
  já apostou manualmente, com o próprio dinheiro dele, na casa de apostas
  (Betano/Bet365/etc.). Este sistema NUNCA aposta sozinho: não há nenhuma
  integração de login/sessão com casas de apostas, nenhum clique automático,
  nenhum envio de aposta a terceiros. O endpoint apenas persiste, como
  `Bet` + `BetLeg`s, exatamente o combo que o usuário já decidiu (e já
  apostou) — ele confia no payload recebido (que é o mesmo objeto que
  `/generator/preview` retornou) e NÃO recalcula nada.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Bet, BetLeg
from app.schemas import GeneratedComboOut, GeneratorConfirm, GeneratorRequest, BetOut
from app.services.generator_service import InsufficientOddsError, generate_combo

router = APIRouter(prefix="/generator", tags=["generator"])


@router.post("/preview", response_model=GeneratedComboOut)
def preview_combo(payload: GeneratorRequest, db: Session = Depends(get_db)):
    """Monta um preview de múltipla ("combo") para o `tier` e `stake`
    pedidos, usando as odds disponíveis em cache.

    NÃO salva nada no banco — é só preview. O usuário decide se quer
    apostar manualmente na casa de apostas antes de chamar `/generator/confirm`.

    Retorna 400 se o `tier` for inválido (`ValueError` de `generate_combo`)
    ou se não houver odds suficientes em cache para atingir o `tier`
    (`InsufficientOddsError`).
    """
    try:
        return generate_combo(
            db,
            payload.tier,
            payload.stake,
            event_from=payload.event_date_from,
            event_to=payload.event_date_to,
        )
    except InsufficientOddsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/confirm", response_model=BetOut)
def confirm_combo(payload: GeneratorConfirm, db: Session = Depends(get_db)):
    """Persiste, como `Bet` (status="pending") + `BetLeg`s, o combo que o
    usuário já decidiu apostar.

    IMPORTANTE: este endpoint NUNCA aposta em lugar nenhum — a aposta já foi
    feita manualmente pelo próprio usuário, na casa de apostas, fora deste
    sistema. Aqui só registramos a intenção/registro dela para
    acompanhamento de banca e histórico. Não recalculamos nada: confiamos no
    payload recebido, que é o mesmo objeto que `/generator/preview` retornou
    (o front reenvia o preview exato que o usuário decidiu apostar).
    """
    bet = Bet(
        tier=payload.tier,
        stake=payload.stake,
        total_odds=payload.total_odds,
        combined_probability=payload.combined_probability,
        potential_return=payload.potential_return,
        status="pending",
    )
    db.add(bet)
    db.flush()  # garante bet.id antes de criar as legs

    for leg in payload.legs:
        db.add(
            BetLeg(
                bet_id=bet.id,
                league=leg.league,
                event_name=leg.event_name,
                market_type=leg.market_type,
                selection_description=leg.selection_description,
                odds_decimal=leg.odds_decimal,
                implied_probability=leg.implied_probability,
                result="pending",
                event_start=leg.event_start,
            )
        )

    db.commit()
    db.refresh(bet)
    return bet
