# Verifying the OT-2 connector on hardware

The README says which OpenLabAI tools have run against a physical instrument. A
claim like that should be backed by a record anyone can read. This page explains
how to produce one for the Opentrons OT-2 and what that record does and does not
show.

## What the script does

`scripts/verify_ot2_hardware.py` starts OpenLabAI's own OT-2 server
(`mcp_servers/ot2_server.py`) and calls its tools over the Model Context
Protocol, the same path an agent uses. It then writes a JSON log to
`hardware_logs/`.

It also records two things the server does not report: the robot's own `/health`
identity (model, API and firmware version, and a hash of the serial number), and
the list of endpoints the robot's own OpenAPI document says it has.

| Tier | Flag | Tools exercised | Robot state |
|---|---|---|---|
| Read-only | (default) | `read_deck`, `get_run_status`, `create_protocol` | nothing changes |
| Upload | `--allow-upload` | adds `upload_protocol` with a home-only protocol | an idle run is created, checked, and deleted; nothing moves |
| Motion | `--allow-motion` | adds `home_robot` | the robot homes all axes; you must type `HOME` to confirm |

**Never exercised by this script:** `start_run`, `pause_run`, `resume_run`,
`stop_run`. Their hardware status stays "not yet run" until someone runs them
under a separate procedure with a person at the robot.

## How to run it

1. Check out the commit you want the log to describe, with no local changes. The
   log records the commit hash and whether the working tree had uncommitted
   changes.
2. Connect to the robot (Ethernet or USB, see Opentrons' documentation) and note
   its IP address.
3. Install the requirements and run:

```bash
pip install -r requirements.txt
python scripts/verify_ot2_hardware.py --host 169.254.x.x --operator "Your Name" \
    --notes "Deck empty, no pipettes attached. Lights on during read-only steps."
```

Add `--allow-upload` and then `--allow-motion` for the later tiers. For the
motion tier, clear the deck and keep hands out of the robot before typing `HOME`.

Write in `--notes` what you saw: lights, sounds, motion, anything unexpected. The
log shows what the tools returned; it cannot show what the robot physically did.

## Reading the result

* **verified**: every requested step passed. `tools_passed` lists exactly which
  tools that covers, and `tools_not_exercised` lists the rest.
* **failed**: at least one step failed. Commit that log too. A failed log is
  useful, and it is the honest record.
* **No log at all**: no robot answered. Nothing was run.
* **simulated**: the target was a test double (`mcp_servers/fake_ot2.py`). The
  script refuses to write those into `hardware_logs/`.

### If `read_deck` fails

`read_deck` falls back to a mock deck whenever it cannot read the robot, and the
script treats that as a failure, because a mock deck is not a read. The log's
`robot_openapi.has_labware_path` field says whether the robot's own API lists a
`/labware` path. If it does not, `read_deck` needs to be rewritten against an
endpoint the robot has, and the README should not claim it as verified until a
log shows it passing.

## Committing the log

Commit the file exactly as written, in its own commit. Do not edit it. Update the
README's status only to match the `tools_passed` of a committed log, and leave the
execution tools marked as not yet run unless a separate record covers them.

Other labs are welcome to contribute logs from their own robots: open a
"Hardware verification report" issue and attach or link the file.
