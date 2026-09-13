"""Aplicacao mínima para exercitar a esteira de CI/CD de ponta a ponta.

O front-end é renderizado no servidor, em Python, com Jinja2: não há bundler,
não há Node, não há passo de build de assets. Para um teste de fluxo isso é
proposital — o que se quer provar é o caminho commit → imagem → GitOps →
ArgoCD → canary, e cada peça a mais no meio é uma variável a mais quando algo
falha.

Os três sinais de observabilidade, e de onde cada um vem:

  métricas  do sidecar do Istio (taxa, erro, latência) sem o app fazer nada,
            mais as de negócio em /metrics, raspadas pelo Alloy por anotação;
  logs      daqui, em JSON, com trace_id — ver `observabilidade.py`;
  traces    da auto-instrumentação do OpenTelemetry, injetada pelo operador
            no cluster. O app não importa SDK nenhum para isso.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app.observabilidade import RegistroDeRequisicao, configurar_logs

configurar_logs()
log = logging.getLogger("demo-python")

INICIO = time.monotonic()


def versao() -> str:
    """Tag da imagem, lida A CADA CHAMADA e não no import.

    O chart escreve APP_VERSION a partir do values que o CI atualiza, e é assim
    que a tela diz qual versão respondeu — o ponto inteiro de um canary.

    Ler no import parece equivalente e não é: obriga o teste a recarregar o
    módulo para exercitar outra versão, e recarregar registra as métricas do
    Prometheus de novo, o que estoura com `Duplicated timeseries`. Uma função
    resolve os dois problemas e não custa nada.
    """
    return os.getenv("APP_VERSION", "desenvolvimento")


def cor() -> str:
    return os.getenv("APP_COR", "#2d7ff9")


# Métricas de negócio. As de requisição (RED) já vêm do Istio; duplicá-las aqui
# só criaria dois números para a mesma pergunta, e eles divergem.
PAGINAS = Counter("demo_paginas_servidas_total", "Páginas renderizadas", ["rota"])
RENDER = Histogram(
    "demo_render_segundos",
    "Tempo de renderização do template",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5),
)

app = FastAPI(title="demo-python", docs_url="/api/docs", redoc_url=None)
app.add_middleware(RegistroDeRequisicao)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

log.info(
    "aplicacao iniciada",
    extra={"campos": {"versao": versao(), "pod": socket.gethostname()}},
)


def uptime_segundos() -> float:
    return round(time.monotonic() - INICIO, 1)


@app.get("/", response_class=HTMLResponse)
def pagina(request: Request) -> HTMLResponse:
    with RENDER.time():
        resposta = templates.TemplateResponse(
            request,
            "index.html",
            {
                "versao": versao(),
                "cor": cor(),
                "pod": socket.gethostname(),
                "uptime": uptime_segundos(),
            },
        )
    PAGINAS.labels(rota="/").inc()
    return resposta


@app.get("/api/info")
def info() -> JSONResponse:
    """O mesmo que a tela mostra, em JSON — para o teste de carga do canary."""
    return JSONResponse(
        {
            "versao": versao(),
            "pod": socket.gethostname(),
            "uptime_segundos": uptime_segundos(),
        }
    )


@app.get("/metrics")
def metricas() -> Response:
    """Métricas de negócio, no formato que o Alloy raspa.

    A anotação `prometheus.io/scrape` no values do GitOps é o que faz alguém
    vir buscar; sem ela este endereço existe e ninguém olha.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/erro")
def erro_proposital() -> JSONResponse:
    """Devolve 500 de propósito.

    Existe para dois testes que não dá para fazer com um app que nunca falha:
    ver o log de erro chegar no Loki com o trace_id certo, e fazer a análise do
    canary reprovar de verdade, em vez de forçar o limiar.
    """
    log.error(
        "erro proposital para teste de observabilidade",
        extra={"campos": {"rota": "/api/erro"}},
    )
    return JSONResponse({"erro": "proposital"}, status_code=500)


@app.get("/healthz", response_class=PlainTextResponse)
def saude() -> str:
    """Sonda de readiness e liveness.

    Deliberadamente burra: responde 200 enquanto o processo estiver de pé. Uma
    sonda que checa dependências derruba o pod quando o banco pisca, que é o
    oposto do que se quer.
    """
    return "ok"
