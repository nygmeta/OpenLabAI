# Changelog

## Unreleased

### Added
- GitHub Actions workflow that runs every test suite on each push and pull request.
- `mcp_servers/fake_ot2.py`, a fake Opentrons HTTP API for tests, and
  `mcp_servers/test_ot2_server.py` (35 checks), which covers the OT-2 execution
  path and the approval gate against it. Previously only the refusal paths were
  tested.
- `scripts/verify_ot2_hardware.py` and `docs/HARDWARE_VERIFICATION.md`: run
  OpenLabAI's own OT-2 server against a physical robot and record a log in
  `hardware_logs/`. Test: `scripts/test_verify_ot2_hardware.py` (25 checks).
- `--port` option on the OT-2 server.
- CONTRIBUTING.md, SECURITY.md, and issue templates.

### Fixed
- `start_run` now refuses a run that was not uploaded through `upload_protocol` in
  the current session. Before, such a run skipped the analysis-error and deck-constraint
  checks, because those checks read what `upload_protocol` had recorded.
- `audit_logged` in `start_run`, `pause_run`, `resume_run` and `stop_run` responses
  now says whether the audit record was actually written. It used to report whether
  the logging module had loaded. A logging failure still never blocks a robot
  action.

### Not changed
- The README's hardware status. No hardware log has been committed yet, so the status
  is as it was.
