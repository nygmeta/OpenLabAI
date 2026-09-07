# Instrument Connection Guide

How to connect OpenLabAI to each supported instrument. Status values match the
table in the README: **Implemented** (run against a physical instrument),
**Partial** (written, mock mode only), **Planned** (not built).

All commands are run from the repository root.

---

## Opentrons OT-2 — Implemented (execution not yet hardware-verified)

**What you get:** The agent reads your actual deck over the Opentrons HTTP API,
reports run status, homes the robot, and generates a PyLabRobot protocol file.

**Execution.** The server can also upload and run protocols, behind an approval
gate:

1. `create_protocol` writes an Opentrons Python API file to `protocols/`.
2. `upload_protocol` sends it to the robot, waits for the robot's own analysis,
   and creates a run in the **idle** state. Nothing moves. It returns any
   analysis errors and any deck constraint violations.
3. You review the protocol.
4. `start_run` begins execution, and only with `confirm=true`. It refuses if the
   analysis reported errors or the constraint checks failed.
5. `pause_run`, `resume_run` and `stop_run` control the run. `stop_run` is never
   gated — stopping is always allowed.

Every start, pause, resume and stop is written to the audit log.

**Status:** the read, status, home and generate tools have been run against a
physical OT-2. The execution tools have not — they are written against the
documented Opentrons HTTP API and their refusal paths are tested, but no robot
has yet executed a protocol through them. Treat the first hardware run as a
commissioning step: use a plate of water, watch it, and keep a hand near the
stop.

**Setting your deck.** Generated protocols use the pipette and tip rack you pass
at startup:

```bash
python mcp_servers/ot2_server.py --host 169.254.10.10 \
  --pipette p1000_single_gen2 --mount right \
  --tiprack opentrons_96_tiprack_1000ul --operator "your name"
```

`create_protocol` also takes a `format`: `opentrons` (default, uploadable and
runnable) or `pylabrobot` (a script that runs on your computer and drives the
robot over the network — it cannot be uploaded).

**Requirements:**
- OT-2 connected to your computer via USB or on the same WiFi network
- Python 3.13+
- `pip install -r requirements.txt`

**Setup:**
```bash
python mcp_servers/ot2_server.py --host 169.254.x.x  # replace with your OT-2 IP
```

Find your OT-2's IP address in the Opentrons App under Robot Settings → Networking.

**Test the connection:**
In Claude Desktop, type: `Read my OT-2 deck`

If it returns your actual labware, you're connected. 🎉

---

## Hamilton STAR / STARlet — Partial (simulation only)

**Status:** the connector exists and generates protocols that dry-run
successfully through PyLabRobot's chatterbox backend. **It has never been
connected to a physical Hamilton.** Everything below the simulation step is
untested.

**What you get:**
- `read_deck` — the deck geometry the server builds: a `TIP_CAR_480_A00` tip
  carrier on rail 1 holding `tips_1`–`tips_5`, and a `PLT_CAR_L5AC_A00` plate
  carrier on rail 10 holding `plate_1`–`plate_5`. Address labware by those names
  plus a column number 1–12.
- `create_protocol` — writes a PyLabRobot script to `protocols/`.
- `simulate_protocol` — dry-runs it through `LiquidHandlerChatterboxBackend`,
  which logs every command instead of sending it to hardware. Safe with no
  instrument attached.
- `get_status` — reports connection state and PyLabRobot availability.

**Setup:**
```bash
python mcp_servers/hamilton_server.py --deck starlet   # or --deck star
```

**Requirements for simulation:** just `pip install -r requirements.txt`. No
hardware, no Windows.

