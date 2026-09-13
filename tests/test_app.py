"""Testes do que a esteira precisa garantir antes de construir a imagem.

Não testam o CSS nem o texto da página: testam os contratos de que o cluster
depende — a sonda responder 200, a página renderizar, e a versão exibida ser a
que veio do ambiente. Um teste que falha por causa de uma vírgula no HTML é um
teste que o time aprende a ignorar.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

cliente = TestClient(app)


def test_sonda_responde_ok():
    r = cliente.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_pagina_renderiza():
    r = cliente.get("/")
    assert r.status_code == 200
    assert "demo-python" in r.text
    assert "text/html" in r.headers["content-type"]


def test_info_traz_versao_e_pod():
    dados = cliente.get("/api/info").json()
    assert set(dados) == {"versao", "pod", "uptime_segundos"}
    assert dados["pod"]
    assert isinstance(dados["uptime_segundos"], float)


def test_versao_vem_do_ambiente(monkeypatch):
    """O canary só é observável se a versão exibida for a da imagem.

    Se esta asserção quebrar, a tela passa a mentir durante um rollout — e um
    canary que não se enxerga não serve para decidir nada.
    """
    monkeypatch.setenv("APP_VERSION", "sha-abc1234")
    assert cliente.get("/api/info").json()["versao"] == "sha-abc1234"
    assert "sha-abc1234" in cliente.get("/").text


def test_metricas_expostas():
    """O Alloy raspa /metrics por anotacao; se o endereco sumir, ninguem avisa."""
    r = cliente.get("/metrics")
    assert r.status_code == 200
    assert "demo_paginas_servidas_total" in r.text
    assert "demo_render_segundos" in r.text


def test_erro_proposital_devolve_500():
    assert cliente.get("/api/erro").status_code == 500


def test_log_sai_em_json_com_os_campos_certos(capsys):
    """O log e JSON de uma linha so, e o campo de correlacao chama `trace_id`.

    O nome importa: o datasource Loki deste cluster liga log a trace por um
    campo derivado com o padrao `"trace_id":"([a-f0-9]+)"`. Renomear a chave
    nao da erro em lugar nenhum — so desliga o link.
    """
    import json
    import logging

    from app.observabilidade import FormatadorJson

    registro = logging.LogRecord(
        name="teste",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="oi %s",
        args=("mundo",),
        exc_info=None,
    )
    registro.campos = {"extra": 1}
    linha = FormatadorJson().format(registro)

    assert "\n" not in linha
    dados = json.loads(linha)
    assert dados["msg"] == "oi mundo"
    assert dados["level"] == "info"
    assert dados["extra"] == 1
    assert {"ts", "logger", "app", "versao"} <= set(dados)
