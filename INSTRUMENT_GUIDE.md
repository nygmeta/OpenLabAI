# 🔌 Instrument Connection Guide

How to connect Lab-Assistant to each supported instrument type.

---

## Opentrons OT-2 (Tier 1 — Full Live Control)

**What you get:** Real-time bidirectional control. The agent can read your actual deck, execute steps, and adapt mid-run.

**Requirements:**
- OT-2 connected to your computer via USB or on the same WiFi network
- Python 3.13+
- `pip install mcp pylabrobot`

**Setup:**
```bash
python mcp_servers/ot2_server.py --host 169.254.x.x  # replace with your OT-2 IP
```

Find your OT-2's IP address in the Opentrons App under Robot Settings → Networking.

**Test the connection:**
In Claude Desktop, type: `Read my OT-2 deck`

If it returns your actual labware, you're connected. 🎉

---

## Hamilton STAR / STARLet (Tier 2 — Full Programmatic Control)

**What you get:** Full control via PyLabRobot's USB firmware interface.

**Requirements:**
- Hamilton STAR or STARLet with USB connection
- Windows PC (USB driver required)
- `pip install mcp pylabrobot pywin32`
- libusbK driver installed (see [PyLabRobot docs](https://docs.pylabrobot.org))

**Setup:**
```bash
python mcp_servers/hamilton_server.py
```

**Note:** First-time setup requires installing the libusbK USB driver, which replaces Hamilton's default driver. Follow the [PyLabRobot Hamilton setup guide](https://docs.pylabrobot.org/hamilton.html) carefully. This is a one-time setup.

---

## Cellario Workcells (Tier 2 — COM Automation)

**What you get:** Orchestration of full integrated workcell runs — schedule batches, query device status, monitor queues.

**Requirements:**
- Windows PC running Cellario software
- Cellario version 6.x or higher (COM interface required)
- `pip install mcp pywin32`

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

## Beckman Coulter Biomek FXP (Tier 3 — File-Based)

**What you get:** The agent generates ready-to-open `.mth` method files. You open them in Biomek Software and run manually.

**Requirements:**
- Python 3.13+
- `pip install mcp pywin32`
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

All MCP servers run in **mock mode** if the robot hardware is not connected. The agent will:
- Return simulated deck layouts based on your configuration
- Generate valid protocol files
- Show the full Gantt timeline
- Simulate protocol execution in the GUI

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
→ Run `pip install mcp pywin32` in your terminal.

**"COM object not found" (Cellario/Biomek)**
→ Make sure the instrument software is open before starting the MCP server.
→ Try running terminal as Administrator.

**"No such file or directory" (Biomek .mth files)**
→ The server creates `C:\Biomek\Methods\` automatically. If it fails, create the folder manually.
