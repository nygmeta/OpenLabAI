# Model Hardware Standard: alignment, and what is not claimed

## The short version

Anthropic announced the **Model Hardware Standard (MHS)** on 27 August 2026, a
specification for AI agents to operate physical devices — described as doing for
hardware what the Model Context Protocol did for software.

**The MHS specification is not published.** Access is by application to a
research preview. No schema, SDK, or code example is publicly available.

So this repository does **not** implement MHS and does **not** claim conformance
with it. What it does is build its device layer on the concepts Anthropic
described publicly, and verify that those architectural properties hold.

Claiming conformance to a specification nobody can read would be unverifiable,
and would be wrong if the published schema differs.

## What was actually done

OpenLabAI's benchtop device layer (`mcp_servers/devices/`) is built around a
state-and-procedure model rather than bespoke per-device calls:

| MHS concept (public description) | OpenLabAI construct |
|---|---|
| state | `devices/core.py :: State` |
| procedure | `devices/core.py :: Procedure` |
| manifest | `Device.manifest()` |
| read primitive | `Device.read(state)` |
| write primitive | `Device.write(state, value)` |
| device-enforced safety limit | `SafetyLimit`, checked inside `Device.write` and `Device.run` before any action |
| discovery | `DeviceRegistry` |
| natural-language description tags | `State.description`, `Procedure.description` |

Nine simulated devices across eight classes are described this way: two
microplate readers, a heat sealer, a seal peeler, a plate centrifuge, a shaking
incubator, a thermal cycler, a barcode reader, and a plate hotel.

## Verifying it

```bash
python mcp_servers/mhs_bridge.py --verify
python mcp_servers/mhs_bridge.py --export bench_manifest.json
```

`--verify` checks the architectural invariants that a manifest-driven device
layer depends on:

- every device carries a manifest describing itself
- every state carries a natural-language description
- every **writable** state declares a safety limit
- every procedure that moves hardware **refuses to run without confirmation**

It found real gaps in the drivers when first run — four devices with an
undescribed `status` state and one writable state with no declared limit — which
were fixed. That is the point of having it.

`--export` emits the whole bench as one portable manifest document, which is the
artefact a mapping to the published schema would start from.

## The claim, stated narrowly

When the MHS schema is published, adopting it should be a **mapping exercise
against that export**, not a rewrite of the instrument layer, because the
underlying model is already state-and-procedure with device-enforced limits.

That is a checkable claim. "Supports MHS" would not be.

## If you are reading this after MHS is published

The mapping table above is the place to change. Compare the published schema
against `export_manifest()` output and adjust `Device.manifest()` to emit the
real field names. The tool surface — read, write, run, discover — should not need
to change, which is the property the alignment was for.

## Sources

- [Previewing the Model Hardware Standard — Anthropic](https://www.anthropic.com/news/model-hardware-standard-research-preview)
- [modelhardwarestandard.com](https://modelhardwarestandard.com) — research preview, access by application
