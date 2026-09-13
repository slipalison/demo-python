# Multi-stage: as dependencias sao instaladas numa camada que some, e so o que
# elas produzem vai para a imagem final. Reduz tamanho e tira o pip do runtime.
FROM python:3.13-slim AS dependencias

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/instalado -r requirements.txt

# ------------------------------------------------------------------------------
FROM python:3.13-slim

# O cluster roda o pod com runAsNonRoot e UID 65532 — vindo do chart, nao daqui.
# A imagem precisa funcionar com QUALQUER uid: nada de escrever em $HOME, nada
# de arquivo com dono fixo. Por isso o app fica em /app com permissao de
# leitura para todos, e nao ha nada para escrever em disco.
# Pacotes do sistema atualizados. A imagem base do Python e reconstruida com
# menos frequencia do que as correcoes do Debian saem: no primeiro build deste
# repositorio o portao do Trivy barrou 3 CRITICAL, todas de pacote de sistema e
# todas ja corrigidas no repositorio do Debian (perl-base, entre outras).
#
# O custo e a reprodutibilidade: duas construcoes da mesma tag podem trazer
# pacotes diferentes. Para uma aplicacao que se reconstroi a cada commit, e uma
# troca boa — o contrario significa publicar CVE conhecida de proposito.
RUN apt-get update  && apt-get upgrade -y --no-install-recommends  && rm -rf /var/lib/apt/lists/*

COPY --from=dependencias /instalado /usr/local
WORKDIR /app
COPY app ./app

# O chart tambem monta o sistema de arquivos como somente leitura em alguns
# casos; manter o Python sem escrever .pyc evita surpresa.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 8080 e nao 80: porta abaixo de 1024 exige privilegio que um pod nao-root nao
# tem. O values do GitOps repete este numero em `port`.
EXPOSE 8080

# Um worker so: o Rollout escala por replicas, nao por processo. Dois niveis de
# escala e um a mais para entender quando algo esta lento.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
