# Contributing to PyRun

Thanks for your interest in improving PyRun! This is a self-hosted Python
automation platform built with Django. Contributions of all sizes are welcome —
bug reports, docs, and code.

## Ways to contribute

- **Report a bug** — open an issue using the Bug Report template.
- **Request a feature** — open an issue using the Feature Request template.
- **Ask / discuss** — join the [Discord](https://discord.gg/BjkmTn7XSd).
- **Send a fix** — open a pull request (see below).

## Development setup

You need Python 3.13+ and Node.js 20+ (the Tailwind build and the optional Claude
AI integration use Node).

```bash
git clone https://github.com/hassancs91/PyRun.git
cd PyRun
python -m venv venv
# Windows: venv\Scripts\activate   |   macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Generate the two required keys and paste them into .env:
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

python manage.py migrate
python manage.py runserver
```

Convenience launchers are provided: `run-local.sh` (macOS/Linux),
`run-local.ps1` (Windows), and `run-local-postgres.ps1` for a Postgres stack.

You can also run the whole thing in Docker:

```bash
docker compose up -d
```

## Running the tests

The suite runs on the zero-config SQLite backend:

```bash
python manage.py test core
```

Please add or update tests for any behavior you change. New tests live alongside
the existing ones in `core/` as `test_*.py`.

## Pull request guidelines

1. Branch off `main`.
2. Keep each PR focused on one change; separate unrelated changes into separate PRs.
3. Make sure `python manage.py test core` passes and `python manage.py check` is clean.
4. Update docs (README / `.env.example`) when you add or change configuration.
5. Fill out the PR template so reviewers have context.

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).

## Building plugins

PyRun has a first-class plugin system. If you want to build a plugin rather
than change the core, start with the plugin author guide at
[docs/plugins.md](docs/plugins.md) and the worked examples under `examples/`.

## Code of conduct

Participation in this project is governed by our
[Code of Conduct](CODE_OF_CONDUCT.md).
