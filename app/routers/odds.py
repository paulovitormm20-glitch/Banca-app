"""
Router de odds: entrada manual (curada pela whitelist) e atualização
opcional a partir da The Odds API.

Regra de produto (não-negociável, ver DESIGN.md): este router NUNCA aposta
em lugar nenhum e NUNCA calcula ou expõe uma "% de segurança" inventada — só
lida com o registro de odds reais (`odds_decimal`). A probabilidade
implícita (1 / odds_decimal) e a probabilidade combinada da múltipla são
calculadas por `app.services.generator_service`, não aqui.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import OddsCacheOut, OddsManualCreate
from app.services.odds_service import (
    get_api_keys,
    add_manual_odds,
    get_cached_odds,
    get_remaining_credits,
    refresh_cache_from_api,
)

router = APIRouter(prefix="/odds", tags=["odds"])


@router.post("/manual", response_model=OddsCacheOut)
def create_manual_odds(entry: OddsManualCreate, db: Session = Depends(get_db)):
    """Registra manualmente uma odd coletada pelo próprio usuário em uma casa
    de apostas (Betano/Bet365/etc.). Nenhuma integração de login/aposta
    automática acontece aqui — é só o registro do que o usuário viu.

    Retorna 400 se a liga ou o tipo de mercado não estiverem na whitelist do
    sistema (`ValueError` levantado por `add_manual_odds`).
    """
    try:
        return add_manual_odds(db, entry)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cached", response_model=list[OddsCacheOut])
def list_cached_odds(db: Session = Depends(get_db)):
    """Lista as odds em cache já filtradas pela whitelist de ligas/mercados,
    ordenadas por `odds_decimal` ASC (menor odd = maior probabilidade
    implícita primeiro)."""
    return get_cached_odds(db)


@router.get("/credits")
def get_credits():
    """Consulta quantos créditos restam nas chaves da The Odds API
    configuradas (`ODDS_API_KEY`/`ODDS_API_KEYS`), SEM gastar nenhum
    crédito — usa `GET /v4/sports`, que a documentação oficial afirma
    explicitamente não contar no limite de uso. Usado pelo painel pra
    mostrar o consumo a qualquer momento, sem precisar disparar um refresh
    completo (que aí sim gasta créditos de verdade)."""
    keys = get_api_keys()
    return {
        "configured": bool(keys),
        "num_keys": len(keys),
        "remaining": get_remaining_credits(keys) if keys else None,
    }


@router.post("/refresh")
def refresh_odds(
    leagues: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Tenta atualizar o cache de odds a partir da The Odds API.

    `leagues` precisa ser `Query(...)` explicitamente: em endpoints POST, o
    FastAPI trataria um `list[str]` sem essa anotação como corpo JSON da
    requisição, não como query string — sem isso, `?leagues=Serie+A` era
    silenciosamente ignorado e a atualização sempre buscava as 7 ligas da
    whitelist inteira, gastando muito mais créditos da API do que o pedido.

    Se nenhuma chave estiver configurada (`ODDS_API_KEY` ou `ODDS_API_KEYS`),
    `refresh_cache_from_api` retorna o resumo zerado — nesse caso
    respondemos com uma mensagem amigável explicando que a entrada manual
    continua disponível em `/odds`. Caso contrário, devolvemos quantas
    pernas são novas e quantas já existiam e tiveram a odd atualizada (com a
    contagem de quantas caíram/subiram/ficaram iguais), para alimentar o
    indicador de "odds ao vivo" do painel.
    """
    summary = refresh_cache_from_api(db, leagues)
    touched = summary["new"] + summary["updated"]

    if touched == 0 and not get_api_keys():
        return {
            **summary,
            "message": "Nenhuma chave da The Odds API configurada — use entrada manual em /odds ou no painel.",
        }

    parts = []
    if summary["new"]:
        parts.append(f"{summary['new']} pernas novas")
    if summary["down"]:
        parts.append(f"{summary['down']} com odd caindo")
    if summary["up"]:
        parts.append(f"{summary['up']} com odd subindo")
    if summary["same"]:
        parts.append(f"{summary['same']} sem mudança")
    message = ("Atualizado: " + ", ".join(parts) + ".") if parts else "Nenhuma odd nova encontrada nas ligas monitoradas."

    if summary.get("requests_remaining") is not None:
        message += f" ({summary['requests_remaining']} créditos restantes este mês.)"

    return {**summary, "message": message}
