# demo-python

Aplicação mínima em Python — FastAPI com front-end renderizado no servidor, sem
JavaScript de build — que existe para **exercitar a esteira de CI/CD de ponta a
ponta**: commit → testes → imagem → GitOps → ArgoCD → canary.

A tela mostra a versão da imagem e o nome do pod que respondeu. Durante um
canary, recarregar troca o pod, e o selo muda de versão conforme o peso do
tráfego — que é a forma mais direta de ver o rollout acontecendo.

```
GET /            página (HTML renderizado por Jinja2)
GET /api/info    o mesmo em JSON — útil para gerar carga durante o canary
GET /healthz     sonda de readiness e liveness
GET /api/docs    OpenAPI
```

---

## Rodar localmente

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
```

Testes e lint, os mesmos que o CI roda:

```bash
ruff check . && ruff format --check .
pytest -q
```

---

## O que foi preciso para isto chegar ao cluster

Esta é a parte que interessa, e a razão de o repositório existir. Nada aqui é
específico de Python: é o contrato da esteira.

### 1. A aplicação precisa caber nas regras do cluster

Não é o cluster que se adapta ao app.

| Regra | Onde ela mora | O que o app faz |
|---|---|---|
| Roda como **não-root**, UID 65532 | `securityContext` do chart | Nada escreve em `$HOME`; nenhum arquivo tem dono fixo |
| Sistema de arquivos **somente leitura** | `readOnlyRootFilesystem: true` | `PYTHONDONTWRITEBYTECODE=1`, nada gravado em disco |
| Porta **acima de 1024** | Pod não-root não abre porta privilegiada | Uvicorn na `8080`, e `port: 8080` no values |
| Imagem com **tag imutável** | Política do cluster e schema do chart | O CI usa `sha-<7>`; `latest` é recusada por schema |
| `requests` e `limits` declarados | Política do cluster | Definidos no values |
| Uma sonda HTTP que responda rápido | `readinessProbe`/`livenessProbe` | `/healthz`, que só diz que o processo está de pé |

O `Dockerfile` é multi-stage por causa da primeira linha dessa tabela: as
dependências são instaladas numa camada que some, e a imagem final não tem
`pip` nem compilador.

**A sonda é deliberadamente burra.** Ela não verifica banco nem serviço
externo: uma sonda que checa dependências derruba o pod quando o banco pisca —
o oposto do que se quer, porque aí um problema de fora vira um problema de
dentro.

### 2. O diretório do app no repositório de GitOps

O deploy **falha de propósito** se `apps/demo-python/values.yaml` não existir.
Quem define o que uma aplicação é — porta, réplicas, limites, hostname — não é
o pipeline.

```yaml
name: demo-python
owner: alison
image:
  repository: ghcr.io/slipalison/demo-python
  tag: sha-ffdb27c        # o CI reescreve esta linha a cada deploy
port: 8080
replicas: 2
probes:
  readiness: { path: /healthz }
  liveness:  { path: /healthz }
```

O resto — Rollout com canary, Service, VirtualService, NetworkPolicy,
AuthorizationPolicy — vem do chart
[`app`](https://github.com/slipalison/helm-charts).

### 3. O `ci.yml`, chamando a esteira

**Uma** chamada. O `ci.yml` deste repositório tem vinte linhas de configuração e
nenhuma de orquestração — ela mora em
[`slipalison/github-workflows`](https://github.com/slipalison/github-workflows).

```
        ┌─ qualidade   ruff, pytest, piso de cobertura em 80%
        ├─ imagem      constrói → varre → publica (nessa ordem)
push ───┼─ sonar       análise + Quality Gate
        └─ seguranca   7 jobs: Gitleaks, TruffleHog, Semgrep,
        │              CodeQL, SCA, IaC, SBOM
        └──────────────────────────────────► publicar  (só na main)
