"""Testes do que a esteira precisa garantir antes de construir a imagem.

Não testam o CSS nem o texto da página: testam os contratos de que o cluster
depende — a sonda responder 200, a página renderizar, e a versão exibida ser a
que veio do ambiente. Um teste que falha por causa de uma vírgula no HTML é um
teste que o time aprende a ignorar.
"""

from __future__ import annotations

import importlib

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
    from app import main

    importlib.reload(main)
    assert main.VERSAO == "sha-abc1234"
    assert TestClient(main.app).get("/api/info").json()["versao"] == "sha-abc1234"

    monkeypatch.delenv("APP_VERSION")
    importlib.reload(main)
