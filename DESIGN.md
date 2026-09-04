# Gestor de Banca + Gerador de Múltiplas — Especificação de Arquitetura

Este é o contrato ÚNICO que todos os módulos devem seguir. Cada agente implementa
os arquivos designados a ele EXATAMENTE conforme os nomes de classes, campos,
funções e endpoints abaixo, porque outros arquivos vão importar essas coisas
pelo nome exato. Não invente nomes alternativos.

## Regras de produto não-negociáveis (aplicar em TODA a stack)

1. NUNCA existe uma "% de segurança" fixa ou inventada em lugar nenhum do código
   ou da UI. Toda probabilidade exibida é `1 / odd_decimal` por perna, e a
   probabilidade da múltipla é o PRODUTO das probabilidades das pernas
   escolhidas. Isso deve estar visível em: preview do gerador, confirmação,
   histórico e qualquer card de "múltipla segura".
2. O sistema NUNCA aposta sozinho em casa de apostas nenhuma. Não existe
   nenhuma integração de login/sessão com Betano/Bet365, nenhum clique
   automático, nenhum envio de aposta a terceiros. O fluxo é sempre:
   sistema sugere → usuário decide → usuário aposta manualmente no
   app/site da casa → usuário volta e registra o resultado manualmente.
3. Toda tela de múltipla "segura" mostra, ao lado do retorno potencial, a
   probabilidade combinada real (não escondida, não em letra miúda).
4. Pernas do mesmo jogo/evento nunca devem ser combinadas juntas numa mesma
   múltipla (correlação estatística invalidaria o cálculo de probabilidade
   por produto). Regra: no máximo 1 perna por `event_name` por múltipla.

## Stack

- Python 3.11+, FastAPI, SQLAlchemy (SQLite), Jinja2 (templates server-side),
  JS vanilla (fetch) nas páginas — sem build step, sem npm.
- Idioma da interface: português (pt-BR). Moeda: R$ (BRL), formatar com 2 casas
  decimais.
- Raiz do projeto: `banca-app/` (já criada com subpastas `app/services`,
  `app/routers`, `app/data`, `app/templates`, `app/static`).

## Estrutura de arquivos (cada um implementado por um agente designado)

```
banca-app/
  requirements.txt
  README.md
  .env.example
  app/
    __init__.py
    main.py
    database.py
    models.py
    schemas.py
    services/
      __init__.py
      odds_service.py
      generator_service.py
    routers/
      __init__.py
      bankroll.py
      odds.py
      generator.py
      history.py
    data/
      __init__.py
      markets_whitelist.py
    templates/
      base.html
      dashboard.html
      bankroll.html
      generator.html
      history.html
    static/
      style.css
```

## `app/database.py`

```python
SQLALCHEMY_DATABASE_URL = "sqlite:///./banca.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    # generator dependency padrão do FastAPI, yield db, finally db.close()
```

## `app/models.py` (SQLAlchemy, `Base` vem de `app.database`)

```python
class Bankroll(Base):
    __tablename__ = "bankroll"
    id: Integer, primary_key
    initial_amount: Float, not null
    current_amount: Float, not null
    unit_percent: Float, not null, default=3.0          # % da banca por unidade de aposta
    stop_daily_percent: Float, nullable=True             # ex.: 10.0 = para se perder 10% no dia
    stop_weekly_percent: Float, nullable=True
    created_at: DateTime, default=datetime.utcnow

class BankrollLog(Base):
    __tablename__ = "bankroll_log"
    id: Integer, primary_key
    bankroll_id: Integer, ForeignKey("bankroll.id")
    change_amount: Float, not null      # positivo (ganho/depósito) ou negativo (perda/saque)
    balance_after: Float, not null
    reason: String, not null            # "initial" | "bet_won" | "bet_lost" | "bet_void" | "manual_adjustment"
    bet_id: Integer, ForeignKey("bets.id"), nullable=True
    created_at: DateTime, default=datetime.utcnow

class OddsCacheEntry(Base):
    __tablename__ = "odds_cache"
    id: Integer, primary_key
    league: String, not null                 # deve bater com um valor em WHITELIST_LEAGUES
    event_name: String, not null             # "Time A vs Time B"
    market_type: String, not null            # chave de MARKET_TYPES
    selection_description: String, not null  # texto humano, ex. "Menos de 2.5 gols"
    odds_decimal: Float, not null
    source: String, not null, default="manual"  # "manual" | "api"
    event_start: DateTime, nullable=True
    fetched_at: DateTime, default=datetime.utcnow

class Bet(Base):
    __tablename__ = "bets"
    id: Integer, primary_key
    tier: Integer, not null              # 25, 50 ou 100
    stake: Float, not null
    total_odds: Float, not null
    combined_probability: Float, not null   # 0..1, produto das probabilidades das pernas
    potential_return: Float, not null       # stake * total_odds
    status: String, not null, default="pending"  # "pending" | "won" | "lost" | "void"
    created_at: DateTime, default=datetime.utcnow
    settled_at: DateTime, nullable=True

class BetLeg(Base):
    __tablename__ = "bet_legs"
    id: Integer, primary_key
    bet_id: Integer, ForeignKey("bets.id"), not null
    league: String, not null
    event_name: String, not null
    market_type: String, not null
    selection_description: String, not null
    odds_decimal: Float, not null
    implied_probability: Float, not null   # 1 / odds_decimal
    result: String, not null, default="pending")  # "pending" | "won" | "lost" | "void"
```