```

Tudo isso corre **ao mesmo tempo**; só o `publicar` espera.

Antes eram três etapas em fila — `testes` → `imagem` → `publicar` — e a do meio
esperava por um motivo que não se sustenta: `docker build` não depende de
`pytest`. O que não pode acontecer é imagem de código reprovado **chegar ao
cluster**, e quem impede isso é o `publicar`. Uma tag `sha-<commit>` no registro
de um commit que falhou não machuca ninguém: o GitOps nunca a aponta.

Medido aqui em 2026-09-13, e o número honesto não é o que a intuição sugere:

| | Jobs | Relógio |
|---|---|---|
| Esteira antiga, em fila | 3 | **78s** |
| Esteira nova, em paralelo | 13 | **100s** |

A nova é 22 segundos **mais lenta** — e faz sete varreduras de segurança, Sonar
e piso de cobertura que a antiga não fazia. Em fila, esses mesmos jobs dariam
~5 minutos. O paralelismo não encurtou o que já existia; absorveu o que faltava.

O job da imagem subiu de 43s para 79s de propósito: agora ele constrói, varre e
só então publica.

**O que a esteira nova pegou no primeiro run**, e vale como amostra do que ela
faz: `DS-0002` no `Dockerfile` — nenhum `USER`. Dentro deste cluster não tinha
efeito (o Rollout já impõe `runAsNonRoot` e UID 65532, e o Pod Security
`restricted` recusaria o contrário); fora dele tinha, porque `docker run` desta
imagem em qualquer outra máquina subia como root. Corrigido na origem, com
`USER 65532` no Dockerfile, e não por exceção no `.trivyignore` — uma lista de
exceção que cresce é um portão desligado com passos a mais.

**Pendência:** falta o secret `SONAR_TOKEN` aqui, então o Sonar está com
`sonar_exigir_token: false` e cada run emite um `::warning::` dizendo isso — em
vez de um job verde que não analisou nada. Ligar exige, nesta ordem: desligar a
*Automatic Analysis* no SonarCloud (é mutuamente exclusiva com a análise por CI)
e criar o secret.

### 4. A credencial para escrever no GitOps

Uma **chave de deploy com escrita**, criada no repositório de GitOps e guardada
como segredo aqui:

```bash
ssh-keygen -t ed25519 -N "" -f chave -C "ci-deploy-gitops"
gh repo deploy-key add chave.pub -R slipalison/homelab-gitops --allow-write   --title "CI: escrita da tag de imagem (demo-python)"
gh secret set GITOPS_SSH_KEY -R slipalison/demo-python < chave
shred -u chave chave.pub
```

Por que chave e não token de conta: ela alcança **um** repositório e mais nada,
não depende de usuário nenhum, e se revoga num clique. Um token fine-grained
também funciona (`GITOPS_TOKEN`), mas vive preso a uma conta — se ela sair ou o
token expirar, todo app que o usa para de publicar no mesmo dia.

O `GITHUB_TOKEN` automático não serve para nenhum dos dois: ele não alcança
outro repositório.

### 5. O pacote da imagem acessível ao cluster

O cluster puxa a imagem sem credencial. Com o pacote privado no GHCR, o pod
fica em `ImagePullBackOff` — e a mensagem não diz "falta credencial", diz que
não encontrou a imagem, o que manda quem está depurando para o lado errado.

*Package settings → Change visibility → Public*, uma vez.

---

## Os três sinais, e como conferir cada um

| Sinal | De onde vem | Como conferir |
|---|---|---|
| **Logs** | Deste código, em JSON, uma linha por requisição | Grafana → Explore → Loki: `{namespace="demo-python"} \| json \| level="error"` |
| **Traces** | SDK do OpenTelemetry, ligado em `observabilidade.py` | Grafana → Explore → Tempo → Search → service `demo-python` |
| **Métricas** | Sidecar do Istio (taxa, erro, latência) + `/metrics` do app | Grafana → Explore → Mimir: `sum(demo_paginas_servidas_total)` |

**O log é JSON por decisão, não por estética.** O Loki guarda a linha como
está; uma linha humana obriga cada consulta a virar expressão regular, e a
primeira mudança de formato quebra todas de uma vez.

**E o campo de correlação tem de se chamar `trace_id`.** O datasource Loki
deste cluster liga log a trace por um campo derivado com o padrão
`"trace_id":"([a-f0-9]+)"`. `traceId` ou `trace-id` não dão erro em lugar
nenhum — só desligam o link, silenciosamente.

Uma linha real, do cluster:

```json
{"ts":"2026-09-13T02:49:07.044+00:00","level":"info","logger":"demo-python.acesso",
 "msg":"GET /api/info -> 200","app":"demo-python","versao":"sha-22260d5",
 "trace_id":"fddd5ee8709095881a59c56e62fe7b33","span_id":"7aa4743119188a72",
 "metodo":"GET","caminho":"/api/info","status":200,"duracao_ms":0.61}
