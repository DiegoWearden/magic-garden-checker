SHELL := /bin/bash
GIT_HELPER := ./git-mirror

.PHONY: add commit push status

# Prevent make from treating extra words as targets
%:
	@:

# Usage: make add .
add:
	@echo "Running: $(GIT_HELPER) add $(filter-out $@,$(MAKECMDGOALS))"
	$(GIT_HELPER) add $(filter-out $@,$(MAKECMDGOALS))

# Usage: make commit MSG="your message"
# Note: `make commit -m "msg"` won't work because make treats `-m` as an option.
commit:
	@if [ -n "$(MSG)" ]; then \
	  echo "Committing with message: $(MSG)"; \
	  $(GIT_HELPER) commit -m "$(MSG)"; \
	else \
	  echo "Please provide a message: make commit MSG='your message'"; exit 2; \
	fi

# Usage: make push [FORCE=1]
# If FORCE=1, the mirror push will be forced (git push --force mirror branch:main)
push:
	@echo "Pushing to origin and mirror..."
	$(GIT_HELPER) push $(if $(filter 1,$(FORCE)),--force,)

status:
	$(GIT_HELPER) status