## `app/schemas.py` (Pydantic v2, `model_config = ConfigDict(from_attributes=True)` nos schemas de leitura)

Criar schemas espelhando os models acima, com os seguintes nomes exatos
(campos = mesmos nomes das colunas):

- `BankrollInit` (request): `initial_amount: float`, `unit_percent: float = 3.0`,
  `stop_daily_percent: float | None = None`, `stop_weekly_percent: float | None = None`
- `BankrollAdjust` (request): `amount: float`, `note: str`
- `BankrollOut` (response): todos os campos de `Bankroll` + campos calculados:
  `profit_loss: float` (current_amount - initial_amount), `roi_percent: float`
  ((current_amount - initial_amount) / initial_amount * 100)
- `BankrollStats` (response, usado no dashboard): `win_rate_overall: float` (0..100),
  `win_rate_by_tier: dict[str, float]` (chaves "25","50","100"), `max_losing_streak: int`,
  `total_bets: int`, `settled_bets: int`
- `StopStatusOut`: `daily_loss_percent: float`, `weekly_loss_percent: float`,
  `daily_limit_hit: bool`, `weekly_limit_hit: bool`,
  `stop_daily_percent: float | None`, `stop_weekly_percent: float | None`
- `OddsManualCreate` (request): `league: str`, `event_name: str`, `market_type: str`,
  `selection_description: str`, `odds_decimal: float`, `event_start: datetime | None = None`
- `OddsCacheOut` (response): espelha `OddsCacheEntry`
- `GeneratorRequest` (request): `tier: int` (deve ser 25, 50 ou 100), `stake: float`
- `GeneratedLegOut`: `league`, `event_name`, `market_type`, `selection_description`,
  `odds_decimal`, `implied_probability`
- `GeneratedComboOut` (response do preview, NÃO salvo ainda): `tier: int`, `stake: float`,
  `legs: list[GeneratedLegOut]`, `total_odds: float`, `combined_probability: float`,
  `potential_return: float`
- `GeneratorConfirm` (request): mesmo shape de `GeneratedComboOut` (o front reenvia o
  preview exato que o usuário decidiu apostar) — usado para persistir como `Bet` + `BetLeg`s
- `BetLegOut`: espelha `BetLeg`
- `BetOut` (response): espelha `Bet` + `legs: list[BetLegOut]`
- `SettleRequest` (request): `result: str` ("won" | "lost" | "void"),
  `leg_results: list[dict] | None = None` (cada item `{"leg_id": int, "result": str}`, opcional)
- `TierStatOut`: `tier: int`, `total: int`, `settled: int`, `wins: int`,
  `win_rate_real: float` (wins/settled*100, 0 se settled=0),
  `avg_estimated_probability: float` (média de `combined_probability` das apostas
  settled desse tier, em %)

## `app/data/markets_whitelist.py`

```python
WHITELIST_LEAGUES = [
    "Brasileirão Série A", "Champions League", "Libertadores",
    "Premier League", "La Liga", "Serie A", "Bundesliga",
]

MARKET_TYPES = {
    "under_goals": "Menos de X.5 gols na partida",
    "double_chance_favorite": "Dupla chance no favorito claro",
    "under_corners": "Menos de X.5 cantos na partida",
    "btts_no": "Ambas marcam - Não",
    "team_not_lose": "Time não perde (dupla chance / empate anula)",
}
```
Exportar também `MARKET_TYPE_KEYS = list(MARKET_TYPES.keys())` para validação.

## `app/services/odds_service.py`

Funções (assinatura exata, recebem `db: Session` do SQLAlchemy):

