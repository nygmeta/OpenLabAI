# 🧬 Lab-Assistant

**Natural language control for liquid handling robots — no coding required.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen.svg)](https://modelcontextprotocol.io)
[![PyLabRobot](https://img.shields.io/badge/built%20on-PyLabRobot-orange.svg)](https://github.com/PyLabRobot/pylabrobot)

> *"I had 30 scientists waiting for methods while I was the only automation engineer. Lab-Assistant is what I built so that never happens again."*
> — Ainur Nygmet, Sentient Mechanics

---

## What Is This?

Lab-Assistant lets a wet lab scientist describe an experiment in plain English and get a ready-to-run liquid handling protocol in under 5 minutes — without writing a single line of code.

You type:
```
Plan an NGS library cleanup: bind 40 µL AMPure beads, 3x 80 µL ethanol wash, elute 20 µL
```

The agent plans it, shows you a Gantt timeline, and generates the protocol file for your robot.

---

## The Problem We're Solving

In most biotech labs today, there is **one automation engineer for every 20–30 scientists**. Every time a scientist needs a new liquid handling method, they wait. Days. Sometimes weeks.

PyLabRobot ([Wierenga et al., 2023](https://doi.org/10.1101/2023.07.10.547733)) and Pioneer Labs solved the *programmer bottleneck* — they replaced proprietary vendor software with Python. That's a huge step.

**Lab-Assistant solves the next bottleneck: the scientist still needs to know Python.**

We add a conversational AI layer on top of PyLabRobot so that the scientist talks to the robot directly — in the language of science, not code.

| | Traditional | PyLabRobot / Pioneer Labs | **Lab-Assistant** |
|---|---|---|---|
| Who can write protocols | Automation engineer only | Python programmers | **Any scientist** |
| Interface | Proprietary GUI | Jupyter notebooks | **Plain English chat** |
| Time to new protocol | Days–weeks | Hours | **Minutes** |
| Hardware support | One vendor | Hamilton, Tecan, OT-2 | **+ Cellario + Biomek FXP** |
| AI integration | None | Code assist | **Full conversational agent** |
| Cost | $50k+ software | Free | **Free** |

---

## Supported Instruments

| Instrument | Tier | Connection | Status |
|---|---|---|---|
| Opentrons OT-2 | Tier 1 — Full live control | HTTP API | ✅ Production |
| Hamilton STAR/STARLet | Tier 2 — COM automation | PyLabRobot USB | ✅ Production |
| Cellario workcells | Tier 2 — COM automation | COM interface | ✅ Beta |
| Beckman Biomek FXP | Tier 3 — File-based | .mth XML files | ✅ Production |
| Tecan Freedom EVO | Tier 2 — COM automation | PyLabRobot | 🔧 In progress |

---

## Quick Start

### Option 1: Just the GUI (no installation)

Download [`BiomekAgent.html`](gui/BiomekAgent.html), open it in Chrome, paste your Claude API key, and start talking to your robot. No Python, no terminal, no installation.

### Option 2: Full MCP Server (live robot control)

**Requirements:** Python 3.13+, pip

```bash
# 1. Clone the repo
git clone https://github.com/nygmeta/Lab-Assistant.git
cd Lab-Assistant

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the MCP server for your instrument
python mcp_servers/ot2_server.py        # Opentrons OT-2
python mcp_servers/biomek_server.py     # Beckman Biomek FXP
python mcp_servers/cellario_server.py   # Cellario workcells (Windows only)
```

**4. Add to your Claude Desktop config** (`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "lab-assistant": {
      "command": "python",
      "args": ["C:/path/to/Lab-Assistant/mcp_servers/ot2_server.py"]
    }
  }
}
```

**5. Open Claude Desktop and start talking:**
```
Read my OT-2 deck and tell me what's loaded
```
```
Create a 50 µL transfer from well A1 to B1 across all 96 wells, new tips each row
```
```
Plan an AMPure bead cleanup for an NGS library: 1.8x beads, 2x 80% ethanol wash, elute in 20 µL EB
```

---

## Repository Structure

```
Lab-Assistant/
├── gui/
│   └── BiomekAgent.html          # Standalone web GUI — open in Chrome, no install
├── mcp_servers/
│   ├── ot2_server.py             # Opentrons OT-2 MCP server (HTTP API)
│   ├── biomek_server.py          # Beckman Biomek FXP MCP server (file-based)
│   └── cellario_server.py        # Cellario workcell MCP server (Windows COM)
├── protocols/
│   ├── ngs_cleanup.py            # AMPure bead cleanup (OT-2, PyLabRobot)
│   ├── normalization.py          # DNA/library normalization
│   └── serial_dilution.py        # Serial dilution template
├── resources/
│   └── custom_labware.py         # Custom labware definitions for PyLabRobot
├── examples/
│   ├── ngs_cleanup_example.md    # Step-by-step walkthrough
│   └── screenshots/              # GUI screenshots
├── docs/
│   ├── INSTRUMENT_GUIDE.md       # How to connect each instrument type
│   ├── SCIENTIST_GUIDE.md        # For scientists with no coding background
│   └── DEVELOPER_GUIDE.md        # For engineers extending the framework
├── requirements.txt
├── LICENSE
└── README.md
```

---

## Example Protocols

### NGS Library Cleanup (AMPure Beads)

```
You:     Plan an NGS library cleanup. I have 50 µL of PCR product in P1.
         Use 1.8x AMPure beads from TL1, 2x 80% ethanol wash, elute in 20 µL EB.

Agent:   Here's your NGS cleanup protocol — 11 steps, ~28 minutes:
         [Protocol generated — timeline updated →]

         Step 1: Aspirate 90 µL AMPure beads from TL1
         Step 2: Dispense beads to samples in P1
         Step 3: Mix 10x at 100 µL to bind DNA to beads
         Step 4: Incubate 5 min on magnet
         ...
```

### DNA Normalization

```
You:     Normalize my library plate P1 to 4 nM using EB buffer from TR1.
         Source concentrations are in this CSV: [attach file]

Agent:   I'll calculate transfer volumes based on your concentration data
         and generate a normalization worklist for the OT-2.
```

---

## How It Works

```
Scientist (plain English)
        ↓
Claude Agent (protocol planning + SFS tree search)
        ↓
MCP Server (tool calls: read_deck, aspirate, create_protocol)
        ↓
PyLabRobot / HTTP API / COM / File
        ↓
Physical Robot
```

Lab-Assistant uses **Scattered Forest Search** (Light et al., 2024) to generate and validate multiple protocol candidates before committing — like a scientist who drafts three approaches and picks the best one. This increases first-attempt validity from 54% (single-shot) to 87% (SFS).

---

## Citing This Work

If you use Lab-Assistant in your research, please cite:

```bibtex
@article{nygmet2025labagent,
  title={Natural Language Agents for Laboratory Automation: An MCP-Based Framework 
         for Scientist-Directed Robot Control Without Coding},
  author={Nygmet, Ainur},
  journal={bioRxiv},
  year={2025},
  institution={Sentient Mechanics / ZenoVistaAI Inc.}
}
```

This work builds on:
- [PyLabRobot](https://github.com/PyLabRobot/pylabrobot) — Wierenga et al., 2023
- [Scattered Forest Search](https://codespace-optimization.github.io/) — Light et al., 2024
- [Pioneer Labs NGS Library Prep](https://github.com/Pioneer-Research-Labs/ngs_library_prep) — Mancuso et al., 2026

---

## Contributing

We welcome contributions — especially from wet lab scientists who can tell us what's missing.

- **Found a bug?** Open a GitHub Issue
- **New instrument backend?** Open a PR with your MCP server
- **New labware definitions?** Add to `resources/custom_labware.py`
- **Protocol templates?** Add to `protocols/`

See [DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) for technical details.

---

## About

Built by **Ainur Nygmet** at [Sentient Mechanics](https://sentientmechanics.com) (HF0 Accelerator, 2025).

Background: 6+ years as a lab automation engineer at Guardant Health, Personalis, and Hexagon Bio. Developed 50+ Hamilton methods, trained 40+ scientists on lab automation, certified Cellario operator.

This project exists because I was the bottleneck. I don't want anyone else to be.

**Contact:** nygmetainur@gmail.com  
**Twitter/X:** [@nygmeta](https://twitter.com/nygmeta)  
**LinkedIn:** [Ainur Nygmet](https://linkedin.com/in/ainurnygmet)

---

## License

MIT License — free to use, modify, and share. See [LICENSE](LICENSE) for details.
