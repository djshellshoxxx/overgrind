# Developer Guide

## Setup and tests

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI (`.github/workflows/tests.yml`) runs on Python 3.10 and 3.13. On this snapshot, `pytest` ran the
suite on Linux with 20 tests passing.

## Adding an application adapter

An adapter implements `set_control(control, value)`, `activate_feature(feature)` and `healthy()`, and
can optionally implement `prepare_case(features)` for state reset. Planning, sampling and the JSONL
evidence code are reused unchanged. Applications with custom-drawn controls (for example JUCE knobs)
may need a dedicated adapter or a test-only automation bridge.

## Rules

- Keep the engine deterministic for a given seed; tests cover the planner, validation and runner.
- Keep inspection read-only.
- Do not present UIA sequential interactions as simultaneous feature activation.
