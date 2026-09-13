"""Aplicacao mínima para exercitar a esteira de CI/CD de ponta a ponta.

O front-end é renderizado no servidor, em Python, com Jinja2: não há bundler,
não há Node, não há passo de build de assets. Para um teste de fluxo isso é
proposital — o que se quer provar é o caminho commit → imagem → GitOps →
ArgoCD → canary, e cada peça a mais no meio é uma variável a mais quando algo
falha.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

INICIO = time.monotonic()

# A tag da imagem chega por variável de ambiente, escrita pelo chart a partir
# do values que o CI atualiza. É assim que a tela consegue dizer qual versão
# está respondendo — que é o ponto inteiro de um teste de canary.
VERSAO = os.getenv("APP_VERSION", "desenvolvimento")
COR = os.getenv("APP_COR", "#2d7ff9")

app = FastAPI(title="demo-python", docs_url="/api/docs", redoc_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def uptime_segundos() -> float:
    return round(time.monotonic() - INICIO, 1)


@app.get("/", response_class=HTMLResponse)
def pagina(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "versao": VERSAO,
            "cor": COR,
            "pod": socket.gethostname(),
            "uptime": uptime_segundos(),
        },
    )


@app.get("/api/info")
def info() -> JSONResponse:
    """O mesmo que a tela mostra, em JSON — para o teste de carga do canary."""
    return JSONResponse(
        {
            "versao": VERSAO,
            "pod": socket.gethostname(),
            "uptime_segundos": uptime_segundos(),
        }
    )


@app.get("/healthz", response_class=PlainTextResponse)
def saude() -> str:
    """Sonda de readiness e liveness.

    Deliberadamente burra: responde 200 enquanto o processo estiver de pé. Uma
    sonda que checa dependências derruba o pod quando o banco pisca, que é o
    oposto do que se quer.
    """
    return "ok"
