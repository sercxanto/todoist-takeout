# Agent instructions

[CONTRIBUTING.md](CONTRIBUTING.md) describes the development environment, the tests and the project's requirements and design decisions. [README.md](README.md) is for users of the tool; keep development topics out of it.

User documentation lives in two places: [README.md](README.md) is the short entry point and keeps only what makes someone start using the tool, and the pages under [docs/](docs/) hold the details. The README is also the project description on PyPI, so it uses absolute links only, to `https://github.com/sercxanto/todoist-takeout/blob/main/...`, and no links to its own sections; [tests/test_docs.py](tests/test_docs.py) checks this and the links of `docs/`. Development notes, such as [dev/ideas.md](dev/ideas.md), go into `dev/`.

## Before you commit

Run these and fix what they report; CI runs the same checks and fails on any finding:

```sh
uv run ruff format .
uv run ruff check --fix .
uv run pytest
```

While the repository is private, CI runs only when started by hand (see [.github/workflows/tests.yml](.github/workflows/tests.yml)), so these local checks are the only gate, and they run on Linux only. When a change touches code that behaves differently on Windows or macOS, such as paths, file locking or atomic file replacement, say that it needs a manual CI run before it is merged.

Do not silence a Ruff finding with `# noqa` or a new ignore in `pyproject.toml` unless the rule is wrong for that code, and then say why in a comment.

## Planning

Changes are planned with OpenSpec under [openspec/](openspec/); [openspec/config.yaml](openspec/config.yaml) holds the constraints every change must keep.
