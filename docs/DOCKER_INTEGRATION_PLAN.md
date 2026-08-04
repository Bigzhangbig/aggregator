# 多平台多架构 Docker 镜像 + Actions 整合计划

## 目标

构建一个**多平台/多架构 docker 镜像**，承载整个 aggregator 项目的所有 GitHub Actions 逻辑，让 5 个 workflows 都通过这个镜像运行，而非各 action 自行 `pip install + python xxx.py`。带来：

1. **统一环境**：依赖、二进制、Python 版本完全锁定，CI 与本地一致
2. **多架构支持**：linux/amd64 + linux/arm64 同时构建（Graviton runners / Apple Silicon 本地都能跑）
3. **构建提速**：依赖一次装好，每次 action 启动秒级（vs 每次 ~30s `pip install`）
4. **actions 整合**：单一镜像 + ENTRYPOINT 参数化，减少 5 个 workflow 的重复 setup 步骤

---

## 现状盘点

### 现有 Dockerfile 局限

```dockerfile
FROM python:3.12.3-slim
ENV GIST_PAT="" GIST_LINK="" CUSTOMIZE_LINK=""
ARG PIP_INDEX_URL="https://pypi.org/simple"
WORKDIR /aggregator
COPY requirements.txt /aggregator
COPY subscribe /aggregator/subscribe
COPY clash/clash-linux-amd clash/Country.mmdb /aggregator/clash
COPY subconverter /aggregator/subconverter
RUN rm -rf subconverter/subconverter-darwin-arm \
    && rm -rf subconverter/subconverter-linux-arm \
    && rm -rf subconverter/subconverter-windows.exe
RUN pip install -i ${PIP_INDEX_URL} --no-cache-dir -r requirements.txt
CMD ["python", "-u", "subscribe/collect.py", "--all", "--overwrite", "--skip"]
```

**问题**：
- ❌ 只 `linux/amd64`，不支持 `arm64`
- ❌ CMD 硬编码 collect.py，process.py / checkin.yml 跑不了这个镜像
- ❌ `rm -rf` 跨平台删除策略不通用
- ❌ 没有镜像标签规范、没有镜像仓库配置
- ❌ Dockerfile 未参与 CI 构建（GitHub Actions 直接 `pip install`，不用镜像）

### 5 个 actions 现状

| Workflow | Python 环境 | 依赖安装 | 二进制 | 实际跑 |
|----------|------------|---------|--------|--------|
| process.yaml | actions/setup-python@v5 + pip install | 每次 | clash-linux-amd + subconverter-linux-amd（仓库已有）| `python process.py` |
| collect.yaml | 同上 | 每次 | 同上 | `python collect.py --all --overwrite --skip` |
| refresh.yaml | 同上 | 每次 | 同上 | `python collect.py --all --refresh --overwrite --skip` |
| checkin.yml | 同上 | 每次 | **不需要** | `python ./.github/actions/checkin/universal.py` |
| delete.yaml | 无 Python | 无 | 无 | GitRML/delete-workflow-runs action |

**重复的 setup 步骤**（5 个 workflow 都有）：
- `actions/checkout@v4`
- `actions/setup-python@v5`
- `pip install -r requirements.txt`
- chmod clash/subconverter 二进制（utils.chmod 会处理）

---

## 第一阶段：多平台多架构镜像

### 1.1 多架构目标

- `linux/amd64`（x86_64，GitHub Actions ubuntu-latest 默认）
- `linux/arm64`（ARM64，Apple Silicon 本地 + Graviton runners）
- 暂不支持 `linux/386` `windows/*` `darwin/*`（项目以 linux 服务端为主）

### 1.2 构建工具

- `docker buildx`（Docker 23.0+ 内置，支持 `--platform` 多架构并行构建）
- 多架构清单（manifest）：`docker manifest` 或 buildx 自动生成

### 1.3 Dockerfile 改造

