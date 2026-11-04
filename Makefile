# dywatch —— 常用开发任务。Linux / WSL 下用 make；Windows 直接用等价命令。
#   make check == CI 做的事（ruff format 检查 + 全部测试）。
PYTHON ?= .venv/bin/python
NPM ?= npm

.PHONY: help install test test-one format format-check check docs docs-build

help:
	@echo "make install       安装依赖（含 dev，来自 pyproject）"
	@echo "make test          跑全部测试"
	@echo "make test-one T=... 只跑某个测试文件/用例"
	@echo "make format        用 ruff 格式化 src 与 tests"
	@echo "make format-check  只检查格式"
	@echo "make check         format-check + test（与 CI 一致）"
	@echo "make docs          本地起文档站（VitePress）"
	@echo "make docs-build    构建文档站"

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

test-one:
	$(PYTHON) -m pytest -q $(T)

format:
	$(PYTHON) -m ruff format src tests

format-check:
	$(PYTHON) -m ruff format --check src tests

check: format-check test

docs:
	$(NPM) run docs:dev

docs-build:
	$(NPM) run docs:build
