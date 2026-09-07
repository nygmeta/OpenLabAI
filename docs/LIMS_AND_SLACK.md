# LIMS and Slack

Two ways to reach the instruments without opening a terminal: connect the agent
to the laboratory's system of record, and let a scientist drive it from Slack.

Status, stated plainly: both are implemented and tested, but only against the
mock adapter and an offline dry run. Neither has been connected to a production
LIMS or a live Slack workspace.

---

## Why a LIMS connector

A protocol is only useful if it acts on the right samples. The concentrations
needed to normalise a library, the list of samples actually queued today, and
the record of what was done to them all live in the LIMS. Without a connector a
scientist retypes that data into a protocol by hand, which is where transcription
errors enter — and in a regulated laboratory a transcription error is a deviation.

## Supported systems

`mcp_servers/lims_server.py` drives every system through one declarative profile
table (`VENDOR_PROFILES`), so adding a system is configuration rather than code.

| Profile | System |
|---|---|
| `benchling` | Benchling |
| `labware` | LabWare LIMS |
| `starlims` | STARLIMS (Abbott Informatics) |
| `labvantage` | LabVantage |
| `samplemanager` | Thermo Fisher SampleManager |
| `genera` | RETISOFT Genera (workcell scheduler) |
| `generic` | Any conventional REST LIMS |
| `mock` | Deterministic sample data, no LIMS needed |

Endpoint paths follow each vendor's published REST conventions. **They have not
been tested against a live instance of every system.** Treat any profile other
than `mock` as a starting point to confirm against your own server; if a path
differs, correct the profile — the tool surface does not change.

HighRes Biosolutions **Cellario** is a workcell scheduler rather than a LIMS and
has its own connector at `mcp_servers/cellario_server.py`.

## Running it

```bash
python mcp_servers/lims_server.py                                        # mock
export OPENLAB_LIMS_TOKEN=...                                            # never a tool argument
python mcp_servers/lims_server.py --lims labware --base-url https://lims.example.org
```

Tools: `get_lims_status`, `list_worklist`, `get_sample`, `update_sample_status`,
`attach_run_record`.

Reads are ungated. **Writes are not**: `update_sample_status` and
`attach_run_record` change the laboratory's system of record and refuse without
`confirm=true`, the same gate that guards robot motion. `attach_run_record` puts
the OpenLabAI protocol hash and operator into the LIMS, so the system of record
carries a pointer to exactly what was physically executed.

Credentials come from `OPENLAB_LIMS_TOKEN` in the environment, never from a tool
argument, so a token cannot be supplied by an agent or captured in a transcript.

---

## Why Slack

The approval gate needs a human to authorise each physical action. That human is
rarely sitting at the terminal running the MCP server — they are at the bench, in
a meeting, or holding a phone. Slack is where laboratory teams already are, which
makes it a natural place to put the gate rather than a shortcut around it.

It also solves attribution. Slack knows who pressed the button, so the audit
record gains a real identity rather than whatever name the server was started
with.

## Running it

```bash
python integrations/slack_bot.py --dry-run          # no Slack, no robot
export SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-...
python integrations/slack_bot.py --instrument Hamilton_STAR --require-second-person
```

In Slack:

```
@openlab hamilton ngs cleanup 1.8x beads, 2 ethanol washes, elute 20 uL
```

The bot plans the protocol, runs it through the eval framework, and posts the
step list, the validation score and the protocol hash with **Approve and run**
and **Reject** buttons. Approve carries a second confirmation dialog.

## What Slack does not change

Pressing Approve does not bypass anything:

- The request **never executes on arrival**. It is planned, validated and posted;
  nothing physical happens until someone presses Approve.
- A protocol that **fails validation cannot be approved at all** — the button
  refuses, it does not warn.
- The **requester and the approver are both recorded**. With
  `--require-second-person`, the person who asked cannot approve their own
  request, which is how a laboratory implements two-person authorisation.
- On the OT-2 the instrument's **own protocol analysis still applies** on upload.
- **Stop is never gated**, in Slack as everywhere else.

Slack is a channel for the existing approval gate, not a replacement for it.

## The combined path

```
Scientist in Slack  ─▶  agent plans protocol
                            │
        LIMS worklist ──────┤  samples, concentrations
                            ▼
                    eval framework: deck constraints + acceptance criteria
                            │
                            ▼
                    Slack approval  ─── rejected ──▶  nothing happens
                            │
                        approved (named person)
                            ▼
                    instrument executes  ──▶  audit record (hash, operator)
                            │
                            ▼
                    attach_run_record  ──▶  LIMS system of record
```

## Tests

```bash
python integrations/test_slack_flow.py    # 12 checks
python evals/test_criteria.py             # 32 checks
```

The Slack tests assert the safety properties directly: that a message does not
execute on arrival, that self-approval is refused under the two-person rule, that
an approved request cannot be replayed, and that a protocol failing validation
cannot be approved.
