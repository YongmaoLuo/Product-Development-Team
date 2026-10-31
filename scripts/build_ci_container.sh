#!/usr/bin/env bash
#
# 构建本地门禁容器镜像。
#
#   bash scripts/build_ci_container.sh                        # → pdt-ci:local
#   PDT_CI_IMAGE=pdt-ci:test bash scripts/build_ci_container.sh
#
# 为什么它存在：本地门禁此前用的是手工 `docker run -d ... sleep infinity`
# 拉起来的临时容器 —— 镜像没有定义在仓库里，缺什么依赖就临时装什么，
# 本地验的那套依赖和 CI 装的那套不是同一份。这个脚本把镜像定义
# （scripts/ci_container.Dockerfile）变成一条命令：构建期装好 backend
# 依赖与 venv，容器启动即可跑 lane。
#
# 退出码：
#   0   构建成功
#   127 机器上没有 docker
#   1   其余前置条件不满足（例如导不出依赖清单）
#
# stdout 上只有一行：built <image> from scripts/ci_container.Dockerfile
# 进度信息与错误信息都走 stderr —— 捕获 stdout 的调用方拿到的是干净输出。
set -euo pipefail

# tag 可以在外面换：PDT_CI_IMAGE 是这个脚本对外的接口，构建出来的镜像
# 叫什么由调用的人决定，脚本自己只给一个默认值。
IMAGE="${PDT_CI_IMAGE:-pdt-ci:local}"

# docker 缺失要先报：没有它，后面每一步都只是噪声。这里刻意放在最前面，
# 而且只用 shell 内建 —— PATH 被清空时（就是没有 docker 的那条路径）
# 脚本仍要能跑完并给出正确的退出码。
#
# 它也必须早于依赖清单的导出：跑不成的那次调用不该在仓库里留下东西。
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 127
fi

# 仓库根：脚本在 scripts/ 下，上一级就是。只用 shell 内建（cd / pwd），
# 不用 dirname 之类的外部命令 —— 那条路径上 PATH 可能已经被清空了。
# pwd -P 取物理路径：docker 看到的是物理路径，别让它收到一个带 .. 的。
_script_dir="${BASH_SOURCE[0]%/*}"
if [[ "$_script_dir" == "${BASH_SOURCE[0]}" ]]; then
  _script_dir="."
fi
REPO_ROOT="$(cd "$_script_dir/.." && pwd -P)"

DOCKERFILE_REL="scripts/ci_container.Dockerfile"
REQUIREMENTS_REL="backend/requirements.txt"
REQUIREMENTS="${REPO_ROOT}/${REQUIREMENTS_REL}"

# ---------------------------------------------------------------------------
# 依赖清单：现生成，不入库
# ---------------------------------------------------------------------------
# 仓库的依赖真源是 backend/pyproject.toml + backend/uv.lock，
# backend/requirements.txt 已经删掉了 —— 两份清单就是两份真源，没有
# 东西核对谁跟谁一致。Dockerfile 里 COPY 的是一份 requirements 清单，
# 所以构建前用 `uv export` 从锁文件导出一份，构建完删掉：生成物，不入库
# （backend/tests/static_gates/test_dependency_manifest_and_lock_agree.py
#  断言仓库里没有这份文件）。
#
# 只有我们自己生成的那一份会被删：要是调用的人自己放了一份在
# backend/requirements.txt，那是他的文件，脚本不碰。
GENERATED_REQUIREMENTS=0

cleanup() {
  if [[ "$GENERATED_REQUIREMENTS" -eq 1 && -f "$REQUIREMENTS" ]]; then
    rm -f "$REQUIREMENTS"
  fi
}
# EXIT 覆盖正常结束与命令失败；INT / TERM 覆盖"构建到一半被 Ctrl-C 掉"
# —— 那条路径上没人会替我们删掉那份清单，而留在仓库里会撞上静态门禁。
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -f "$REQUIREMENTS" ]]; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required to export ${REQUIREMENTS_REL} from backend/uv.lock" >&2
    exit 1
  fi
  echo "[build_ci_container] exporting ${REQUIREMENTS_REL} from backend/uv.lock" >&2
  # --locked：锁文件跟 pyproject 对不上就当场失败，而不是重新解一遍
  # 依赖 —— 镜像里装的东西必须跟 CI 装的是同一份。
  # --quiet：uv export 默认把清单内容也打到 stdout，那 166 行会把 stdout
  # 淹掉 —— stdout 上只该有"构建出了什么"这一行。
  GENERATED_REQUIREMENTS=1
  (
    cd "$REPO_ROOT"
    uv export --project backend --format requirements.txt --no-hashes --locked \
      --quiet --output-file "$REQUIREMENTS"
  )
fi

# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------
# 构建上下文是仓库根：Dockerfile 里的 COPY backend/requirements.txt 按
# 上下文解析。仓库本身不会进镜像 —— 只有那一份清单会。
#
# 多余的参数原样交给 docker build（例如 --build-arg APT_HOST=... 换
# Debian 源）；上下文永远放在最后一位。
#
# 进度信息走 stderr，stdout 上只留最后那一行"构建出了什么"：捕获 stdout
# 的调用方（脚本、门禁报告）拿到的是干净的输出，而不是一屏进度。
echo "[build_ci_container] building ${IMAGE} from ${DOCKERFILE_REL}" >&2
if [[ $# -gt 0 ]]; then
  docker build --file "${REPO_ROOT}/${DOCKERFILE_REL}" --tag "$IMAGE" \
    "$@" "${REPO_ROOT}"
else
  docker build --file "${REPO_ROOT}/${DOCKERFILE_REL}" --tag "$IMAGE" \
    "${REPO_ROOT}"
fi
echo "built $IMAGE from $DOCKERFILE_REL"
