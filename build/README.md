# build/

Packaging helpers, kept separate from the runtime source in `src/` so a
normal `pip install -r requirements.txt` never pulls in packaging-only
dependencies.

```
pip install -r build/requirements.txt
python build/build.py
```

Produces a standalone executable in `dist/` at the repo root using
PyInstaller, bundling `run.py` and its dependencies (including `icon.png`)
into a single file. Useful for anyone who wants to run this without setting
up a Python environment themselves.

Build artifacts (`build/_work/`, `dist/`) are git-ignored.
