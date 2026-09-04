"""
Configuração do banco de dados (SQLAlchemy + SQLite) para o Gestor de Banca.

Este módulo é o contrato compartilhado de acesso a dados: `Base` é usada por
`app.models` para declarar as tabelas, e `get_db` é usada como dependência do
FastAPI (`Depends(get_db)`) em todos os routers.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

SQLALCHEMY_DATABASE_URL = "sqlite:///./banca.db"

# `check_same_thread=False` é necessário porque o SQLite por padrão só permite
# uso da conexão pela thread que a criou, e o FastAPI pode atender requisições
# em threads diferentes.
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency generator padrão do FastAPI: abre uma sessão, entrega (yield)
    para o endpoint usar, e garante o fechamento no final (mesmo em caso de erro)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
