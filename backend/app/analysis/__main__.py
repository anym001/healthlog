"""``python -m app.analysis`` entry point.

The scheduler runs the nightly analysis as a subprocess via this module path
(see ``scheduler.py``); keep it a thin wrapper over ``run.main``.
"""

from .run import main

if __name__ == "__main__":
    raise SystemExit(main())
