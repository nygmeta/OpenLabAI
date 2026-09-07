# OpenLabAI

**Natural language agents for lab robot control, built on the Model Context Protocol.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen.svg)](https://modelcontextprotocol.io)

OpenLabAI exposes liquid handling instruments to an LLM agent as a small set of
structured tools, so a scientist can describe a protocol in plain English and get
a reviewable protocol file back. It does not give the agent raw hardware access,
and it never moves a robot without a person explicitly approving that run.

---

## Project Status — 7 September 2026

This is an early-stage framework. What follows is the honest current state.

**Runs end to end today**

- The OT-2 MCP server: reads the deck over the Opentrons HTTP API, reports run
  status, homes the robot, generates protocols, and can upload and execute them
  under an explicit approval gate. The read, status, home and generate tools have
  been run against a physical OT-2. **The execution tools — `upload_protocol`,
  `start_run`, `pause_run`, `resume_run`, `stop_run` — have not yet been run
  against a physical OT-2.** They are written against the documented Opentrons
  HTTP API and their refusal paths are tested, but no robot has executed a
  protocol through them.
- The Biomek FXP MCP server: reads deck and method variables over COM and
  generates `.mth` method files. Generated files have been opened and validated
  in Biomek Software on a real instrument.
- The Hamilton MCP server: generates PyLabRobot protocols for a STAR/STARlet and
  dry-runs them through PyLabRobot's chatterbox backend, which logs every command
  instead of sending it to hardware. Verified in simulation only — no physical
  Hamilton has been connected.
- The eval framework (`evals/`): deck constraint checking, acceptance criteria
  per protocol type, scoring, run logging with a protocol hash, and a test suite
  (`python evals/test_criteria.py`, 32 checks).
- The browser GUI (`gui/BiomekAgent.html`): a single HTML file, no install.

**Partial**

- The Cellario MCP server: the four tools are implemented against the
  `CellarioAutomation.Application` COM interface, but this connector has only
  been exercised in mock mode. It has not been run against a physical workcell.
- The Hamilton connector is simulation-verified only, as above. A live connection
  additionally needs Windows, USB, and the libusbK driver, none of which has been
  exercised.

**Planned, not built**

- Tecan Freedom EVO connector.
- The `resources/` labware library. `protocols/` is created at runtime as the
  output directory for `create_protocol()`.

## Safety and Control

The architecture deliberately puts four barriers between an agent and a moving
robot. This is the part of the design that matters most.

**1. Instruments are exposed as structured tools, not raw hardware access.**
Each server publishes a small, fixed set of MCP tools with explicit JSON
schemas — `read_deck`, `get_run_status`, `create_protocol`, and so on. The agent
cannot issue arbitrary commands, open a socket to the instrument, or reach the
COM object directly. Anything not on the tool list is not reachable. The full
inventory is 9 tools for the OT-2, 4 for Hamilton, 3 for the Biomek FXP, and 4
for Cellario.

**2. Protocols are validated before execution.**
`evals/protocol_evals.py` checks a generated protocol against per-instrument
deck constraints (volume limits per tip type, valid slot and position names, tip
availability) and against acceptance criteria for the protocol type (required
steps, bead ratios, wash cycles, elution volume ranges). It returns a score, a
pass/fail, and an explicit list of violations.

**3. A human approves before anything physical happens.**
The OT-2 server can execute protocols, and the approval gate is where that is
made safe. `upload_protocol` talks to the robot but moves nothing: it uploads the
file, waits for the robot's own protocol analysis, and creates a run that sits
idle. `start_run` is the only tool that begins motion, and it refuses unless
`confirm=true` is passed — which represents a person's approval of that specific
protocol — and refuses again if the robot's analysis reported errors or if the
protocol fails the deck constraint checks. `stop_run` is deliberately never
gated, because stopping is the safe direction. The Biomek, Cellario and Hamilton
servers execute nothing at all: they write files a person runs through the vendor
software, and the Hamilton server's `simulate_protocol` uses PyLabRobot's
chatterbox backend, which never contacts hardware.

**4. Every run is logged.**
`evals/run_logger.py` writes a JSON audit record per run: operator, instrument,
protocol name, a SHA-256 hash of the protocol, eval score, generation method,
per-step start and completion timestamps, status, and errors. Intended for
GxP-adjacent environments where traceability is a requirement.

---

## The Problem

In the labs I have worked in, one automation engineer supports somewhere between
twenty and thirty scientists. Every new liquid handling method goes through that
one person, and scientists wait days or weeks. This ratio is my own observation
from six years at Guardant Health, Personalis, and Hexagon Bio; I am not aware of
a published survey establishing it as an industry-wide figure.

PyLabRobot ([Wierenga et al., 2023](https://doi.org/10.1101/2023.07.10.547733))
and Pioneer Labs ([ngs_library_prep](https://github.com/Pioneer-Research-Labs/ngs_library_prep))
replaced proprietary vendor software with Python, which removes the vendor lock-in
bottleneck. OpenLabAI is aimed at the next one: the scientist still has to know
Python. It adds a conversational layer on top, so the scientist describes the
experiment and reviews a generated protocol instead of writing one.

---

## Supported Instruments

Status values mean: **Implemented** — the tools work and have been run against a
physical instrument. **Partial** — the code is written but has only run in mock
mode. **Planned** — not built.

| Instrument | Status | Interface | What is implemented | What is not |
|---|---|---|---|---|
| Opentrons OT-2 | Implemented — partly verified on hardware | Opentrons HTTP API, port 31950 | `read_deck`, `get_run_status`, `home_robot`, `create_protocol` (Opentrons API and PyLabRobot formats), plus `upload_protocol`, `start_run`, `pause_run`, `resume_run`, `stop_run`. Read, status, home and generate have run against a physical OT-2. | The execution tools have not been run against a physical OT-2. Their refusal paths are tested; the successful-run path is not hardware-verified. |
| Beckman Biomek FXP | Implemented — verified on hardware | COM (`BiomekFX.Application`), Windows | `read_deck`, `get_variables`, `create_protocol` writing `.mth` XML. Generated methods opened and validated in Biomek Software. | No runtime execution — the FXP exposes no runtime API in its standard configuration. Methods are run manually. |
| Hamilton STAR/STARlet | Partial — simulation only | PyLabRobot STAR backend | `read_deck`, `get_status`, `create_protocol`, `simulate_protocol`. Generated protocols dry-run through PyLabRobot's chatterbox backend. | Never connected to a physical Hamilton. No execution tool. A live connection needs Windows, USB and the libusbK driver, none of it exercised. |
| Cellario workcells | Partial — mock mode only | COM (`CellarioAutomation.Application`), Windows | `schedule_run`, `get_device_status`, `query_queue`, `get_batch_list` are written against the documented COM interface. | Not yet run against a physical workcell. All results to date are from the mock responder. |
| Tecan Freedom EVO | Planned | PyLabRobot | Nothing. | Everything. |

Every server falls back to mock mode when its instrument is unreachable, and
labels the response `"mode": "mock"`. Mock output is for development and training
only; it is not evidence that a connector works on hardware.

---

## Quick Start

### Option 1: the browser GUI, no installation

Download [`gui/BiomekAgent.html`](gui/BiomekAgent.html), open it in Chrome, and
paste in a Claude API key. No Python, no terminal.

### Option 2: the MCP servers

**Requirements:** Python 3.13+, pip. The Cellario and Biomek servers need
Windows for COM; they start on macOS and Linux but run in mock mode only.

```bash
git clone https://github.com/nygmeta/OpenLabAI.git
cd OpenLabAI
pip install -r requirements.txt
```

Run the server for your instrument, from the repository root:

```bash
python mcp_servers/ot2_server.py --host 169.254.10.10   # Opentrons OT-2
python mcp_servers/hamilton_server.py --deck starlet    # Hamilton STAR/STARlet
python mcp_servers/biomek_server.py                     # Beckman Biomek FXP
python mcp_servers/cellario_server.py                   # Cellario (Windows)
```

The OT-2 server takes the deck it should generate protocols for, and the operator
name recorded in the audit log:

```bash
python mcp_servers/ot2_server.py --host 169.254.10.10 \
  --pipette p1000_single_gen2 --mount right \
  --tiprack opentrons_96_tiprack_1000ul --operator "your name"
```

Each server speaks MCP over stdio, so it will appear to hang when run directly —
that is a server waiting for a client on stdin, not a failure. Point Claude
Desktop at it by adding to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "openlab": {
      "command": "python",
      "args": ["/path/to/OpenLabAI/mcp_servers/ot2_server.py", "--host", "169.254.10.10"]
    }
  }
}
```

The config file lives at `%APPDATA%\Claude\claude_desktop_config.json` on Windows
and `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS.

Then, in Claude Desktop:

```
Read my OT-2 deck and tell me what is loaded
```
```
Plan an AMPure bead cleanup: 1.8x beads, 2x 80% ethanol wash, elute in 20 µL EB
```

The agent returns a plan and writes a protocol file to `protocols/`. To run it:

```
Upload that protocol to the robot and tell me what the analysis says
```

`upload_protocol` sends the file, waits for the robot's own analysis, and creates
a run that sits idle — nothing moves. Review the protocol, then approve it
explicitly:

```
I have reviewed it. Start the run.
```

The agent calls `start_run` with `confirm=true`. It will refuse if the robot's
analysis reported errors, or if the protocol fails the deck constraint checks.
`stop_run` is never gated.

---

## Repository Structure

```
OpenLabAI/
├── gui/
│   └── BiomekAgent.html          # Standalone web GUI — open in Chrome, no install
├── mcp_servers/
│   ├── ot2_server.py             # Opentrons OT-2 (HTTP API, can execute)
│   ├── hamilton_server.py        # Hamilton STAR/STARlet (PyLabRobot, simulation)
│   ├── biomek_server.py          # Beckman Biomek FXP (Windows COM, file-based)
│   └── cellario_server.py        # Cellario workcells (Windows COM)
├── evals/
│   ├── protocol_evals.py         # Deck constraints, acceptance criteria, scoring
│   ├── run_logger.py             # Audit trail and run logging
│   └── test_criteria.py          # Tests for criteria matching and score ranges
├── examples/
│   ├── ngs_cleanup_example.md    # Step-by-step walkthrough
│   └── slas_boston_case_study.md # SLAS 2026 Boston live demo case study
├── docs/
│   ├── INSTRUMENT_GUIDE.md       # How to connect each instrument
│   └── SCIENTIST_GUIDE.md        # For scientists with no coding background
├── requirements.txt
├── LICENSE
└── README.md
```

`protocols/` is not checked in. It is created at runtime by `create_protocol()`
as the output directory for generated protocol files.

---

## Eval Framework

`evals/` validates AI-generated protocols before a human is asked to approve them.

- **Deck constraint checker** — volume limits per tip type, valid slot and
  position names, and tip availability, per instrument.
- **Acceptance criteria** — per protocol type (NGS cleanup, normalization, serial
  dilution, simple transfer): required steps, bead ratio bounds, minimum wash
  cycles, elution volume range. Step labels are matched by token and synonym, so
  a criterion is satisfied by the wordings scientists actually use.
- **Run logger** — operator, timestamp, per-step status, and SHA-256 protocol hash.

Run the bundled example and the tests:

```bash
python evals/protocol_evals.py
python evals/run_logger.py
python evals/test_criteria.py
```

Criteria matching is token-based with synonyms, not literal substring matching,
so ordinary wordings are recognised: "Aspirate AMPure beads" and "Add 90 uL SPRI
beads to sample" both satisfy the `aspirate_beads` criterion. It is not merely
permissive — "Aspirate 50 uL of sample" does not satisfy it, and "Wash with PBS"
does not satisfy `ethanol_wash`. `evals/test_criteria.py` covers both directions
plus score-range and constraint checks, and exits non-zero on failure.

```python
from evals.protocol_evals import evaluate_protocol

result = evaluate_protocol(protocol, protocol_type="ngs_cleanup", instrument="OT-2")
print(result.overall_score)   # float in 0.0–1.0
print(result.passed)          # True when acceptance passes and score >= 0.80
print(result.protocol_hash)   # first 16 hex chars of the SHA-256 protocol hash
```

Scores depend entirely on the protocol you pass in. No benchmark figure is
claimed here, because no benchmark has been run.

---

## How It Works

```
Scientist (plain English)
        ↓
Claude agent (protocol planning)
        ↓
MCP server (structured tools: read_deck, create_protocol, upload_protocol, ...)
        ↓
Opentrons HTTP API  /  PyLabRobot  /  Windows COM  /  .mth file output
        ↓
Robot analysis + deck constraint checks
        ↓
Human approval (confirm=true)  →  physical robot
        ↓
Audit log
```

The design borrows the generate-several-candidates-and-select idea from
Scattered Forest Search ([Light et al., 2024](https://codespace-optimization.github.io/)),
which reports its own results on code generation benchmarks. Those results are
for code generation, not laboratory protocols, and no equivalent measurement has
been made for OpenLabAI. Any first-attempt validity rate for protocol generation
would need its own benchmark, which has not been run.

---

## Contributing

Contributions welcome, especially from wet lab scientists who can say what is
missing.

- **Found a bug?** Open a GitHub issue.
- **New instrument backend?** Open a PR adding a server under `mcp_servers/`.
  Tecan is the remaining gap; the Hamilton connector needs someone with physical
  hardware to verify it.
- **Protocol templates or labware definitions?** Open an issue first — there is
  no library structure yet and it is worth agreeing on one.

---

## References

If you use OpenLabAI, please link to this repository. There is no paper to cite.

This work builds on:

- [PyLabRobot](https://github.com/PyLabRobot/pylabrobot) — Wierenga, Golas, Ho and
  Coley, 2023. [doi:10.1101/2023.07.10.547733](https://doi.org/10.1101/2023.07.10.547733)
- [Scattered Forest Search](https://codespace-optimization.github.io/) — Light et al., 2024
- [Pioneer Labs NGS library prep](https://github.com/Pioneer-Research-Labs/ngs_library_prep)

---

## About

Built by **Ainur Nygmet** at ZenoVistaAI Inc.

Six years as a lab automation engineer at Guardant Health, Personalis, and
Hexagon Bio: 50+ Hamilton methods developed, 40+ scientists trained on lab
automation, certified Cellario operator. Demonstrated live AI-driven workcell
orchestration at the SLAS 2026 International Conference and Exhibition in Boston,
February 2026 — see [examples/slas_boston_case_study.md](examples/slas_boston_case_study.md).

This project exists because I was the bottleneck.

**Contact:** nygmetainur@gmail.com ·
[LinkedIn](https://linkedin.com/in/nygmetainur) ·
[GitHub](https://github.com/nygmeta)

---

## License

MIT. See [LICENSE](LICENSE).
