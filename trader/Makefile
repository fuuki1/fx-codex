# trader/ operations shortcuts. Run from the trader/ directory.
# `make help` でターゲット一覧。
SHELL := /bin/bash
COMPOSE := docker compose
REDIS := $(COMPOSE) exec -T redis redis-cli

.DEFAULT_GOAL := help

.PHONY: help
help: ## このヘルプを表示
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---- ライフサイクル --------------------------------------------------------
.PHONY: up
up: guard-env ## 全サービスをビルドして起動（バックグラウンド）
	$(COMPOSE) up -d --build

.PHONY: down
down: ## 全サービス停止（データは保持）
	$(COMPOSE) down

.PHONY: restart
restart: ## 全サービス再起動
	$(COMPOSE) restart

.PHONY: ps
ps: ## サービス状態
	$(COMPOSE) ps

.PHONY: logs
logs: ## 全ログを追従（Ctrl-C で終了）
	$(COMPOSE) logs -f --tail=100

.PHONY: config
config: ## compose 構文と依存関係を検証
	$(COMPOSE) config >/dev/null && echo "compose OK"

# ---- 安全装置（Kill switch）-----------------------------------------------
.PHONY: kill-on
kill-on: ## 全発注を即時停止（Kill switch ON）
	$(REDIS) set kill_switch 1 && echo "KILL SWITCH ON"

.PHONY: kill-off
kill-off: ## 発注を再開（Kill switch OFF）
	$(REDIS) set kill_switch 0 && echo "KILL SWITCH OFF"

.PHONY: kill-status
kill-status: ## Kill switch の現在値
	@echo -n "kill_switch="; $(REDIS) get kill_switch

# ---- 開発・検証 ------------------------------------------------------------
.PHONY: test
test: ## ローカルで pytest を実行
	cd $(CURDIR) && python -m pytest -q

.PHONY: lint
lint: ## ruff で lint
	ruff check app tests optimize

.PHONY: fmt
fmt: ## ruff で自動整形
	ruff format app tests optimize
	ruff check --fix app tests optimize

# ---- 運用 ------------------------------------------------------------------
.PHONY: backup
backup: ## DB バックアップを取得
	./deploy/backup.sh

.PHONY: reconcile
reconcile: ## ブローカーと DB のリコンサイルを手動実行
	$(COMPOSE) run --rm --no-deps executor python reconcile.py --once

.PHONY: guard-env
guard-env:
	@test -f .env || { echo "ERROR: .env がありません。'cp .env.example .env && chmod 600 .env' を実行してください"; exit 1; }
