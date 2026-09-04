# Gestor de Banca + Gerador de Múltiplas

Ferramenta de gestão e organização de banca para apostas esportivas, com um
gerador de múltiplas ("acumuladores") baseado em odds que você mesmo cadastra
(ou importa, opcionalmente, do The Odds API). O sistema **sugere** combinações
de pernas a partir de odds cadastradas — ele nunca aposta por você e nunca se
conecta a nenhuma casa de apostas para efetuar apostas.

Este projeto é construído com **Python 3.11+, FastAPI, SQLAlchemy (SQLite) e
Jinja2** (templates renderizados no servidor, com JS vanilla via `fetch` nas
páginas). Não há build step nem npm — é só instalar as dependências Python e
rodar.

## Dois jeitos de tentar crescer a banca

1. **Gerador de Múltiplas (25x / 50x / 100x)**: uma única múltipla grande
   combinando várias pernas de odd baixa até atingir um multiplicador de odd
   alvo. Probabilidade real de bater tudo cai rápido conforme o tier sobe
   (ex.: ~3% pra 25x com pernas "quase certas") — é tudo ou nada numa única
   cartela.
2. **Ciclo de Juros Compostos** (`/cycle-page`): em vez de uma cartela
   gigante, uma SEQUÊNCIA de apostas simples (1 perna, odd baixa), onde o
   saldo TOTAL da banca é reinvestido a cada rodada vencida ("deixa rolar"),
   até acumular um lucro-alvo em R$ (ex.: R$25/50/100) — ou perder uma
   rodada, o que encerra o ciclo (já que 100% do saldo está em jogo a cada
   vez). A vantagem sobre a múltipla única: você pode encerrar o ciclo e
   guardar o lucro a qualquer momento, sem precisar bater tudo de uma vez.
   **Isso é bem mais arriscado que a unidade normal (2-3%) do resto do
   app** — a interface deixa esse risco sempre visível.

Os dois usam a mesma base de odds cadastradas (manual ou via API) e o mesmo
fluxo de "você aposta manualmente, depois volta e registra o resultado".

## O que o sistema faz

- Controla sua banca (saldo inicial, saldo atual, % de unidade de aposta,
  stop diário e semanal em %).
- Guarda um cache de odds (`odds_cache`) que você cadastra manualmente a
  partir do que vê na Betano/Bet365/etc., ou que opcionalmente é importado do
  The Odds API.
- Gera um preview de múltipla ("acumulador") para os tiers 25x / 50x / 100x,
  escolhendo pernas de menor odd primeiro (maior probabilidade implícita),
  sempre com no máximo 1 perna por evento/jogo.
- Ao confirmar, registra a intenção de aposta (`Bet` + `BetLeg`s) no seu
  histórico — a aposta em si você faz manualmente, fora do sistema, na casa
  de apostas.
- Depois que você aposta manualmente e sabe o resultado, você volta ao
  sistema e faz o "settle" (won/lost/void) para atualizar seu histórico e sua
  banca.
- Mostra estatísticas: taxa de acerto geral e por tier, maior sequência de
  perdas, ROI, e a comparação entre taxa de acerto real observada e a
  probabilidade combinada média estimada por tier.
- Desenha um gráfico de evolução do saldo da banca ao longo do tempo, a
  partir de cada evento registrado (depósito, ajuste, aposta liquidada).
- Mostra "odds ao vivo" no painel: um snapshot do cache de odds com um botão
  "Atualizar via API" e um indicador de **movimento** (▼ caiu / ▲ subiu / —
  igual) para cada perna, comparando a odd atual com a última vez que ela foi
  consultada. Isso funciona tanto pela integração opcional com a The Odds
  API quanto simplesmente reentrando manualmente uma odd que você já tinha
  cadastrado (a linha é atualizada, não duplicada, e o movimento fica
  registrado) — então dá pra acompanhar se uma odd está caindo mesmo sem
  `ODDS_API_KEY` configurada.
- Suporta jogos de **dias diferentes** (hoje, amanhã, ou qualquer data
  futura): ao cadastrar uma odd manual, preencha "Data e hora do jogo" — a
  tabela de odds em cache do Gerador passa a agrupar as pernas por dia
  ("Hoje", "Amanhã", "sex., 05/09", etc.), e o formulário de geração ganha um
  filtro "Dia do jogo" para montar a múltipla usando só as pernas daquele dia
  específico (em vez de misturar jogos de datas diferentes na mesma
  múltipla). O filtro compara instantes reais (não só a data em texto), então
  funciona corretamente mesmo com jogos à noite que cruzam a meia-noite UTC.
- Se o dia escolhido não tiver odds suficientes para fechar o tier pedido, o
  gerador **busca automaticamente nos próximos dias já cadastrados** (hoje →
  amanhã → depois de amanhã...) até conseguir montar a múltipla ou esgotar os
  dias disponíveis — sempre avisando claramente qual dia acabou sendo usado
  (nunca troca o dia "escondido").
- Se você ainda não cadastrou nenhuma odd, o Gerador mostra um aviso logo ao
  abrir a página (em vez de só falhar silenciosamente quando você clicar em
  "Gerar Múltipla"), explicando as duas formas de alimentar o sistema:
  entrada manual ou configurar a `ODDS_API_KEY`.
