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

### 3. O `ci.yml`, chamando os workflows reutilizáveis

Três chamadas, nenhuma linha de YAML copiada:
[`python.yml`](https://github.com/slipalison/github-workflows) para lint e
testes, `build-push.yml` para a imagem, `deploy.yml` para escrever a tag no
GitOps.

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
