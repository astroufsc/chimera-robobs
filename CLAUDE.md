# chimera-robobs

Robotic observation scheduler plugin for the chimera observatory control
system.  Layout and conventions follow the
[chimera-template](https://github.com/astroufsc/chimera-template)
cookiecutter and the chimera-supervisor 2.0 house style.

- Use **uv** for everything (`uv sync`, `uv run pytest`, `uv run ruff`);
  never pip.
- Lint/format with ruff as configured in `.pre-commit-config.yaml` and
  `pyproject.toml`; keep the SPDX two-line header on every `.py` file.
- Keep the project structure compatible with the chimera-template so
  template updates can be applied.
- The algorithm ids/names stored in `blockpar.sched_algorithm` (0 HIG,
  1 STD, 2 TIMED, 3 RECURRENT, 4 TIMESEQUENCE) and the legacy input-file
  formats (project YAML, targets CSV, block `.list` + block YAML,
  pid-config YAML) are compatibility surfaces — never change them.
  See `docs/robobs-port-notes.md` and `docs/plans/deep-refactor.md`.