- `get_cached_odds(db, leagues: list[str] | None = None) -> list[OddsCacheEntry]`
  — retorna entradas de `odds_cache` cujo `league` esteja em `WHITELIST_LEAGUES`
  (e em `leagues` se informado) e `market_type` em `MARKET_TYPE_KEYS`, ordenadas
  por `odds_decimal` ASC (menor odd = maior probabilidade implícita primeiro).
- `add_manual_odds(db, entry: OddsManualCreate) -> OddsCacheEntry` — valida que
  `league` está em `WHITELIST_LEAGUES` e `market_type` em `MARKET_TYPE_KEYS`
  (senão `raise ValueError` com mensagem clara), cria e salva o registro com
  `source="manual"`.
- Classe `OddsApiNotConfigured(Exception)`.
- `fetch_from_odds_api(leagues: list[str]) -> list[dict]` — só deve ser chamada
  se a env var `ODDS_API_KEY` estiver definida; se não estiver, `raise OddsApiNotConfigured`.
  Se estiver definida, faz `requests.get` para `https://api.the-odds-api.com/v4/sports/{sport_key}/odds`
  (usar um mapeamento simples liga->sport_key do The Odds API, ex.
  `{"Premier League": "soccer_epl", "La Liga": "soccer_spain_la_liga", "Serie A": "soccer_italy_serie_a", "Bundesliga": "soccer_germany_bundesliga", "Champions League": "soccer_uefa_champs_league", "Brasileirão Série A": "soccer_brazil_campeonato", "Libertadores": "soccer_conmebol_copa_libertadores"}`),
  params `apiKey`, `regions=eu`, `markets=h2h,totals`. Envolver em try/except:
  se a request falhar (rede, chave inválida, liga sem mercado disponível), logar
  e pular essa liga, NUNCA quebrar a função inteira. Retornar lista de dicts crus
  da API (o parsing detalhado para `OddsCacheEntry` pode ficar simplificado/best-effort,
  já que a cobertura de mercados como "dupla chance"/"cantos" na API varia por plano —
  documentar isso como limitação conhecida no docstring).
- `refresh_cache_from_api(db, leagues: list[str] | None = None) -> int` — chama
  `fetch_from_odds_api`, converte o que conseguir mapear para `OddsCacheEntry`
  (`source="api"`), salva, retorna quantidade de novas entradas. Se
  `OddsApiNotConfigured`, retorna `0` silenciosamente (endpoint que chama isso
  decide a mensagem amigável para o usuário).

## `app/services/generator_service.py`

```python
class InsufficientOddsError(Exception): ...

def generate_combo(db: Session, tier: int, stake: float) -> GeneratedComboOut:
    """
    1. tier precisa ser 25, 50 ou 100 (senão ValueError).
    2. legs_pool = get_cached_odds(db)  # já filtrado por whitelist e ordenado por odd ASC
    3. Selecionar gulosamente pernas com MENOR odd primeiro (maior probabilidade
       implícita), respeitando no máximo 1 leg por event_name (pular pernas de
       evento já escolhido).
    4. Multiplicar total_odds acumulando cada leg escolhida; parar assim que
       total_odds >= tier (não precisa ultrapassar muito o alvo, parar no
       primeiro momento em que atingir ou passar o alvo).
    5. Se esgotar o pool sem atingir o tier, raise InsufficientOddsError(
       "Odds insuficientes no cache para montar uma múltipla de {tier}x. "
       "Adicione mais odds manualmente em /odds.")
    6. combined_probability = produto de (1/leg.odds_decimal) de cada leg escolhida
    7. potential_return = stake * total_odds
    8. Retornar GeneratedComboOut com tier, stake, legs (GeneratedLegOut),
       total_odds, combined_probability, potential_return.
    NÃO salva nada no banco — é só preview. Salvar é responsabilidade do
    endpoint /generator/confirm.
    """
```

## Routers (todos com `router = APIRouter()`, usam `Depends(get_db)`)

### `app/routers/bankroll.py` — prefix `/bankroll`, tag "bankroll"
- `POST /bankroll/init` (body `BankrollInit`) → cria a (única) linha de `Bankroll`
  se ainda não existir (se já existir, `raise HTTPException(400, "Banca já inicializada")`),
  registra `BankrollLog(reason="initial", change_amount=initial_amount, balance_after=initial_amount)`,
  retorna `BankrollOut`.
- `GET /bankroll` → retorna `BankrollOut` da única banca existente (404 se não existe).
- `POST /bankroll/adjust` (body `BankrollAdjust`) → soma `amount` a `current_amount`
  (pode ser negativo), cria `BankrollLog(reason="manual_adjustment")`, retorna `BankrollOut`.
