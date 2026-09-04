"""
Autenticação HTTP Basic para proteger o app quando ele for exposto na
internet (ex.: deploy no Render) — sem isso, qualquer pessoa com o link
conseguiria ver e alterar sua banca real.

Localmente (via `iniciar.bat`, sem `APP_USERNAME`/`APP_PASSWORD` definidas no
`.env`), a autenticação fica DESLIGADA automaticamente — não faz sentido
pedir senha pra acessar seu próprio computador. Ela só passa a ser exigida
quando essas duas variáveis estiverem configuradas (é isso que o deploy no
Render precisa fazer).
"""

from __future__ import annotations

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Dependency global (aplicada a todas as rotas em `app.main`). Compara
    usuário/senha com `secrets.compare_digest` (evita timing attack). Se
    `APP_USERNAME`/`APP_PASSWORD` não estiverem definidas no ambiente, a
    checagem é pulada por completo — uso local sem senha continua igual.
    """
    expected_username = os.environ.get("APP_USERNAME")
    expected_password = os.environ.get("APP_PASSWORD")

    if not expected_username or not expected_password:
        return  # autenticação desligada — uso local, sem credenciais configuradas

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciais inválidas ou ausentes.",
        headers={"WWW-Authenticate": "Basic"},
    )

    if credentials is None:
        raise unauthorized

    valid_username = secrets.compare_digest(credentials.username, expected_username)
    valid_password = secrets.compare_digest(credentials.password, expected_password)
    if not (valid_username and valid_password):
        raise unauthorized
