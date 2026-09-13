"""Log em JSON, com o trace_id dentro — que é o que liga log a trace.

Por que JSON e não texto: o Loki guarda a linha inteira como está. Uma linha
humana obriga cada consulta a virar expressão regular, e a primeira mudança de
formato quebra todas de uma vez. Com JSON, `json | level="error"` funciona
sozinho.

**O nome do campo importa.** O datasource Loki deste cluster tem um campo
derivado com o padrão `"trace_id":"([a-f0-9]+)"`; é ele que transforma o log
num link clicável para o trace no Tempo. Renomear a chave para `traceId` ou
`trace-id` não dá erro em lugar nenhum — só desliga o link, silenciosamente.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime

try:  # a API vem junto com a auto-instrumentacao injetada no cluster
    from opentelemetry import trace as _trace
except ImportError:  # rodando local, sem OTel
    _trace = None


def _contexto_de_trace() -> dict[str, str]:
    """IDs do span atual, quando há um.

    Fora do cluster não há instrumentação e isto devolve vazio — o log continua
    saindo, sem os campos de correlação.
    """
    if _trace is None:
        return {}
    span = _trace.get_current_span()
    ctx = span.get_span_context() if span else None
    if not ctx or not ctx.is_valid:
        return {}
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }


class FormatadorJson(logging.Formatter):
    """Uma linha de log = um objeto JSON, sem quebra de linha no meio."""

    def format(self, record: logging.LogRecord) -> str:
        dados: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "app": os.getenv("APP_NAME", "demo-python"),
            "versao": os.getenv("APP_VERSION", "desenvolvimento"),
        }
        dados.update(_contexto_de_trace())

        # Campos extras passados via logger.info("...", extra={"campos": {...}})
        extras = getattr(record, "campos", None)
        if isinstance(extras, dict):
            dados.update(extras)

        if record.exc_info:
            dados["excecao"] = self.formatException(record.exc_info)

        # default=str para nao explodir com tipo nao serializavel — um log que
        # derruba a requisicao e pior do que um log impreciso.
        return json.dumps(dados, ensure_ascii=False, default=str)


def configurar_logs(nivel: str | None = None) -> None:
    """Põe o formatador JSON na raiz e faz o uvicorn usar o mesmo.

    Os loggers do uvicorn vêm com handler próprio e `propagate=False`: sem
    limpá-los, metade das linhas sai em JSON e metade em texto.
    """
    nivel = (nivel or os.getenv("LOG_LEVEL", "INFO")).upper()

    manipulador = logging.StreamHandler(sys.stdout)
    manipulador.setFormatter(FormatadorJson())

    raiz = logging.getLogger()
    raiz.handlers = [manipulador]
    raiz.setLevel(nivel)

    for nome in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(nome)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(nivel)


class RegistroDeRequisicao:
    """Middleware ASGI que loga uma linha por requisição.

    É ASGI puro, e não `@app.middleware("http")`, porque o middleware do
    Starlette consome o corpo da resposta para medir tamanho — aqui só se
    precisa do status e da duração.
    """

    def __init__(self, app, logger_nome: str = "demo-python.acesso"):
        self.app = app
        self.log = logging.getLogger(logger_nome)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inicio = time.perf_counter()
        status = {"codigo": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["codigo"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            ms = round((time.perf_counter() - inicio) * 1000, 2)
            self.log.info(
                "%s %s -> %s",
                scope.get("method", "?"),
                scope.get("path", "?"),
                status["codigo"],
                extra={
                    "campos": {
                        "metodo": scope.get("method"),
                        "caminho": scope.get("path"),
                        "status": status["codigo"],
                        "duracao_ms": ms,
                    }
                },
            )
