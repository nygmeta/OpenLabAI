# Contributing to OpenLabAI

Thank you for helping. Wet lab scientists who can say what is missing are as
useful as people who write code.

## Set up and run the tests

```bash
git clone https://github.com/nygmeta/OpenLabAI.git
cd OpenLabAI
pip install -r requirements.txt          # Python 3.13+
```

Every suite is a script that exits non-zero on failure. CI runs all of them:

```bash
python evals/test_criteria.py
python mcp_servers/devices/test_devices.py
python mcp_servers/test_ot2_server.py
python mcp_servers/test_plr_connector.py
python mcp_servers/test_remote_usb.py
python scripts/test_verify_ot2_hardware.py
python integrations/test_slack_flow.py
python integrations/test_workcell.py
python teleop/test_teleop.py
```

None of them needs hardware. The OT-2 tests run against `mcp_servers/fake_ot2.py`,
a test double. A passing test there means the server is consistent with the fake;
it is not evidence about a physical robot.

## Status claims must be backed

The README's Project Status section says what has been run on what. Keep it that
way:

* Do not mark a connector as verified on hardware without a committed log from
  `scripts/verify_ot2_hardware.py` (OT-2) or an equivalent record for the
  instrument.
* Mock-mode and simulation output is labelled as such and is never evidence that a
  connector works on hardware.
* If a change makes a README claim untrue, fix the README in the same pull
  request.

## Adding an instrument

Add a server under `mcp_servers/` with a small, fixed set of tools and explicit
JSON schemas. Anything that moves hardware must be gated behind an explicit human
approval, and `stop` must never be gated. Add tests, and describe in the README
exactly what has and has not been run. For benchtop devices, a driver file under
`mcp_servers/devices/` is enough; see `docs/INSTRUMENT_GUIDE.md`.

## Pull requests

* One change per pull request, with tests for new behaviour. Tests for refusal
  paths matter as much as tests for the happy path.
* Say how you checked it, and on what hardware if any.
* Safety-relevant changes (anything on the path to `start_run` or another
  motion tool) need a test showing the refusal still holds.

## Reporting problems

Bugs and hardware reports go in GitHub issues. For a safety or security problem,
see [SECURITY.md](SECURITY.md).
