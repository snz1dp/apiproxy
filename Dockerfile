##################################################################
# 定义编译标签
##################################################################
ARG PYTHON_TAG=python3.12-bookworm-slim

# 定义Python基础镜像
FROM snz1.cn/base/python-uv:${PYTHON_TAG} AS python

##################################################################
# 定义编译镜像：使用NVIDIA CUDA开发镜像作为编译环境
##################################################################
FROM python AS builder

# Install the project into `/app`
WORKDIR /app/

RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=README.md,target=README.md \
    --mount=type=bind,source=src/apiproxy/pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=src/apiproxy/uv.lock,target=uv.lock \
    echo "Installing ApiProxy dependencies..." && \
    uv sync --frozen --no-install-project --no-editable && \
    echo "ApiProxy dependencies installed successed."

ADD src/apiproxy /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable

##################################################################
# 定义运行时镜像：使用NVIDIA CUDA基础镜像作为运行时环境
##################################################################
FROM python AS runtime

WORKDIR /app

RUN useradd user -u 1000 -g 0 --no-create-home --home-dir /app

COPY --from=builder --chown=1000 /app/.venv /app/.venv

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

LABEL org.opencontainers.image.title=ApiProxy
LABEL org.opencontainers.image.authors=['Snz1DP']
LABEL org.opencontainers.image.url=https://snz1.cn/gitrepo/dp/ai/taiyiflow
LABEL org.opencontainers.image.source=https://snz1.cn/gitrepo/dp/ai/taiyiflow

COPY scripts/start.sh /app/start.sh
RUN chmod +x /app/start.sh

USER user
WORKDIR /app

CMD [ "/app/start.sh" ]