```

`/api/erro` devolve 500 de propósito: sem uma rota que falha não dá para ver um
log de erro chegar com o `trace_id` certo, nem para fazer a análise do canary
reprovar de verdade.

### Duas coisas que só apareceram ligando isto de verdade

**1. A auto-instrumentação do operador não funcionava, e falhava calada.** O
webhook de admissão do `opentelemetry-operator` estava com `bad certificate` e
`failurePolicy: Ignore`: o pod nascia sem instrumentação e nada no cluster
reclamava. A causa é o chart gerar a CA no `helm template` — o ArgoCD renderiza
a cada sync, então cada sync inventa uma CA nova — enquanto o `caBundle` do
webhook fica congelado por um `ignoreDifferences`.

Não tem conserto com ArgoCD: a saída oficial é o `lookup` do Helm, que o ArgoCD
não tem. **O operador foi removido do cluster em 2026-09-13** (ADR-001 em
`helm-charts/README.md`). O que ele entregava de concreto era apontar o OTLP
para o Alloy e nomear o serviço; isso são quatro variáveis de ambiente, e o
chart `app` 0.2.0 passou a escrevê-las:

```
OTEL_EXPORTER_OTLP_ENDPOINT   http://alloy.observability.svc.cluster.local:4318
OTEL_EXPORTER_OTLP_PROTOCOL   http/protobuf
OTEL_SERVICE_NAME             demo-python
OTEL_RESOURCE_ATTRIBUTES      service.version=<tag>,...
```

Por isso `configurar_traces()` não tem endereço nenhum escrito: o SDK lê essas
variáveis sozinho, e rodando fora do cluster elas não existem — o tracing
simplesmente não liga, sem erro e sem coletor para procurar.

**2. A NetworkPolicy do chart bloqueava quem vem buscar as métricas.** O chart
oferecia a anotação `prometheus.io/scrape` e, ao mesmo tempo, só permitia
entrada do namespace do gateway — e o Alloy vive em `observability`. O sintoma
é discreto: o alvo aparece em `up` **com valor 0** e nenhuma métrica chega.
Corrigido no chart `app` 0.1.4, liberando a `15020` (o endpoint mesclado do
sidecar, para onde o próprio istiod reescreve a anotação).

---

## O que quebrou na primeira tentativa

Seis coisas, e nenhuma delas era o código do app. Ficam aqui porque a próxima
aplicação vai tropeçar nas mesmas.

| # | Sintoma | Causa |
|---|---|---|
| 1 | `startup_failure`, sem log de passo | `aquasecurity/trivy-action@0.28.0` **não existe** — as tags têm prefixo `v`. O workflow nunca tinha rodado |
| 2 | `startup_failure` de novo | `The workflow is requesting 'packages: write', but is only allowed 'packages: read'`. Quem chama precisa conceder a permissão no job |
| 3 | `startup_failure` uma terceira vez | O contexto `secrets` **não existe em `if:` de passo** (`Unrecognized named-value: 'secrets'`) |
| 4 | Job da imagem vermelho | `cannot find ignorefile '.trivyignore'` — o workflow exigia um arquivo que o repositório não tem |
| 5 | Trivy derrubando o build | Vulnerabilidade **real**: `starlette` 0.49.3 com CVE-2026-48818, e 3 CRITICAL de pacote de sistema na imagem base |
| 6 | `Rollout` preso em `Degraded` | A tag de placeholder no values (`sha-0000000`) não existia; o ReplicaSet estável nasceu quebrado e o Rollout não promove sobre um estável doente |

As três primeiras têm a mesma característica desagradável: **`startup_failure`
não produz log de passo nenhum**. A mensagem existe só na tela do run — não sai
em `gh run view --log`, nem em `--log-failed`.

E a de número 5 é a esteira funcionando: o portão barrou a imagem antes de ela
chegar ao cluster. `fastapi` puxava `starlette` 0.49.3 sozinho, com SSRF e
roubo de credencial NTLM por caminho UNC. Depois de fixar a versão e atualizar
os pacotes do sistema: **25 vulnerabilidades → 3, zero CRITICAL**.

---

## O que mudou no resto do projeto por causa deste app

Quatro coisas que não existiam e faltaram na primeira tentativa:

1. **`python.yml`** nos workflows reutilizáveis. Só havia .NET, e um app em
   Python teria de trazer o próprio YAML — exatamente o que aqueles workflows
   existem para evitar.
2. **`deploy.yml` aceita chave de deploy**, não só token de conta. Uma chave
   com escrita criada no próprio repositório de GitOps alcança aquele
   repositório e mais nada, não depende de conta nenhuma e se revoga num
   clique. É o que este app usa.
3. **`APP_VERSION` injetado pelo chart** (`app` 0.1.2). A aplicação não tinha
   como saber a própria versão, e um canary que não se enxerga não serve para
   decidir nada.
4. **Um `envFrom` só, no chart.** `envFrom` do values e `envFrom` do banco eram
   dois blocos independentes: um app com os dois renderizava a chave duas vezes
   no mesmo container, o último vencia e o primeiro sumia sem erro nenhum.

---

## Depois do deploy

```bash
# a tag chegou ao GitOps?
git -C homelab-gitops log --oneline -3 -- apps/demo-python/values.yaml

# o ArgoCD sincronizou?
kubectl -n argocd get app demo-python

# o canary está em que passo?
kubectl argo rollouts get rollout demo-python -n demo-python
```

Durante o canary, `curl` repetido em `/api/info` mostra a proporção entre as
versões — e, de quebra, gera o tráfego de que a análise precisa para concluir.
