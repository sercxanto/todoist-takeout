# Agent instructions

[CONTRIBUTING.md](CONTRIBUTING.md) describes the development environment, the tests and the project's requirements and design decisions. [README.md](README.md) is for users of the tool; keep development topics out of it.

## Before you commit

Run these and fix what they report; CI runs the same checks and fails on any finding:

```sh
uv run ruff format .
uv run ruff check --fix .
uv run pytest
```

Do not silence a Ruff finding with `# noqa` or a new ignore in `pyproject.toml` unless the rule is wrong for that code, and then say why in a comment.

## Planning

Changes are planned with OpenSpec under [openspec/](openspec/); [openspec/config.yaml](openspec/config.yaml) holds the constraints every change must keep.
