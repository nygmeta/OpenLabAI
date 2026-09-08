# Humanoid teleoperation

How the Unitree G1 fits into OpenLabAI, and why the agent is deliberately not
allowed to drive it.

Status. The teleoperation system and its operator console are built and have
been demonstrated publicly, including at SLAS 2026 in Boston. What lives in
*this repository* is the session, safety and integration layer around that
system — the `teleop/` package, `mcp_servers/teleop_server.py` and
`integrations/teleop_bridge.py`. It is tested offline and has not been run
against the physical robot.

Two things are deliberately **not** in this repository. The control loop is
Unitree Robotics' open-source `xr_teleoperate`, which is theirs and is not
vendored here. The **G1 Teleop Console** — ZenoVistaAI's browser-based operator
interface — is a separate application and is not published under this licence.
This layer is what a laboratory workflow needs in order to reason about whether
a teleoperated step may proceed, and to produce the record that it did.

---

## Why a humanoid at all

Fixed automation covers the repetitive, precisely specified part of laboratory
work: liquid handling, plate sealing, spinning, cycling, reading. That is what
the rest of this repository connects to.

It does not cover the rest. Opening a reagent box that is not a labware format.
Retrieving a sample from a shared cold room. Loading a bench instrument that was
never designed for robotic access. Recovering a plate that a gripper left
crooked. These defeat fixed automation not because they are difficult but
because they are unstructured, and a workcell that meets one of them stops.

A humanoid under live teleoperation handles that class of work, because a person
is doing it — at a distance, with their own judgement, through a robot's hands.

---

## The control loop

