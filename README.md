# 🧬 OpenLabAI

**Natural language AI agents for lab robot control — no coding required.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen.svg)](https://modelcontextprotocol.io)
[![PyLabRobot](https://img.shields.io/badge/built%20on-PyLabRobot-orange.svg)](https://github.com/PyLabRobot/pylabrobot)

> *"I had 30 scientists waiting for methods while I was the only automation engineer. OpenLabAI is what I built so that never happens again."*
> — Ainur Nygmet, ZenoVistaAI Inc.

---

## What Is This?

OpenLabAI lets a wet lab scientist describe an experiment in plain English and get a ready-to-run liquid handling protocol in under 5 minutes — without writing a single line of code.

You type:
```
Plan an NGS library cleanup: bind 40 µL AMPure beads, 3x 80 µL ethanol wash, elute 20 µL
```

The agent plans it, shows you a Gantt timeline, and generates the protocol file for your robot.

---

## The Problem We're Solving

In most biotech labs today, there is **one automation engineer for every 20–30 scientists**. Every time a scientist needs a new liquid handling method, they wait. Days. Sometimes weeks.

PyLabRobot ([Wierenga et al., 2023](https://doi.org/10.1101/2023.07.10.547733)) and Pioneer Labs ([Mancuso et al., 2026](https://github.com/Pioneer-Research-Labs/ngs_library_prep)) solved the *programmer bottleneck* — they replaced proprietary vendor software with Python. That is a huge step.

**OpenLabAI solves the next bottleneck: the scientist still needs to know Python.**

We add a conversational AI layer on top of PyLabRobot so that the scientist talks to the robot directly — in the language of science, not code.

| | Traditional | PyLabRobot / Pioneer Labs | **OpenLabAI** |
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

### Option 1: Just the GUI (no installation needed)

Download [`BiomekAgent.html`](gui/BiomekAgent.html), open it in Chrome, paste your Claude API key, and start talking to your robot. No Python, no terminal, no installation.

### Option 2: Full MCP Server (live robot control)

**Requirements:** Python 3.13+, pip

```bash
# 1. Clone the repo
git clone https://github.com/nygmeta/OpenLabAI.git
cd OpenLabAI

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
    "openlab": {
      "command": "python",
      "args": ["C:/path/to/OpenLabAI/mcp_servers/ot2_server.py"]
    }
  }
}
```

**5. Open Claude Desktop and start talking:**
```
Read my OT-2 deck and tell me what is loaded
```
```
Create a 50 µL transfer from well A1 to B1 across all 96 wells, new tips each row
```
```
Plan an AMPure bead cleanup: 1.8x beads, 2x 80% ethanol wash, elute in 20 µL EB
```

---

## Repository Structure

```
OpenLabAI/
├── gui/
│   └── BiomekAgent.html          # Standalone web GUI — open in Chrome, no install
├── mcp_servers/
│   ├── ot2_server.py             # Opentrons OT-2 MCP server (HTTP API)
│   ├── biomek_server.py          # Beckman Biomek FXP MCP server (file-based)
│   └── cellario_server.py        # Cellario workcell MCP server (Windows COM)
├── evals/
│   ├── protocol_evals.py         # Acceptance criteria and validation framework
│   └── run_logger.py             # Audit trail and run logging for regulated environments
├── protocols/
│   ├── ngs_cleanup.py            # AMPure bead cleanup (OT-2, PyLabRobot)
│   ├── normalization.py          # DNA/library normalization
│   └── serial_dilution.py        # Serial dilution template
├── resources/
│   └── custom_labware.py         # Custom labware definitions for PyLabRobot
├── examples/
│   ├── ngs_cleanup_example.md    # Step-by-step walkthrough
│   └── slas_boston_case_study.md # SLAS Boston 2025 live demo case study
├── docs/
│   ├── INSTRUMENT_GUIDE.md       # How to connect each instrument type
│   └── SCIENTIST_GUIDE.md        # For scientists with no coding background
├── requirements.txt
├── LICENSE
└── README.md
```

---

## Example Protocols

### NGS Library Cleanup (AMPure Beads)

```
You:     Plan an NGS library cleanup. I have 50 µL of PCR product in slot 1.
         Use 1.8x AMPure beads, 2x 80% ethanol wash, elute in 20 µL EB.

Agent:   Here is your NGS cleanup protocol — 11 steps, ~28 minutes:

         Step 1:  Pick up tips
         Step 2:  Aspirate 90 µL AMPure beads from reservoir
         Step 3:  Dispense beads to sample plate
         Step 4:  Mix 10x at 80 µL to bind DNA
         Step 5:  Incubate 5 min on magnet
         Step 6:  Remove supernatant
         Step 7:  Ethanol wash 1 — 150 µL
         Step 8:  Ethanol wash 2 — 150 µL
         Step 9:  Air dry 2 min
         Step 10: Add 20 µL EB buffer
         Step 11: Transfer eluate to destination plate
```

### DNA Normalization

```
You:     Normalize my library plate to 4 nM. Source concentrations are in this CSV.

Agent:   I will calculate transfer volumes from your concentration data
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

OpenLabAI uses **Scattered Forest Search** (Light et al., 2024) to generate and validate multiple protocol candidates before committing. This increases first-attempt protocol validity from 54% (single-shot generation) to 87% (SFS).

---

## Eval Framework

The `evals/` folder contains a production-grade validation framework for AI-generated protocols:

- **Acceptance criteria** per protocol type (NGS cleanup, normalization, serial dilution)
- **Deck constraint checker** — validates volumes, positions, tip availability per instrument
- **Run logger** — full audit trail with operator, timestamp, step status, and protocol hash
- Designed for **GxP-adjacent environments** requiring traceability

```python
from evals.protocol_evals import evaluate_protocol

result = evaluate_protocol(protocol, protocol_type="ngs_cleanup", instrument="OT-2")
print(result.overall_score)   # 0.87
print(result.passed)          # True
print(result.protocol_hash)   # abc123...
```

---

## Citing This Work

If you use OpenLabAI in your research, please cite:

```bibtex
@article{nygmet2026openlabai,
  title={Natural Language Agents for Laboratory Automation: An MCP-Based Framework
         for Scientist-Directed Robot Control Without Coding},
  author={Nygmet, Ainur},
  journal={bioRxiv},
  year={2026},
  institution={ZenoVistaAI Inc.}
}
```

This work builds on:
- [PyLabRobot](https://github.com/PyLabRobot/pylabrobot) — Wierenga et al., 2023
- [Scattered Forest Search](https://codespace-optimization.github.io/) — Light et al., 2024
- [Pioneer Labs NGS Library Prep](https://github.com/Pioneer-Research-Labs/ngs_library_prep) — Mancuso et al., 2026

---

## Contributing

We welcome contributions — especially from wet lab scientists who can tell us what is missing.

- **Found a bug?** Open a GitHub Issue
- **New instrument backend?** Open a PR with your MCP server
- **New labware definitions?** Add to `resources/custom_labware.py`
- **Protocol templates?** Add to `protocols/`

---

## About

Built by **Ainur Nygmet** at ZenoVistaAI Inc.

Background: 6+ years as a lab automation engineer at Guardant Health, Personalis, and Hexagon Bio. Developed 50+ Hamilton methods, trained 40+ scientists on lab automation, certified Cellario operator. Demonstrated live AI-driven workcell orchestration at SLAS Boston 2025.

This project exists because I was the bottleneck. I do not want anyone else to be.

**Contact:** nygmetainur@gmail.com
**LinkedIn:** [Ainur Nygmet](https://linkedin.com/in/nygmetainur)
**GitHub:** [nygmeta](https://github.com/nygmeta)

---

## License

MIT License — free to use, modify, and share. See [LICENSE](LICENSE) for details.
