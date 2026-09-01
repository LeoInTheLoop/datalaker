#
# Data Steward Claw —— 两个 target，两个身份（CLAUDE.md 铁律 2）
#
#   callback  审批服务。唯一能写 decisions 表的身份。不含 Hermes，镜像很小。
#   agent     Hermes 运行时 + 治理 Plugin。对 decisions 只有 SELECT。
#
# 分开不是为了洁癖：同一个镜像跑两个角色，「独立进程 + 独立数据库账号」
# 就退化成了自觉，而不是事实。

# ---------------------------------------------------------------- base
FROM python:3.13-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN pip install --no-cache-dir "psycopg[binary]==3.*" "sqlglot>=25" openpyxl

# ---------------------------------------------------------------- callback
FROM base AS callback
COPY services/ /app/services/
COPY plugins/ /app/plugins/
ENV PYTHONPATH=/app
EXPOSE 8787
CMD ["python", "services/approval_callback.py"]

# ---------------------------------------------------------------- agent
FROM base AS agent
# Hermes 要求 python >=3.11,<3.14 —— 本镜像是 3.13，符合
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

# 只取最新一次提交，并剔除运行时用不到的体积大头
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /opt/hermes \
    && rm -rf /opt/hermes/.git /opt/hermes/website /opt/hermes/tests-js \
              /opt/hermes/assets /opt/hermes/contributors /opt/hermes/ui-tui \
              /opt/hermes/mcp-research-data /opt/hermes/datagen-config-examples

RUN uv pip install --system -e /opt/hermes

COPY services/ /app/services/
COPY plugins/ /app/plugins/
COPY tests/ /app/tests/
# Hermes 自己有顶层 `plugins` 包 —— 把我们的目录也入路径，
# 以 `datasteward_gate` 顶层名导入，避开同名冲突（见 R1 handoff）
ENV PYTHONPATH=/app:/app/plugins:/opt/hermes
CMD ["sleep", "infinity"]
