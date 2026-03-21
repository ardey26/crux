.PHONY: build test bench clean install

build:
	cd daemon && cargo build --release

test: build
	cd daemon && cargo test
	cd sdk && python -m pytest tests/ -v

bench: build
	cd bench && python runner.py

install: build
	cd sdk && pip install -e .

clean:
	cd daemon && cargo clean
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
