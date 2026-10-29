# Run `make check` before every commit; CI runs the same target.

UV ?= uv
IMAGE ?= steward:dev

# Licenses allowed for runtime (shipped) Python dependencies. Anything else fails
# `make licenses` and needs a decision (and an ADR if it's accepted).
ALLOWED_LICENSES := MIT;MIT License;MIT-CMU;BSD License;BSD-2-Clause;BSD-3-Clause;Apache Software License;Apache-2.0;ISC License (ISCL);ISC;Python Software Foundation License;PSF-2.0;Mozilla Public License 2.0 (MPL 2.0);MPL-2.0

RUNTIME_PACKAGES = $$($(UV) export --no-dev --no-emit-workspace --no-hashes --no-header --format requirements-txt | sed -n 's/^\([A-Za-z0-9._-]*\)==.*/\1/p')

.PHONY: help sync check lint format typecheck imports test headers licenses notices image clean

help:
	@echo "make sync       install dependencies into .venv"
	@echo "make check      everything CI runs: lint, types, imports, tests, headers, licenses"
	@echo "make format     auto-fix formatting and safe lint issues"
	@echo "make test       run the test suite"
	@echo "make notices    regenerate THIRD_PARTY_NOTICES.md"
	@echo "make image      build the Docker image ($(IMAGE))"

sync:
	$(UV) sync --locked

check: lint typecheck imports test headers licenses

lint:
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

format:
	$(UV) run --locked ruff format .
	$(UV) run --locked ruff check --fix .

typecheck:
	$(UV) run --locked mypy

imports:
	$(UV) run --locked lint-imports

test:
	$(UV) run --locked pytest

headers:
	$(UV) run --locked python scripts/check_headers.py

licenses:
	$(UV) run --locked pip-licenses --from=mixed --partial-match \
		--allow-only="$(ALLOWED_LICENSES)" --packages $(RUNTIME_PACKAGES)

notices:
	@{ \
		echo "# Third-party notices"; \
		echo; \
		echo "steward-ai is licensed under Apache-2.0 (see LICENSE and NOTICE). The Python"; \
		echo "packages below are installed alongside it in the Docker image, each under its"; \
		echo "own license. Their full license texts are in the image under /licenses/."; \
		echo "Regenerate this file with \`make notices\`."; \
		echo; \
		$(UV) run --locked pip-licenses --from=mixed --format=markdown --with-urls \
			--packages $(RUNTIME_PACKAGES) \
			| sed 's|https://TODO.com |https://github.com/asg017/sqlite-vec|'; \
	} > THIRD_PARTY_NOTICES.md
	@# sqlite-vec's wheel has a placeholder home page; the sed above names the real one.

image:
	docker build -f deploy/Dockerfile -t $(IMAGE) .

clean:
	rm -rf .venv .mypy_cache .ruff_cache .pytest_cache dist build
