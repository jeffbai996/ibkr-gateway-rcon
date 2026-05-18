# Contributing to ibkr-gateway-rcon

Thanks for considering a contribution.

## Before you start

- Open an issue first for anything bigger than a bug fix or doc tweak.
- One logical change per PR. Bundling unrelated changes makes review slow and rollbacks painful.

## Workflow

```bash
git clone https://github.com/jeffbai996/ibkr-gateway-rcon.git
cd ibkr-gateway-rcon
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# run tests if a tests/ directory exists
pytest 2>/dev/null || true
```

## Commit messages

Conventional commits, one line under ~70 chars:

- `feat: …` new user-visible behavior
- `fix: …` bug fix
- `refactor: …` no behavior change
- `docs: …` documentation only
- `test: …` tests only
- `chore: …` build / deps / CI / housekeeping
- `release: …` version bumps

Body in the imperative; explain the *why* not the *what*. Keep one logical change per commit so `git bisect` stays useful.
