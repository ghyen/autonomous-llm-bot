.PHONY: help run run-all run-rapid run-bot test

help:
	@echo "Autonomous LLM Bot - Operation Commands"
	@echo ""
	@echo "Usage:"
	@echo "  make run        Start both rapid-mlx and bot (runs server, waits for health, runs bot)"
	@echo "  make run-rapid  Start rapid-mlx server standalone"
	@echo "  make run-bot    Start autonomous-llm-bot standalone"
	@echo "  make test       Run test suite"
	@echo ""
	@echo "Environment Variables (optional overrides):"
	@echo "  MODEL_PATH      Path to model directory"
	@echo "  HOST            Host address (default: 127.0.0.1)"
	@echo "  PORT            Port number (default: 18080)"
	@echo "  PYTHON_BIN      Path to python interpreter"

run:
	./scripts/run_all.sh

run-all: run

run-rapid:
	./scripts/run_rapid.sh

run-bot:
	./scripts/run_bot.sh

test:
	python3 -m unittest discover -s . -p "test_*.py"