- `GET /bankroll/stop-status` → calcula somando `change_amount` negativos de
  `BankrollLog` desde início do dia UTC (daily) e desde 7 dias atrás (weekly),
  divide pelo `initial_amount` da banca (valor absoluto, em %), compara com
  `stop_daily_percent`/`stop_weekly_percent` (se `None`, `limit_hit=False`
  sempre), retorna `StopStatusOut`.

### `app/routers/odds.py` — prefix `/odds`, tag "odds"
- `POST /odds/manual` (body `OddsManualCreate`) → chama `add_manual_odds`,
  retorna `OddsCacheOut` (400 se `ValueError` da validação de whitelist).
- `GET /odds/cached` → chama `get_cached_odds(db)`, retorna `list[OddsCacheOut]`.
- `POST /odds/refresh` (query opcional `leagues: list[str] | None`) → chama
  `refresh_cache_from_api`; se retornar 0 E `ODDS_API_KEY` não configurada,
  responde `{"added": 0, "message": "ODDS_API_KEY não configurada — use entrada manual em /odds."}`;
  senão `{"added": N, "message": f"{N} novas odds adicionadas do The Odds API."}`.

### `app/routers/generator.py` — prefix `/generator`, tag "generator"
- `POST /generator/preview` (body `GeneratorRequest`) → chama `generate_combo`,
  retorna `GeneratedComboOut` (400 com a mensagem de `InsufficientOddsError` se faltar odds).
- `POST /generator/confirm` (body `GeneratorConfirm`) → cria `Bet` (status="pending")
  + `BetLeg`s a partir do payload recebido (NÃO recalcula, confia no que o preview
  mandou — é o mesmo objeto que o preview retornou, o usuário só decidiu confirmar),
  retorna `BetOut`. IMPORTANTE: este endpoint só registra a intenção do usuário —
  em nenhum momento ele efetivamente aposta em lugar nenhum.

### `app/routers/history.py` — prefix "" (sem prefixo comum), tag "history"
- `GET /history` → `list[BetOut]` de todas as apostas, mais recentes primeiro,
  cada uma com suas `legs`.
- `POST /bets/{bet_id}/settle` (body `SettleRequest`) → 404 se bet não existe ou
  já não está "pending". Atualiza `result` das legs se `leg_results` foi enviado.
  Define `bet.status = result`, `bet.settled_at = now`. Se `result == "won"`:
  `BankrollLog(change_amount=+bet.potential_return - bet.stake, reason="bet_won", bet_id=bet.id)`
  e soma isso a `bankroll.current_amount` (o ganho líquido; o stake já estava
  "reservado" mentalmente, não foi debitado antes — ver nota abaixo). Se `"lost"`:
  `BankrollLog(change_amount=-bet.stake, reason="bet_lost", bet_id=bet.id)`, subtrai
  `stake` de `current_amount`. Se `"void"`: nenhuma mudança de saldo,
  `BankrollLog(change_amount=0, reason="bet_void", bet_id=bet.id)`. Retorna `BetOut`
  atualizado.
  NOTA DE DESIGN: como o app não debita o stake no momento da confirmação (o
  usuário aposta manualmente fora do sistema), o saldo só é afetado no settle —
  isso é intencional e deve ficar comentado no código.
- `GET /history/stats` → retorna `BankrollStats` (usado pelo dashboard) calculando:
  `win_rate_overall` sobre todas as bets settled (won+lost, void não conta),
  `win_rate_by_tier` (dict por tier), `max_losing_streak` (maior sequência
  consecutiva de "lost" ordenando por `settled_at`), `total_bets`, `settled_bets`.
- `GET /history/stats/by-tier` → retorna `list[TierStatOut]` para os tiers 25/50/100
  (taxa de acerto real observada vs. probabilidade combinada média estimada —
  usado para calibrar a curadoria de mercados conforme pedido no módulo 4).

## `app/main.py`

- `app = FastAPI(title="Gestor de Banca")`
- `Base.metadata.create_all(bind=engine)` no startup.
- Monta `/static` como `StaticFiles(directory="app/static")`.
- `templates = Jinja2Templates(directory="app/templates")`.
- Inclui os 4 routers (`app.include_router(...)`).
- Rotas de página (retornam `TemplateResponse`): `GET /` (dashboard.html),
  `GET /bankroll-page` (bankroll.html), `GET /generator-page` (generator.html),
  `GET /history-page` (history.html). Passar `{"request": request}` no context;
  os dados reais são buscados via fetch() no JS de cada template, não no
  server-side render (mantém tudo simples, sem lógica duplicada de template).
