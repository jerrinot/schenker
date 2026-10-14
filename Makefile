# Repository entry points. driver/ is also the self-contained DKMS source.
.DEFAULT_GOAL := all
.PHONY: all clean test check-installed
all clean:
	$(MAKE) -C driver $@

test:
	./tests/run.sh

check-installed:
	python3 tests/check-installed.py
