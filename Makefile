# hike-mode developer tasks. `make check` is the canonical gate — CI runs exactly
# this target, so local and CI never drift. Needs: uv, shellcheck, bats.
.PHONY: check lint test shellcheck bats install

check: lint test shellcheck bats

lint:
	uvx ruff check .

test:
	uv run pytest -q

shellcheck:
	shellcheck bin/* install.sh

bats:
	bats test/hike.bats

install:
	./install.sh