```dockerfile
# syntax=docker/dockerfile:1.7
FROM --platform=$BUILDPLATFORM python:3.12-slim AS builder

ARG TARGETPLATFORM
ARG TARGETARCH
ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /aggregator

# 依赖先行（缓存优化）
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

# 项目文件
COPY . .

# 删除与目标架构不匹配的二进制约束
RUN set -eux; \
    case "$TARGETARCH" in \
        amd64) \
            rm -f clash/clash-darwin-* clash/clash-linux-arm clash/clash-windows-amd.exe \
                  subconverter/subconverter-darwin-* subconverter/subconverter-linux-arm subconverter/subconverter-windows-amd.exe; \
            ;; \
        arm64) \
            rm -f clash/clash-darwin-amd clash/clash-linux-amd clash/clash-windows-amd.exe \
                  subconverter/subconverter-darwin-amd subconverter/subconverter-linux-amd subconverter/subconverter-windows-amd.exe; \
            ;; \
        *) echo "Unsupported arch: $TARGETARCH"; exit 1 ;; \
    esac

# 设置入口
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["collect", "--all", "--overwrite", "--skip"]
```

### 1.4 entrypoint.sh（参数化入口）

```bash
#!/usr/bin/env bash
set -e

CMD="${1:-collect}"
shift || true

cd /aggregator/subscribe

case "$CMD" in
  process)  exec python -u process.py "$@" ;;
  collect)  exec python -u collect.py "$@" ;;
  refresh)  exec python -u collect.py --all --refresh --overwrite --skip "$@" ;;
  checkin)  exec python -u /aggregator/.github/actions/checkin/universal.py "$@" ;;
  delete)
    # 用 curl 调 GitHub API 清旧 run（替代 GitRML action）
    exec bash /aggregator/.github/actions/delete/cleanup.sh "$@"
    ;;
  *) echo "Unknown command: $CMD"; exit 2 ;;
esac
```

### 1.5 镜像仓库选择

| 选项 | 优势 | 劣势 |
|------|------|------|
| **GHCR**（ghcr.io/wzdnzd/aggregator） | 与 GitHub 集成；免费；gh 自动登录 | 公开镜像 |
| Docker Hub | 用户熟 | 速率限制、需另注册 |
| 私有 GHCR | 适合含 secret 的 build args | 配置稍复杂 |

**推荐 GHCR 公开**（aggregator 本身就是公开仓库，镜像公开无妨）。

---

## 第二阶段：actions 整合

### 2.1 整合后的 workflow 模板

```yaml
jobs:
  run:
    runs-on: ubuntu-latest  # 或 ubuntu-22.04-arm（arm64 runner）
    container:
      image: ghcr.io/wzdnzd/aggregator:latest
      credentials:
        username: ${{ github.actor }}
        password: ${{ secrets.GITHUB_TOKEN }}
    steps:
      - uses: actions/checkout@v4   # 仍需 checkout 让镜像内看到最新源码（覆盖 build 时拷贝的旧版）
      - run: aggregator collect --all --overwrite --skip   # 用 ENTRYPOINT 的 aggregator 别名或直接调入口
```

但用 GHCR 公共镜像有个 **版本滞后** 问题：镜像里的代码是 build 时的快照，CI 跑时 `git checkout` 会拉新代码覆盖镜像里的旧版，导致镜像里的依赖/二进制/代码版本错位。

**两种解决方案**：

#### 方案 A：镜像只装"环境"（依赖+二进制），代码每次 checkout 覆盖

```dockerfile
# 镜像只装 pip 依赖和二进制
FROM python:3.12-slim
RUN pip install -r requirements.txt
COPY clash/ /aggregator/clash/
COPY subconverter/ /aggregator/subconverter/
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
# 项目源码 subscribe/ 不 COPY，由 actions/checkout 在挂载时覆盖
```

**优势**：镜像小（无源码）；代码永远最新
**劣势**：依赖更新需要重建镜像，但有缓存层影响不大

#### 方案 B：镜像带完整代码，actions 不 checkout

