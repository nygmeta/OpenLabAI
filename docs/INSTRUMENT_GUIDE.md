# Instrument Connection Guide

How to connect OpenLabAI to each supported instrument. Status values match the
table in the README: **Implemented** (run against a physical instrument),
**Partial** (written, mock mode only), **Planned** (not built).

All commands are run from the repository root.

---

## Opentrons OT-2 — Implemented

**What you get:** The agent reads your actual deck over the Opentrons HTTP API,
reports run status, homes the robot, and generates a PyLabRobot protocol file.

**What you do not get:** protocol upload or execution. The server writes a file to
`protocols/`; you review it and run it through the Opentrons App. `home_robot()`
is the only tool that moves the robot.

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

## Hamilton STAR / STARlet — Planned

**Not built.** There is no `hamilton_server.py` in this repository. This section
describes the intended approach, not working software.

The plan is to wrap PyLabRobot's USB firmware interface, which would require a
Windows PC and the libusbK driver in place of Hamilton's default driver (see the
[PyLabRobot documentation](https://docs.pylabrobot.org)). This is the most useful
open contribution to the project.

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
