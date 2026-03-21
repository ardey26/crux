.PHONY: build test bench clean install publish

build:
	cd daemon && cargo build --release

test: build
	cd daemon && cargo test
	cd sdk && python -m pytest tests/ -v

bench: build
	cd bench && python runner.py

install: build
	cd sdk && pip install -e .

publish:
	@test -f .env && . ./.env || true; \
	cd sdk && rm -rf dist/ build/ && python -m build && twine upload dist/* -u __token__ -p "$$PYPI_TOKEN"

clean:
	cd daemon && cargo clean
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
