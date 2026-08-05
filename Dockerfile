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

# 1. 系统依赖：jq（checkin.yml 用 jq 解析 Gist API 响应，python:3.12-slim 不预装）
RUN apt-get update && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*

# 2. Python 依赖（缓存优化）
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

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
