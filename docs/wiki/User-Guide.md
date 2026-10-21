# User Guide

1. Install with Python 3.10+: `python -m pip install -e .`, and for Windows UI Automation
   `python -m pip install -e ".[windows]"`.
2. Start the application under test in an interactive, logged-in Windows desktop session.
3. Inspect its controls (read-only): `python -m overgrind inspect --title-regex "Your App.*" --output controls.json`.
   The output is a discovery aid, not a manifest.
4. Write a manifest based on `examples/demo.json`. Replace every sample identifier with a real one.
5. Preview cases: `python -m overgrind plan --config <manifest> --samples 2 --seed 123`.
6. Run only against an application you control, preferably in a disposable profile:
   `python -m overgrind run --config <manifest> --samples 100 --seed 2048 --report reports/run.jsonl --execute`.

Read the README "Feature modes" section before choosing `enable` or `invoke`. Do not treat
destructive commands, Save/Open operations or mutually exclusive tabs as independent features.
