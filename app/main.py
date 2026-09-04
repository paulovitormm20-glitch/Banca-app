"""Ponto de entrada da aplicação FastAPI — Gestor de Banca + Gerador de Múltiplas.

Carrega variáveis de ambiente (`.env`) ANTES de qualquer import que possa ler
`os.environ` (relevante para `ODDS_API_KEY`, usada em `app.services.odds_service`
através da cadeia de imports dos routers abaixo).
"""

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from app.auth import require_auth  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.routers import bankroll, cycle, generator, history, odds  # noqa: E402

# `dependencies=[Depends(require_auth)]` aplica a checagem de usuário/senha a
# TODAS as rotas abaixo (páginas e API) — necessário assim que o app for
# exposto na internet (ex.: Render). Localmente, sem APP_USERNAME/APP_PASSWORD
# no ambiente, `require_auth` não exige nada (ver app/auth.py).
app = FastAPI(title="Gestor de Banca", dependencies=[Depends(require_auth)])


@app.on_event("startup")
def on_startup() -> None:
    """Cria as tabelas do banco (SQLite) caso ainda não existam."""
    Base.metadata.create_all(bind=engine)


# Arquivos estáticos (CSS puro, sem build step / sem npm).
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Templates Jinja2 (renderização server-side apenas do esqueleto da página;
# os dados reais são buscados via fetch() no JS de cada template).
templates = Jinja2Templates(directory="app/templates")

# Routers da API.
app.include_router(bankroll.router)
app.include_router(odds.router)
app.include_router(generator.router)
app.include_router(history.router)
app.include_router(cycle.router)


# ---------------------------------------------------------------------------
# Rotas de página (server-side apenas devolve o HTML base; os dados são
# carregados no cliente via fetch() para não duplicar lógica de template).
# ---------------------------------------------------------------------------

@app.get("/")
async def dashboard_page(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/bankroll-page")
async def bankroll_page(request: Request):
    return templates.TemplateResponse(request, "bankroll.html")


@app.get("/generator-page")
async def generator_page(request: Request):
    return templates.TemplateResponse(request, "generator.html")


@app.get("/history-page")
async def history_page(request: Request):
    return templates.TemplateResponse(request, "history.html")


@app.get("/cycle-page")
async def cycle_page(request: Request):
    return templates.TemplateResponse(request, "cycle.html")