**Requirements for a live run (not exercised):** Windows, a STAR or STARlet on
USB, and the libusbK driver in place of Hamilton's default driver. See the
[PyLabRobot documentation](https://docs.pylabrobot.org). A generated protocol run
without `--simulate` will attempt to drive real hardware.

**Verifying a protocol before hardware:**
```bash
python protocols/YourProtocol.py --simulate
```

Someone with a physical STAR or STARlet verifying this connector would be the
most useful contribution to the project.

---

## Cellario Workcells — Partial (mock mode only)

**Status:** the four tools below are implemented against the documented
`CellarioAutomation.Application` COM interface, but this connector has only been
exercised in mock mode. It has not been run against a physical workcell. Treat
the instructions below as untested.

**What it is intended to do:** orchestrate integrated workcell runs — schedule
batches, query device status, monitor queues.

**Requirements:**
- Windows PC running Cellario software
- Cellario version 6.x or higher (COM interface required)
- `pip install -r requirements.txt`

**Setup:**
```bash
python mcp_servers/cellario_server.py
```

**Important:** Cellario must be open and in an idle state before starting the MCP server. The server connects via `CellarioAutomation.Application` COM object.

**What the agent can do:**
- `schedule_run()` — start a workcell batch from a batch definition
- `get_device_status()` — check status of any device (liquid handler, centrifuge, plate reader, hotel)
- `query_queue()` — see what's in the automation queue

---

## Beckman Coulter Biomek FXP — Implemented (file-based)

**What you get:** The agent generates ready-to-open `.mth` method files. You open them in Biomek Software and run manually.

**Requirements:**
- Python 3.13+
- `pip install -r requirements.txt`
- Biomek Software installed (for opening and validating generated files)

**Setup:**
```bash
python mcp_servers/biomek_server.py
```

**Workflow:**
1. Describe your protocol to the agent
2. Agent generates a `.mth` file saved to `C:\Biomek\Methods\`
3. Open Biomek Software → File → Open → navigate to `C:\Biomek\Methods\`
4. Run **Method → Validate** to check for errors
5. Run the method

**Why no live control?** The Biomek FXP does not expose a runtime COM API in its standard configuration. The file-based approach reduces method development time from hours to minutes while maintaining the validation step that experienced automation engineers know is critical for safe robot operation.

---

## Running Without a Robot (Demo Mode)

All MCP servers run in **mock mode** if the instrument is not reachable, and label
the response `"mode": "mock"`. In mock mode the agent will:
- Return a canned deck layout
- Generate protocol files (this part is real — the files are the same either way)
- Simulate protocol execution in the GUI

Mock output is for development and training. It is not evidence that a connector
works against hardware.

This is useful for:
- Learning the system before connecting hardware
- Developing and reviewing protocols before robot time
- Demonstrations and training

---

## Troubleshooting Connection Issues

**"Could not connect to OT-2"**
→ Check the IP address. Try pinging it: `ping 169.254.x.x` in terminal.
→ Make sure the Opentrons App is not currently controlling the robot.

**"ModuleNotFoundError: No module named 'mcp'"**
→ Run `pip install -r requirements.txt` from the repository root.

**"COM object not found" (Cellario/Biomek)**
→ Make sure the instrument software is open before starting the MCP server.
→ Try running terminal as Administrator.

**"No such file or directory" (Biomek .mth files)**
→ The server creates `C:\Biomek\Methods\` automatically. If it fails, create the folder manually.

---

## "The server just hangs when I run it"

That is expected. MCP servers speak JSON-RPC over stdin and stdout, so a server
started directly from a terminal sits waiting for a client. Point Claude Desktop
at it via `claude_desktop_config.json` (see the README Quick Start) rather than
running it by hand.

## "AttributeError: 'Server' object has no attribute 'list_tools'"

You have `mcp` 2.x installed. These servers use the low-level `Server` decorator
API from `mcp` 1.x. Install the pinned version:

```bash
pip install -r requirements.txt
```

## "The protocol references a resource that is not on the deck"

PyLabRobot raises `ResourceNotFoundError` when a protocol addresses labware the
deck does not have. On the Hamilton server the valid names are `tips_1`–`tips_5`
and `plate_1`–`plate_5`; on the OT-2 they are `source`, `destination` and
`reservoir`, optionally qualified as `source:A1`. `read_deck` lists them.
