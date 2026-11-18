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
- `mcp_servers/plr_server.py` and `plr_connector.py`: OpenLabAI's own connector built on
  PyLabRobot, with a recording backend of our own (`TraceBackend`), plans simulated
  under PyLabRobot's tip and volume tracking, carry-over warnings, and an approval
  token that covers the exact commands a plan generates. Simulation only; the live
  OT-2 path is untested on hardware and refuses to start without a committed hardware
  log or `--accept-unverified`. See `docs/PYLABROBOT_CONNECTOR.md`.
  Test: `mcp_servers/test_plr_connector.py` (59 checks).
- `mcp_servers/remote_usb.py` and `usb_relay.py`: a PyLabRobot transport that reaches a
  USB instrument through an authenticated TCP relay running on a Mac or Linux machine,
  so the Windows PC that runs Venus does not need Hamilton's driver replaced with Zadig.
  Authenticated (HMAC challenge), one controller at a time, local by default, audited.
  Tested against a fake instrument, including PyLabRobot's real Hamilton `send_command`
  (`mcp_servers/test_remote_usb.py`, 36 checks); never run against a Hamilton.
  See `docs/USB_RELAY.md`.
- `mcp_servers/lab_agent_bridge.py` and a `plan_workflow` tool on `plr_server.py`: plan a workflow from
  Lab Agent (https://github.com/nygmeta/Claude_Life_sciences, a separate voice front end mostly by Junchen Lu)
  as plain JSON. Plate-to-plate transfers only; any other operation refuses the whole workflow with reasons.
  An accepted plan gets the usual card, approval token and gate, plus a sentence to speak.
  Tested on two real Lab Agent workflows (`mcp_servers/test_lab_agent_bridge.py`, 57 checks) and by
  `examples/lab_agent_to_openlabai.py` over MCP. Not tested with the voice pipeline or a robot.
  See `docs/VOICE_AND_LAB_AGENT.md`.
- `--port` option on the OT-2 server.

### Fixed (continued)
- `plan_protocol` now refuses a starting volume larger than the well can hold. PyLabRobot does not check
  this itself, so a plan could start from an impossible deck.
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