- A importação automática via The Odds API agora extrai cinco mercados:
  "menos de X.5 gols" e "dupla chance no favorito" (derivada do `h2h` com
  normalização correta de probabilidade — nunca gera uma odd abaixo de 1.0)
  para TODOS os jogos das ligas atualizadas (barato, endpoint em lote); e
  "ambas marcam - não", cantos e cartões para os **8 jogos mais próximos**
  de cada atualização (mercados "adicionais" da API, que só existem no
  endpoint por-jogo — bem mais caro em créditos, por isso limitado aos
  próximos jogos em vez de todos de uma vez). A mensagem do botão
  "Atualizar via API" mostra quantos créditos ainda restam no mês.
- Cada odd é validada antes de ser salva: uma odd decimal nunca pode ser
  menor que 1.0 (seria matematicamente impossível). Isso pegou um bug real
  na fórmula de derivação de dupla chance quando o favorito era muito forte
  — já corrigido.

## Como instalar

Pré-requisito: Python 3.11 ou superior.

```bash
cd banca-app
pip install -r requirements.txt
```

(Opcional, mas recomendado) copie o arquivo de exemplo de variáveis de
ambiente:

```bash
cp .env.example .env
```

## Como rodar

```bash
uvicorn app.main:app --reload
```

Depois abra **http://localhost:8000** no navegador. As páginas disponíveis
são:

- `/` — Dashboard (saldo, lucro/prejuízo, ROI, taxas de acerto, alertas de
  stop diário/semanal).
- `/bankroll-page` — Configurar/ajustar a banca.
- `/generator-page` — Cadastrar odds manuais e gerar/confirmar múltiplas.
- `/history-page` — Histórico de apostas e liquidação (settle) de pendentes.

Na primeira execução o banco de dados SQLite (`banca.db`) é criado
automaticamente na raiz do projeto (`Base.metadata.create_all` roda no
startup do FastAPI).

## Como funciona a entrada de odds

**Por padrão, tudo funciona 100% com entrada manual de odds.** Você olha a
odd que a casa de apostas está oferecendo (Betano, Bet365, etc.) e cadastra
ela em `/generator-page` (liga, evento, tipo de mercado, descrição da seleção
e a odd decimal). Isso é o suficiente para usar o gerador de múltiplas.

Opcionalmente, se você definir a variável de ambiente `ODDS_API_KEY` (veja
`.env.example`) com uma chave do [The Odds API](https://the-odds-api.com/)
(há um plano gratuito), o sistema também consegue importar odds
automaticamente para as ligas da whitelist através de `POST /odds/refresh`.

**Nota sobre cobertura limitada de mercados:** a importação automática via
The Odds API cobre bem mercados como `h2h` (1x2) e `totals` (mais/menos
gols), mas mercados mais específicos usados neste projeto — como "dupla
chance", "menos de X.5 cantos" ou "ambas marcam - não" — têm cobertura
variável ou inexistente dependendo do plano contratado na API e da liga. Por
isso, o parsing automático desses mercados é best-effort: quando a API não
oferece o mercado desejado, a importação simplesmente pula essa liga/mercado
sem quebrar o restante da atualização, e o cadastro manual continua sendo o
caminho mais confiável para os mercados de nicho. Sem `ODDS_API_KEY`
configurada, `POST /odds/refresh` não falha — apenas retorna `added: 0` com
uma mensagem avisando que a entrada manual deve ser usada.

## ⚠️ Regras do sistema

Estas regras são inegociáveis e valem em toda a stack (banco de dados,
serviços, endpoints e telas):

1. **Nunca existe uma "% de segurança" fixa ou inventada em lugar nenhum do
   código ou da interface.** Toda probabilidade exibida é `1 / odd_decimal`
   por perna, e a probabilidade da múltipla é o **produto** das
   probabilidades das pernas escolhidas. Isso fica visível no preview do
   gerador, na confirmação, no histórico e em qualquer card de "múltipla
   segura".
2. **O sistema nunca aposta sozinho em casa de apostas nenhuma.** Não existe
   nenhuma integração de login/sessão com Betano/Bet365, nenhum clique
   automático, nenhum envio de aposta a terceiros. O fluxo é sempre:
   sistema sugere → usuário decide → usuário aposta manualmente no
   app/site da casa → usuário volta e registra o resultado manualmente.
3. **Toda tela de múltipla "segura" mostra, ao lado do retorno potencial, a
   probabilidade combinada real** — de forma visível, nunca escondida ou em
   letra miúda.
4. **Pernas do mesmo jogo/evento nunca são combinadas juntas numa mesma
   múltipla** (correlação estatística invalidaria o cálculo de probabilidade
   por produto). Regra aplicada: no máximo 1 perna por `event_name` por
   múltipla.

## Jogo responsável

Este projeto é uma ferramenta de **gestão e organização** de banca e de
geração de sugestões de múltiplas com base em odds — **não é, e nunca será,
uma garantia de lucro**. Apostas esportivas envolvem risco real de perda de
dinheiro, e nenhuma "probabilidade combinada" alta muda esse fato: ela é
apenas o produto das probabilidades implícitas das odds escolhidas, não uma
previsão certeira do resultado. Aposte apenas valores que você pode perder,
defina e respeite os limites de stop diário/semanal configurados na sua
banca, e procure ajuda especializada se sentir que a aposta deixou de ser
uma atividade recreativa sob seu controle (no Brasil, o CVV — 188 — oferece
apoio emocional gratuito e sigiloso).
