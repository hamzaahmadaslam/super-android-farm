# Local quality gates. Nothing here runs on a hosted CI service.
.PHONY: check quick live hook docs
check:  ## lint, static checks and the unit suite
	scripts/check.sh
quick:  ## lint and static checks only
	scripts/check.sh --quick
live:   ## everything, including tests against real containers
	scripts/check.sh --live
hook:   ## refuse a push while a gate is failing
	scripts/check.sh --install-hook
docs:   ## regenerate every generated reference from the code
	.venv/bin/sf config docs -o docs/CONFIGURATION.md
	.venv/bin/python scripts/gen_cli_docs.py -o docs/CLI.md
	.venv/bin/python scripts/gen_api_docs.py -o docs/API.md
	.venv/bin/python scripts/gen_flow_docs.py
