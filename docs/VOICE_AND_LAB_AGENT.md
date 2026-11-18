# Voice control: OpenLabAI and Lab Agent

[Lab Agent](https://github.com/nygmeta/Claude_Life_sciences) is a separate, MIT-licensed project that
turns a spoken request into a validated, robot-agnostic workflow. OpenLabAI is the layer that checks a
plan against the instrument and binds a person's approval to the exact commands. This page explains how
they connect. The two stay separate repositories.

## Who wrote what

Authorship follows the git history of the Lab Agent repository. The voice pipeline (speech to text,
text to speech, the browser widget, the confirmation gate on the audio path) was written mainly by
**Junchen Lu**, who has 50 of that repository's 54 commits. The planner, the validation backend, the web
console and the Opentrons client were committed by **Ainur Nygmet**. Lab Agent is released under the MIT
license as "Lab Agent contributors"; nothing from it is copied into this repository except two small test
fixtures (`mcp_servers/fixtures/lab_agent/`), which keep that credit.

## How they connect

```
spoken request
   -> Lab Agent: planner, compiler, safety rules      (the Workflow: labware and operations)
   -> OpenLabAI plan_workflow                          (this repository)
        refuses anything that is not a plain transfer, with reasons
        simulates the rest under PyLabRobot's tip and volume tracking
        returns a plan card: warnings, final volumes, approval token, and a sentence to speak
   -> the person hears the readback and says "confirm"
   -> OpenLabAI execute_plan                           (confirm=true plus the token, as always)
```

`plan_workflow` is the fifth tool of `mcp_servers/plr_server.py`. It takes Lab Agent's `Workflow` as
plain JSON, so it needs no Lab Agent code. `examples/lab_agent_to_openlabai.py` runs the whole sequence
over the Model Context Protocol with no hardware and no voice.

## What it accepts and what it refuses

Only plate-to-plate transfers in the p300 range (1 to 300 uL) on labware the connector knows. Anything
else refuses the **whole** workflow, by name, so nothing can run partly: incubations, waits, washes,
labware moves, manual steps, mixing, volumes outside the range, unknown labware. A refusal also says how
many transfers come before the first unsupported step.

Real Lab Agent output shows both kinds. Its nanolitre serial dilution is refused because its volumes
are below a p300 pipette's range. Its ELISA is refused because of its incubations, with 58 transfers before
the first one. Those 58 transfers alone make a valid plan.

A Lab Agent workflow does not say how much liquid is in each well, so the bridge assumes each source holds
what the plan takes plus 5 uL, states that, and asks for confirmation. Pass `loaded_volumes_ul` to give
real numbers; a plan that takes more than a well holds is refused.

## Limits

* **Simulation only, and not tested with the voice pipeline.** The tests use Lab Agent's workflow JSON, not
  its speech stack. No robot is involved.
* **Lab Agent's own statements about running on a real Opentrons OT-2 are its own.** This page makes none.
* **Transfers only.** A realistic assay such as an ELISA needs incubations and washes, so it is refused
  whole today. Running the transfer segments between those steps is possible later; it is not built.
* **The Lab Agent side is not wired yet.** Nothing in Lab Agent calls `plan_workflow` yet; the example
  shows the call. An adapter in Lab Agent's `app/adapters/` would be the place.