- Carregar `.env` com `python-dotenv` no topo (`load_dotenv()`), antes de
  qualquer import que leia `os.environ` (relevante para `ODDS_API_KEY`).

## `requirements.txt`

```
fastapi
uvicorn[standard]
sqlalchemy
jinja2
python-multipart
requests
python-dotenv
pydantic
```

## `.env.example`

```
# Opcional. Sem isso, o sistema funciona 100% com entrada manual de odds.
# Ver https://the-odds-api.com/ para conseguir uma chave gratuita.
ODDS_API_KEY=
```

## Templates (`app/templates/*.html`)

Todas estendem `base.html` (`{% extends "base.html" %}` + bloco `{% block content %}`).
`base.html` tem: `<nav>` com links para `/`, `/bankroll-page`, `/generator-page`,
`/history-page`; inclui `<link rel="stylesheet" href="/static/style.css">`;
título "Gestor de Banca"; idioma pt-BR; layout limpo (sem framework CSS externo,
CSS próprio simples em `static/style.css`, cores neutras, cards).

- `dashboard.html`: ao carregar, faz `fetch('/bankroll')` e `fetch('/history/stats')`
  e `fetch('/bankroll/stop-status')` e `fetch('/history/stats/by-tier')`; renderiza
  cards com: saldo atual, lucro/prejuízo acumulado, ROI%, maior sequência de perdas,
  taxa de acerto geral, taxa de acerto por tier (25/50/100), e um banner de alerta
  visível (vermelho) se `daily_limit_hit` ou `weekly_limit_hit` for true. Se banca
  não existe (404 de `/bankroll`), mostra mensagem "Configure sua banca" com link
  para `/bankroll-page`.
- `bankroll.html`: se banca não existe, formulário de `POST /bankroll/init`
  (banca inicial, % unidade, stop diário %, stop semanal %). Se já existe, mostra
  os valores atuais e um formulário de ajuste manual (`POST /bankroll/adjust`)
  com campo de nota.
- `generator.html`: formulário com select de tier (25x/50x/100x) e input de stake
  (sugerir automaticamente `stake = current_amount * unit_percent / 100` como
  valor default, buscado de `/bankroll`, mas editável). Botão "Gerar Múltipla"
  chama `POST /generator/preview` e renderiza: tabela das pernas (liga, evento,
  mercado, odd, probabilidade implícita %), odd total, **probabilidade combinada
  real** em destaque (ex: "Probabilidade real combinada: 8.3% — isso significa
  que, historicamente, esse tipo de combinação erra a maioria das vezes"),
  retorno potencial. Abaixo, texto fixo não removível: "⚠️ Isso NÃO é uma aposta
  segura. É uma sugestão baseada nas odds atuais. Você decide se aposta, e a
  aposta é sempre feita manualmente por você na casa de apostas." Botão
  "Confirmar (eu já apostei manualmente)" chama `POST /generator/confirm`.
  Também tem uma seção simples para `POST /odds/manual` (adicionar odds coletadas
  manualmente da Betano/Bet365) com select de liga (de `WHITELIST_LEAGUES` —
  pode hardcodar a mesma lista no JS ou buscar via um endpoint auxiliar; mais
  simples: hardcodar no template, já que é uma lista fixa) e de tipo de mercado.
- `history.html`: tabela de `GET /history` (data, tier, odd total, stake, status,
  retorno potencial, probabilidade combinada), expandível para ver pernas. Para
  bets "pending", formulário inline de settle (`POST /bets/{id}/settle`) com
  select won/lost/void.

`static/style.css`: CSS simples e limpo — variáveis de cor, cards com sombra leve,
tabelas legíveis, cores de status (verde=won, vermelho=lost, cinza=void, azul=pending),
responsivo básico (max-width no container).

## `README.md`

Explicar: o que é o projeto, como instalar (`pip install -r requirements.txt`),
como rodar (`uvicorn app.main:app --reload`, depois abrir `http://localhost:8000`),
como funciona a entrada de odds (manual por padrão; `ODDS_API_KEY` opcional para
The Odds API, com nota sobre cobertura limitada de mercados como cantos/dupla
chance dependendo do plano), e uma seção "⚠️ Regras do sistema" reafirmando os
4 itens não-negociáveis do topo deste documento. Adicionar nota de jogo
responsável (isso é uma ferramenta de gestão e organização, não uma garantia de
lucro; apostas esportivas envolvem risco de perda).
