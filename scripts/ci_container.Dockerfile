# syntax=docker/dockerfile:1
#
# 本地门禁容器镜像 —— 定义在本仓库里，而不是某台机器的镜像缓存里。
#
# 存在的理由：本地门禁此前跑在手工 `docker run -d ... sleep infinity`
# 拉起来的临时容器中，镜像没有定义在仓库里 —— 缺什么依赖就临时装什么，
# 于是本地验的那套依赖和 CI 装的那套不是同一份。这个文件把依赖装进
# 镜像：容器启动即可跑 lane，不需要在容器里现装依赖。
#
# 三条边界：
#
#   * 整个仓库**不**进镜像。运行期把仓库挂进去，镜像只带依赖。
#   * 依赖清单由 `scripts/build_ci_container.sh` 在构建前用 `uv export`
#     从 `backend/uv.lock` 现生成。仓库的依赖真源是
#     `backend/pyproject.toml` + `backend/uv.lock`；`backend/requirements.txt`
#     已经被删掉了 —— 两份清单就是两份真源（见
#     `backend/tests/static_gates/test_dependency_manifest_and_lock_agree.py`），
#     所以那份清单是**生成物**，不入库。
#   * 已知缺口：镜像里没有 gitleaks。`scripts/scan_commit_range.sh` 在
#     gitleaks 不在 PATH 时只告警并跳过密钥扫描，而 CI 一定装它。
#     gitleaks 是按架构分发的二进制，装进来要按架构钉校验和 —— 那是
#     密钥扫描那条 lane 的事，这里先记下来，不塞一个没钉住的下载。

FROM python:3.11-slim

# Debian 源。默认指向 USTC 镜像：从某些网络出发，官方 CDN 拉 Packages
# 索引会卡住。换网络 / 换机器时用 --build-arg APT_HOST=... 覆盖。
# 只换主机名，不换路径 —— deb.debian.org 的 /debian 与 /debian-security
# 在镜像站上是同名的两个路径。
ARG APT_HOST=mirrors.ustc.edu.cn

# ---------------------------------------------------------------------------
# OS 依赖
# ---------------------------------------------------------------------------
# git 是门禁自己的依赖：commit-hygiene 与 commit-range-privacy 这两条要读
# 提交历史，没有 git 就只能 skip —— skip 不算跑过。
# ca-certificates 是 pip 走 HTTPS 的前提。
#
# --no-install-recommends + 删掉 apt lists：这两个是镜像体积的主要来源。
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|http://deb.debian.org|https://${APT_HOST}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    printf 'Acquire::Retries "3";\nAcquire::http::Timeout "60";\n' \
        > /etc/apt/apt.conf.d/99-ci-mirror; \
    apt-get update -qq; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        ca-certificates \
        git \
        passwd; \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# backend 依赖 + venv
# ---------------------------------------------------------------------------
# 这份清单是构建期生成的（见文件头），所以镜像里装的是锁住的那一份，
# 和 CI 用 `uv sync --locked` 装的是同一份。
COPY backend/requirements.txt /tmp/backend/requirements.txt

# venv 建在 /opt/venv：门禁跑起来时 PATH 里就有它，容器不需要再 bootstrap
# 一次解释器。pip 从 venv 里调用，不是系统 pip —— 系统 pip 装的东西不在
# 门禁的 PATH 上，等于没装。
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install -r /tmp/backend/requirements.txt && \
    rm -rf /root/.cache

ENV PATH="/opt/venv/bin:${PATH}"

# ---------------------------------------------------------------------------
# 非 root —— 门禁会写文件（.pytest_cache、覆盖率数据）
# ---------------------------------------------------------------------------
# root 写出来的文件在宿主机上属主是 root，下一次本地跑就 permission
# denied。uid 1000 与 GitHub runner 一致。
RUN useradd -m -u 1000 -s /bin/bash runner
ENV HOME=/home/runner

# 运行期把仓库挂到这里。
WORKDIR /src

CMD ["sleep", "infinity"]