The underlying extended-reality control loop is Unitree Robotics' open-source
[`xr_teleoperate`](https://github.com/unitreerobotics/xr_teleoperate) framework.
OpenLabAI does not reimplement it and does not send joint commands.

The loop runs continuously, thirty or more times a second:

| Stage | What happens | Component in xr_teleoperate |
|---|---|---|
| Tracking | Headset reports head and wrist pose and per-finger joints | `televuer`, built on Vuer, over a secure WebSocket |
| Retargeting | Human finger poses map onto the robot hand's actual joints | `dex-retargeting` |
| Inverse kinematics | Joint angles computed to place the wrist where the operator's is | Pinocchio rigid-body dynamics |
| Command and filtering | Targets smoothed, checked against joint limits, streamed | weighted moving filter, `unitree_sdk2` |
| Feedback | Robot head camera returns to the headset | `teleimager` over WebRTC |

Below roughly 100 ms end to end, fine manipulation is practical. Above it, the
operator slows down to stay accurate.

`xr_teleoperate` supports the G1 in its 29-degree-of-freedom configuration with
Dex3, Inspire and BrainCo hands and the Dex1 gripper, and Apple Vision Pro,
PICO 4 or Meta Quest 3 as the headset. It includes an episode writer that records
synchronised video and joint trajectories for imitation learning, and safety
behaviours: an initial pose alignment step before control begins, a soft
emergency stop that drops the robot into damping mode, and an automatic return
to a safe pose on exit.

ZenoVistaAI's contribution on top of that foundation is the **G1 Teleop Console**:
browser-based session control, per-finger hand calibration for the Inspire hands,
LiDAR point-cloud visualisation, camera feeds, and diagnostics — the layer that
turns a research-grade control loop into something a laboratory can configure,
operate and supervise from one screen. Authorship of the underlying framework is
Unitree's and is not claimed here.

---

## Why the agent does not drive the robot

Everywhere else in OpenLabAI an agent proposes an action and a human approves it
before anything moves. That model does not transfer to teleoperation.

Teleoperation is a continuous motion stream reproducing an operator's hands in
real time. There is no discrete action for a human to approve, and no meaningful
point at which an agent could be handed the stream and still leave a person in
command. So the agent is not given the robot.

What the agent gets instead is the ability to **request a session**:

```
request_session(
    task_type       = "open_container",
    instruction     = "Fetch the AMPure reagent box from the cold room, open it,
                       and place the reservoir in deck position 5.",
    reason          = "The box is not a labware format any deck accepts and the
                       lid needs a two-handed opening motion.",
    expected_outcome= "Reservoir seated in position 5, lid set aside.",
)
```

A human accepts the request, performs it wearing the headset, and closes it. The
bridge records the request, the operator, the duration and the outcome, so a
teleoperated step leaves the same kind of audit record as a robot step.

## What the bridge refuses

The bridge is not a general-purpose robot API, and it pushes back on requests
that should not be humanoid work:

- **Work that belongs on an instrument.** A request to pipette, aspirate,
  dispense, centrifuge, seal or incubate is refused with a pointer to the
  connector that should do it. Teleoperation is not a way around a connector
  that has not been written yet.
- **Requests with no stated reason.** The caller must say why fixed automation
  cannot do this. That field exists to make the alternative explicit.
- **Task types outside the suitable list**: retrieve, open a container, load an
  instrument, transport, inspect, recover.

Aborting a session is never gated, consistent with stopping any instrument
elsewhere in the project.

---

## What is in this repository

```
teleop/
├── config.py       # Rig configuration, validated at import
├── preflight.py    # Pre-session safety checklist
├── session.py      # Session state machine
└── test_teleop.py  # 23 checks

mcp_servers/teleop_server.py    # Six tools: observe, request, stop
integrations/teleop_bridge.py   # Task request brokering
```

### Configuration is validated, not trusted

`teleop/config.py` encodes the launch configuration used on the rig — a G1 in
its 29-degree-of-freedom form with Inspire hands, driven from a Quest headset in
bare-hand tracking mode. An arm or end-effector string `xr_teleoperate` would not
accept fails at import, rather than after the robot is standing and an operator
is already wearing a headset.

It produces the launch command for the operator to run at the rig:

```
python teleop_and_arm.py --xr-mode=hand --arm=G1_29 --ee=inspire1 --motion
```

Nothing in this package executes that command. Starting a session is a physical
act performed with the robot suspended and in sight.

**No credentials or robot addresses appear anywhere in the package.** The robot
host is read from `OPENLAB_G1_HOST` in the environment. A test scans every file
in `teleop/` for hard-coded addresses and credentials and fails the suite if one
appears.

### Preflight

Ten checks, nine of them blocking, taken from the operating procedure for the
rig. The first is the one that matters most:

> Is the G1 suspended in its support frame, so joints can move without the robot
> falling?

A humanoid that falls under power can injure whoever is nearest. The state
machine will not bring the robot to standing while that check is unconfirmed.

These are confirmations by a named person, not sensor readings. The software
cannot detect whether the robot is suspended; it refuses to proceed until
somebody states that it is, and records who stated it.

### Session state machine

```
OFF ──▶ ZERO_MOMENT ──▶ DAMPING ──▶ STANDING ──▶ READY ──▶ ACTIVE
                           ▲                                 │
                           └──────── soft stop ──────────────┘
```

Each transition carries a precondition and is refused when it does not hold:

- `stand()` is refused until preflight passes
- `begin_control()` is refused without a named operator and confirmed hand tracking
- `connect_services()` warns when measured latency exceeds the 100 ms budget,
  above which fine manipulation stops being practical
- `soft_stop()` returns the robot to damping from any state, and is never gated
- `end_session()` requires damping first, so a session cannot be closed while the
  robot is still under control

## Using it inside a workflow

`integrations/workcell.py` treats a teleoperated action as one step kind among
three — `device`, `manual`, `teleop`. A workflow that meets an unstructured step
does not end; it pauses, a person performs that step through the robot, and the
run continues.

```bash
python integrations/workcell.py --demo
```

The worked example is an NGS library prep across six instruments. Step 3 is the
reagent box, handed to teleoperation. Planning reports the whole run — including
which steps will stop for approval — before anything moves:

```
    10 steps · 6 need approval · 0 blocked
```

Run it again with `--approve` to execute through to the final read.

---

## Tests

```bash
python teleop/test_teleop.py             # 23 checks
python integrations/test_workcell.py     # 18 checks, includes the bridge
python integrations/teleop_bridge.py --demo
```

The teleoperation tests assert properties that are physical rather than
cosmetic: that the robot cannot stand before its support frame is confirmed,
that an explicitly failed suspension check cannot be overridden by confirming
everything else, that control cannot begin without a named operator and verified
tracking, that a stop always succeeds, and that no credential or robot address
has crept into the package.

The tests assert that the bridge refuses pipetting requests, refuses requests
with no reason, refuses unknown task types, will not complete a session that was
never accepted, and writes an audit record when a session closes.