```yaml
container:
  image: ghcr.io/wzdnzd/aggregator:${{ github.sha }}   # 每次 commit 构建新 tag
steps:
  - run: aggregator collect --all --overwrite --skip
```

**优势**：完全自包含，可复现
**劣势**：每次 push 都要 build，CI 时间翻倍

**推荐方案 A**（依赖稳定时重建频率低，代码流动不影响镜像）

### 2.2 5 个 workflows 改造对照

#### process.yaml（简化）

```yaml
name: Process
on: { schedule: [...], workflow_dispatch: }
jobs:
  process:
    runs-on: ubuntu-latest
    container:
      image: ghcr.io/wzdnzd/aggregator:latest
    steps:
      - uses: actions/checkout@v4
      - env:
          SUBSCRIBE_CONF: ${{ secrets.SUBSCRIBE_CONF }}
          PUSH_TOKEN: ${{ secrets.PUSH_TOKEN }}
          SKIP_ALIVE_CHECK: ${{ vars.SKIP_ALIVE_CHECK }}
        run: aggregator process --overwrite
```

**减少的步骤**：setup-python、pip install（节省 ~30s）

#### collect.yaml / refresh.yaml 类似，把 `python collect.py ...` 改成 `aggregator collect ...`

#### checkin.yml

```yaml
container:
  image: ghcr.io/wzdnzd/aggregator:latest
steps:
  - uses: actions/checkout@v4
  - run: aggregator checkin
```

`universal.py` 已是独立脚本，移入镜像后不用 checkout 也能跑（但仍 checkout 让最新源码生效）

#### delete.yaml

可以**完全不依赖镜像**，继续用 `GitRML/delete-workflow-runs` action（这是 GitHub 官方生态，与项目代码无关）。或方案 A 集成进镜像用 curl 调 API。

**建议保留 GitRML 方案**——更简洁。

---

## 第三阶段：CI 构建镜像

### 3.1 新增 workflow：`.github/workflows/docker.yml`

```yaml
name: Build and Push Docker Image
on:
  push:
    branches: [main]
    paths:
      - 'subscribe/**'
      - 'clash/**'
      - 'subconverter/**'
      - 'requirements.txt'
      - 'Dockerfile'
      - '.github/actions/checkin/**'
  workflow_dispatch:
  schedule:
    - cron: "0 4 * * 1"   # 每周一重建，刷新依赖缓存

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push (multi-arch)
        uses: docker/build-push-action@v5
        with:
          context: .
          file: ./Dockerfile
          platforms: linux/amd64,linux/arm64
          push: true
          tags: |
            ghcr.io/wzdnzd/aggregator:latest
            ghcr.io/wzdnzd/aggregator:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
          provenance: false
```

### 3.2 多架构构建的坑

| 坑 | 解决 |
|----|------|
| `docker/setup-qemu-action` 必须先于 buildx | 顺序固定 |
| `linux/arm64` 在 ubuntu-latest 上需 QEMU 模拟（慢） | 用 `cache-from: type=gha` 缓存层 |
| `clash-linux-arm` 二进制如果缺，运行时会段错误 | Dockerfile 里做架构分支删除（避免误用错的二进制） |
| PIP 索引在不同架构都能拉 | `python:3.12-slim` 镜像跨平台 wheel 都有 |
| 镜像 size（linux/arm64 二进制比 amd 大，clash ~50MB） | 可接受，不优化 |

---

## 第四阶段：本地开发体验

### 4.1 开发者本地

```bash
# 拉镜像
docker pull ghcr.io/wzdnzd/aggregator:latest

# 跑任意命令
docker run --rm -v $(pwd):/work -w /work ghcr.io/wzdnzd/aggregator:latest \
  process -s config.json

# 或 docker-compose（推荐）
cat > docker-compose.yml <<EOF
services:
  aggregator:
    image: ghcr.io/wzdnzd/aggregator:latest
    volumes:
      - ./:/work
      - ./data:/aggregator/data
    working_dir: /work
    entrypoint: aggregator
    command: collect --all --overwrite --skip
EOF

docker compose run aggregator
```

