# The PyLabRobot connector

OpenLabAI's other servers talk to an instrument directly (the OT-2 server uses the
Opentrons HTTP API; the Biomek server uses COM). `mcp_servers/plr_server.py` is
different: it is built on [PyLabRobot](https://github.com/PyLabRobot/pylabrobot), an
open-source Python library that gives liquid handlers one common interface. The
connector adds a planning and approval layer on top of it.

**Status: simulation only.** Everything below runs on PyLabRobot's simulation-side
features. The live path, which drives an OT-2 through PyLabRobot's
`OpentronsOT2Backend`, is written but has never been run against a physical robot.

## How it connects to PyLabRobot

PyLabRobot separates what a protocol does from how a machine does it:

* A `LiquidHandler` exposes operations: `pick_up_tips`, `aspirate`, `dispense`,
  `drop_tips`, working on named resources (tip racks, plates) on a `Deck`.
* A **backend** turns those operations into commands for one machine.
  PyLabRobot ships backends for several robots. Writing a new one means
  subclassing a backend class and implementing its methods.

OpenLabAI's own backend is `TraceBackend` in `mcp_servers/plr_connector.py`. It
subclasses PyLabRobot's `SerializingBackend` and records each command instead of
sending it anywhere. The same `LiquidHandler` code runs against it or against a real
backend, which is what makes "simulate first, then run exactly that" possible.

## What it adds

1. **Plans are checked before anything can move.** A plan is a deck (OT-2 slots,
   labware, optional starting volumes) and a list of transfers. The connector runs it
   on `TraceBackend` with PyLabRobot's tip and volume tracking switched on, so a
   plan that aspirates liquid that is not in the well, uses a tip that is not there,
   or exceeds a tip's capacity is rejected, with the step number. Plans that use
   one tip across several source wells are flagged as carry-over risks.
2. **Approval is bound to the exact commands.** The plan card carries an approval
   token: a hash of the plan *and* of the command stream it generates. `execute_plan`
   simulates the plan again and refuses unless the token still matches, so a plan
   changed after approval, or a PyLabRobot version that would now send different
   commands, needs a new approval. A person still has to pass `confirm=true`. The
   token ties an approval to one exact plan; it is not a secret and it does not
   replace the person's decision.
3. **Evidence is computed, not claimed.** `describe_connector` and every plan card
   report what this connector has been shown to do on the selected backend, by
   reading committed logs in `hardware_logs/`. With no such log, the level is
   "simulation only" or "unverified". A live backend refuses to start unless a log
   exists or the operator passes `--accept-unverified`.

## Using it

```bash
python mcp_servers/plr_server.py                                      # simulation, no hardware
python mcp_servers/plr_server.py --backend ot2 --host 169.254.x.x \
       --accept-unverified --operator "Your Name"                    # a physical OT-2, supervised
```

Tools: `describe_connector`, `plan_protocol`, `get_plan`, `execute_plan`. Scope today:
an OT-2 deck, one channel, `transfer` steps, and the labware listed by
`describe_connector`.

## Limits

* The live path is untested on hardware. Nothing produces a hardware log for this
  connector yet; a verification script like `scripts/verify_ot2_hardware.py` is the
  next piece. Until a log exists, any live run is unverified and needs a person at
  the robot.
* The plan's starting volumes are assumptions about what is in the wells. The
  simulation cannot know if the physical plate differs.
* PyLabRobot's tracking flags are global to the Python process, so the connector
  sets them when it builds a deck.
* The approval token covers the commands as PyLabRobot generates them. It does not
  cover how a particular machine executes them.
