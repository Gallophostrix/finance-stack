VERSION_FILE := etl/__init__.py
IMAGE := finance-etl
DOCKERFILE := etl/Dockerfile
BUILD_CTX := .

VERSION := $(shell python -c "import re;print(re.search(r'__version__\\s*=\\s*\\\"([^\\\"]+)\\\"', open('$(VERSION_FILE)').read()).group(1))")


.PHONY: version
version:
	@echo $(VERSION)

.PHONY: bump-patch bump-minor bump-major
bump-patch:
	@read -p "Commit message: " msg; \
	new=$$(python tools/bump_version.py patch); \
	git add $(VERSION_FILE); \
	git commit -m "$$msg (v$$new)"; \
	git tag -a v$$new -m "$$msg"; \
	echo "Bumped to $$new"

bump-minor:
	@read -p "Commit message: " msg; \
	new=$$(python tools/bump_version.py minor); \
	git add $(VERSION_FILE); \
	git commit -m "$$msg (v$$new)"; \
	git tag -a v$$new -m "$$msg"; \
	echo "Bumped to $$new"

bump-major:
	@read -p "Commit message: " msg; \
	new=$$(python tools/bump_version.py major); \
	git add $(VERSION_FILE); \
	git commit -m "$$msg (v$$new)"; \
	git tag -a v$$new -m "$$msg"; \
	echo "Bumped to $$new"

.PHONY: build
build:
	docker build --build-arg VERSION=$(VERSION) -t $(IMAGE):$(VERSION) -t $(IMAGE):latest -f $(DOCKERFILE) $(BUILD_CTX)

.PHONY: release
release: build
	git push --follow-tags
	@echo "Released $(IMAGE):$(VERSION)"