### 4.2 与现有 Dockerfile 关系

- `Dockerfile` 既给 GitHub Actions 构建镜像用，也给本地 docker compose 用
- 删掉根目录里冗余的 Dockerfile 配置（已经够简洁）
- README 增补"Docker 使用"章节

---

## 实施步骤汇总

| 步骤 | 文件 | 改动 | 估算行数 |
|------|------|------|----------|
| 1 | `Dockerfile` | 重写为多架构感知，参数化 CMD | +30 |
| 2 | `entrypoint.sh` | 新增，参数化入口 | +30 |
| 3 | `.github/workflows/docker.yml` | 新增，自动构建并推送镜像 | +50 |
| 4 | `.github/workflows/process.yaml` | 改用 container 跑镜像 | -10 |
| 5 | `.github/workflows/collect.yaml` | 同上 | -10 |
| 6 | `.github/workflows/refresh.yaml` | 同上 | -10 |
| 7 | `.github/workflows/checkin.yml` | 同上 | -5 |
| 8 | `README.md` | 增补 Docker 使用说明 | +20 |
| 9 | `docs/DOCKER_INTEGRATION_PLAN.md` | 本计划文件 | (已有) |
| 10 | `docker-compose.yml` | 新增，本地开发示例 | +10 |

总计：~3 文件新增 + 4 文件修改，约 +125 行。

---

## 风险与收益

### 收益

| 维度 | 收益 |
|------|------|
| **CI 速度** | 每个 action 节省 `pip install` ~30s，5 个 workflow 每天省 ~3 分钟 |
| **环境一致性** | 本地 `docker run` 与 CI 行为完全一致 |
| **多架构** | Apple Silicon / Graviton runners 都能直接跑 |
| **构建缓存** | 依赖变化少时镜像层缓存命中，build ~10s |

### 风险

| 风险 | 严重度 | 缓解 |
|------|--------|------|
| **GHCR 镜像首次构建慢**（无缓存层） | 🟡 中 | `cache-from: type=gha`；定时 schedule 保持镜像新鲜 |
| **clash/subconverter 二进制与 Python 解释器 glibc 版本不兼容** | 🟢 低 | 用 `python:3.12-slim` 与二进制同 debian base，glibc 一致 |
| **actions/checkout 覆盖镜像源码后，二进制与新代码版本不匹配** | 🟢 低 | 二进制是 subconverter / mihomo，API 稳定，几乎无破坏性变更 |
| **多架构镜像层数翻倍，push 慢** | 🟢 低 | `cache-to: type=gha,mode=max` 跨架构共享层；只 push 增量 |
| **维护负担**（多一个 docker.yml + entrypoint.sh） | 🟢 低 | 总 +125 行，相比收益划算 |

---

## 优先级与依赖

```
第一阶段（Dockerfile 多架构改造） ← 立即可做，独立
   ↓
第二阶段（actions 整合）          ← 依赖第一阶段镜像可用
   ↓
第三阶段（CI 自动构建镜像）       ← 让镜像持续更新
   ↓
第四阶段（本地 docker-compose）   ← 提升开发体验
```

**最小可行版本（MVP）**：第一阶段 + 第三阶段 → 镜像能用了，但 actions 还没用上。第二阶段渐进切换，避免一次改动 5 个 workflow 引入回归。

---

## 关联文件

- `Dockerfile`（现有，需重写）
- `.github/workflows/process.yaml` `collect.yaml` `refresh.yaml` `checkin.yml`（现有，需改用 container）
- `clash/clash-linux-amd` `clash/clash-linux-arm`（仓库已有多架构二进制）
- `subconverter/subconverter-linux-amd` `subconverter/subconverter-linux-arm`（同上）
- `.github/actions/checkin/universal.py`（容器内可执行）
- `requirements.txt`（pip 依赖固化在镜像里）
- `docs/CHECKIN_INTEGRATION_PLAN.md`（姊妹计划：checkin 自动集成）
