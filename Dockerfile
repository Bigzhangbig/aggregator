# Aggregator 多架构 docker 镜像
# 构建：docker buildx build --platform linux/amd64,linux/arm64 -t <tag> .
# 或 GitHub Actions: docker/build-push-action@v5 + platforms

FROM python:3.12-slim

LABEL maintainer="wzdnzd"
LABEL org.opencontainers.image.source="https://github.com/wzdnzd/aggregator"

ARG TARGETARCH
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/aggregator:${PATH}"

WORKDIR /aggregator

# 1. 系统依赖：jq + curl + Chromium 运行时依赖（patchright 人机验证绕过需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    jq curl \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 2. Python 依赖（缓存优化，含 patchright）
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

# 3. Chromium 二进制（patchright 人机验证绕过需要，详见 docs/PATCHRIGHT_INTEGRATION_PLAN.md）
#    显式装系统依赖（避开 --with-deps 在多架构 Debian 的脆弱路径），失败则构建失败
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /ms-playwright \
    && patchright install chromium \
    || { echo "[ERROR] patchright chromium install failed" >&2; exit 1; }

# 2. 项目源码
COPY subscribe/ ./subscribe/

# 3. checkin 脚本（独立路径，无 subscribe 依赖）
COPY .github/actions/checkin/ ./.github/actions/checkin/

# 4. clash 二进制 + GeoIP 数据库
COPY clash/ ./clash/

# 5. subconverter 二进制 + 配置 + 规则
COPY subconverter/ ./subconverter/

# 6. 统一入口
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 7. 架构分支：删除不匹配的二进制（保留 Country.mmdb 与所有 subconverter 配置/规则）
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) \
            rm -f clash/clash-darwin-amd clash/clash-darwin-arm clash/clash-linux-arm clash/clash-windows-amd.exe \
                  subconverter/subconverter-darwin-amd subconverter/subconverter-darwin-arm subconverter/subconverter-linux-arm subconverter/subconverter-windows-amd.exe; \
            ;; \
        arm64) \
            rm -f clash/clash-darwin-amd clash/clash-darwin-arm clash/clash-linux-amd clash/clash-windows-amd.exe \
                  subconverter/subconverter-darwin-amd subconverter/subconverter-darwin-arm subconverter/subconverter-linux-amd subconverter/subconverter-windows-amd.exe; \
            ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}"; exit 1 ;; \
    esac

ENTRYPOINT ["/entrypoint.sh"]
CMD ["collect", "--all", "--overwrite", "--skip"]
