"""Start the bot from a source checkout: `python run.py`.

The package is deliberately not installed, not even editable, so that
`setuptools` stays out of the virtualenv and the hash-locked dependency set says
exactly what is running. That means `src/` is not on `sys.path` by default.
pytest gets it from `pythonpath` in `pyproject.toml` and Alembic inserts it in
`alembic/env.py`; this file is the third and last place that needs it.

Everything real lives in `raseed/__main__.py`, which also works as
`python -m raseed` if the package is ever installed properly.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from raseed.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
