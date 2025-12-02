.PHONY: all init format lint tests coverage clean_python_cache clean_all

# Configurations
VERSION=$(shell grep "^version" src/backend/pyproject.toml | sed 's/.*\"\(.*\)\"$$/\1/')
PYTHON_REQUIRED=$(shell grep '^requires-python[[:space:]]*=' src/backend/pyproject.toml)
RED=\033[0;31m
NC=\033[0m # No Color
GREEN=\033[0;32m

OSNAME=$(shell uname -n)
OSARCH=$(shell uname -m)
OSNAME_NORMAL=$(shell if [ "`uname -s`" = "Darwin" ]; then echo "MacOSX"; else echo `uname -s`; fi)

MINIFORGE3_VERSION ?= 24.11.3-0

log_level ?= debug
host ?= 0.0.0.0
port ?= 7860
env ?= .env
open_browser ?= true
path = src/backend/base/taiyiflow/frontend
workers ?= 1
async ?= true
lf ?= false
ff ?= true
all: help

info:
	@echo OS = $(OSNAME) $(OSARCH)
	@echo $(PYTHON_REQUIRED)
	@echo current version = $(VERSION)

######################
# 工具模块
######################

# 更新当前版本
patch: ## requirements.txt
	@echo 'Patching the version'
	@cd src/backend && poetry version patch
	@echo 'Patching the version in taiyiflow-base'
	@cd src/backend/base && poetry version patch
	@make lock

# 检查安装工具
check_tools: ## check if all required tools are installed
	@command -v snz1dpctl >/dev/null 2>&1 || { echo >&2 "$(RED)snz1dpctl is not installed. Aborting.$(NC)"; exit 1; }
	@command -v uv >/dev/null 2>&1 || { echo >&2 "$(RED)uv is not installed. Aborting.$(NC)"; exit 1; }
	@command -v npm >/dev/null 2>&1 || { echo >&2 "$(RED)NPM is not installed. Aborting.$(NC)"; exit 1; }
	@echo "$(GREEN)All required tools are installed.$(NC)"

# 显示帮助信息
help: ## show this help message
	@echo '----'
	@grep -hE '^\S+:.*##' $(MAKEFILE_LIST) | \
	awk -F ':.*##' '{printf "\033[36mmake %s\033[0m: %s\n", $$1, $$2}' | \
	column -c2 -t -s :
	@echo '----'

######################
# 环境安装
######################

# 生成ApiProxy开发依赖
generate_apiproxy_requirements: ## generate requirements.txt
	@echo 'generate requirements.txt...'
	@cd src/apiproxy && uv export --format requirements-txt | \
		sed '/^-e ./d' > requirements.txt

# 重新安装ApiProxy开发依赖
reinstall_apiproxy_dependencies: generate_apiproxy_requirements ## forces reinstall all dependencies (no caching)
	@echo 'Installing dev dependencies...'
	cd src/apiproxy && uv sync -n --reinstall --frozen;
	cd src/apiproxy && pip install --force-reinstall -r requirements.txt && pip install -e .;

# 安装ApiProxy开发依赖
install_apiproxy_dependencies: generate_apiproxy_requirements ## install the dev dependencies
	@echo 'Installing dev dependencies...'
	cd src/apiproxy && uv sync --frozen;
	cd src/apiproxy && pip install --force-reinstall -r requirements.txt && pip install -e .;

# 示例： make alembic-revision message="添加新字段"
alembic-revision: ## generate a new migration
	@echo 'Generating a new Alembic revision'
	@if [[ "$(OSNAME)" == "macwork" && "$(OSARCH)" == "x86_64" ]]; then \
		cd src/apiproxy/openaiproxy/ && alembic revision --autogenerate -m "$(message)" ; \
	else \
		cd src/apiproxy/openaiproxy/ && uv run alembic revision --autogenerate -m "$(message)" ; \
	fi

# 环境初始化
init: check_tools clean_python_cache ## initialize the project
	@make install_apiproxy_dependencies
	@echo "$(GREEN)All requirements are installed.$(NC)"
	@uv run apiproxy run

######################
# 清理编译缓存
######################

# 清理Python缓存
clean_python_cache: ## clean Python cache for the project
	@echo "Cleaning Python cache..."
	find . -type d -name '__pycache__' -exec rm -r {} +
	find . -type f -name '*.py[cod]' -exec rm -f {} +
	find . -type f -name '*~' -exec rm -f {} +
	find . -type f -name '.*~' -exec rm -f {} +
	find . -type d -empty -delete
	@echo "$(GREEN)Python cache cleaned.$(NC)"

# 清理所有缓存和临时目录
clean_all: clean_python_cache # clean all caches and temporary directories
	@echo "$(GREEN)All caches and temporary directories cleaned.$(NC)"

# 安装UV工具
install_uv: ## install uv using pip
	pip install uv

# 安装Miniforge3
install_miniforge3:
	@echo "Installing Miniforge3..."
	@curl -L -o Miniforge3-$(MINIFORGE3_VERSION)-$(OSNAME_NORMAL)-$(OSARCH).sh \
		https://github.com/conda-forge/miniforge/releases/download/$(MINIFORGE3_VERSION)/Miniforge3-$(MINIFORGE3_VERSION)-$(OSNAME_NORMAL)-$(OSARCH).sh \
	&& bash Miniforge3-$(MINIFORGE3_VERSION)-$(OSNAME_NORMAL)-$(OSARCH).sh -b -p ~/miniforge3

# 安装Python3.12
install_python:
	@echo 'Installing Python 3.12'
	@conda install -n apiproxy$(VERSION) python=3.12 -y -q \
		&& conda activate apiproxy$(VERSION)

######################
# 代码测试
######################

# 运行覆盖测试
coverage: ## run the tests and generate a coverage report
	@cd src/apiproxy && uv run coverage run
	@cd src/apiproxy && uv run coverage erase

# 运行单元测试
unit_tests: ## run unit tests
	@cd src/apiproxy && uv sync --extra dev --frozen
	@EXTRA_ARGS=""
	@cd src/apiproxy; \
	if [ "$(async)" = "true" ]; then \
		EXTRA_ARGS="$$EXTRA_ARGS --instafail -n auto"; \
	fi; \
	if [ "$(lf)" = "true" ]; then \
		EXTRA_ARGS="$$EXTRA_ARGS --lf"; \
	fi; \
	if [ "$(ff)" = "true" ]; then \
		EXTRA_ARGS="$$EXTRA_ARGS --ff"; \
	fi; \
	uv run pytest test --ignore=test/integration $$EXTRA_ARGS --instafail -ra -m 'not api_key_required' --durations-path tests/.test_durations --splitting-algorithm least_duration $(args)

build_apiproxy_docker:
	@echo 'Building apiproxy docker image'
	snz1dpctl make docker

publish_apiproxy:
	@echo 'Publishing apiproxy'
	snz1dpctl make publish
