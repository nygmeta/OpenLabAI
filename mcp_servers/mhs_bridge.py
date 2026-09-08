"""
OpenLabAI: Model Hardware Standard alignment layer

    python mcp_servers/mhs_bridge.py --export bench_manifest.json
    python mcp_servers/mhs_bridge.py --verify

WHAT THIS IS, PRECISELY

Anthropic announced the Model Hardware Standard (MHS) on 27 August 2026 as a
specification for AI agents to operate physical devices. As of this writing the
specification is NOT published: access is by application to a research preview,
and no schema, SDK, or code example is publicly available.

This module therefore does NOT implement MHS and does NOT claim conformance
with it. Claiming conformance to an unpublished specification would be
unverifiable, and would be wrong if the published schema differs.

What it does instead: OpenLabAI's device layer is built around the concepts
Anthropic described publicly — devices carrying a manifest of states and
procedures, reached through read and write primitives, with safety limits
enforced by the device. This module exports that layer as a single portable
manifest document and verifies the architectural properties hold.

The practical claim is narrow and checkable: when the MHS schema is published,
adopting it should be a mapping exercise against the export below rather than a
rewrite of the instrument layer, because the underlying model is already
state-and-procedure rather than bespoke per-device calls.

CONCEPT MAPPING (from Anthropic's public description of MHS)

    MHS concept        OpenLabAI construct
    ---------------    -------------------------------------------------
    state              devices/core.py :: State
    procedure          devices/core.py :: Procedure
    manifest           Device.manifest()
    read primitive     Device.read(state)
    write primitive    Device.write(state, value)
    safety limit       devices/core.py :: SafetyLimit, enforced in
                       Device.write and Device.run before any action
    discovery          DeviceRegistry
    natural-language
    description tags   State.description / Procedure.description

Where the published specification later differs, the mapping table above is the
place to change, and docs/MHS_INTEROP.md records what was assumed and why.
"""

from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_servers.devices import default_registry

ALIGNMENT_NOTE = (
    "Architecture aligned with the publicly described MHS model. NOT a conformant "
    "MHS implementation: the MHS specification was unpublished when this was written "
    "(research preview, access by application). No conformance is claimed."
)


def export_manifest(registry=None) -> dict:
    """Emit every device on the bench as one portable manifest document."""
    registry = registry or default_registry()
    return {
        "producer": "OpenLabAI",
        "producer_url": "https://github.com/nygmeta/OpenLabAI",
        "model": "state-and-procedure device manifest",
        "alignment": ALIGNMENT_NOTE,
        "primitives": ["read", "write", "run"],
        "device_classes": registry.classes(),
        "devices": registry.manifests(),
    }


def verify_alignment(registry=None) -> dict:
    """Check the architectural properties the MHS model depends on.

    These are the invariants that make a manifest-driven device layer usable by
    an agent that has never seen the device: every device describes itself,
    every adjustable value declares its bound, and nothing that moves is
    reachable without a human.
    """
    registry = registry or default_registry()
    findings, checks = [], 0

    for device in registry.all():
        manifest = device.manifest()
        checks += 1
        if not manifest.get("states") and not manifest.get("procedures"):
            findings.append(f"{device.device_id}: manifest declares neither states nor procedures")

        for state in device.states.values():
            checks += 1
            if not state.description:
                findings.append(f"{device.device_id}.{state.name}: no natural-language description")
            if state.writable and state.limit is None:
                checks += 1
                findings.append(
                    f"{device.device_id}.{state.name}: writable with no declared safety limit")

        for proc in device.procedures.values():
            checks += 1
            if not proc.description:
                findings.append(f"{device.device_id}.{proc.name}: no natural-language description")
            if proc.physical:
                checks += 1
                # A procedure that moves hardware must refuse without confirmation.
                outcome = device.run(proc.name, _safe_args(proc), confirm=False)
                if not outcome.get("refused"):
                    findings.append(
                        f"{device.device_id}.{proc.name}: moves hardware but ran without confirm")

    return {
        "checks": checks,
        "devices": len(registry.all()),
        "findings": findings,
        "aligned": not findings,
        "note": ALIGNMENT_NOTE,
    }


def _safe_args(proc) -> dict:
    """Arguments inside every declared limit, so the verifier tests the gate
    rather than tripping a limit first."""
    args = {}
    for key, spec in proc.parameters.items():
        limit = spec.get("limit")
        if limit is None:
            continue
        if limit.allowed:
            args[key] = limit.allowed[0]
        elif limit.minimum is not None:
            args[key] = limit.minimum
        elif limit.maximum is not None:
            args[key] = limit.maximum
    return args


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", metavar="PATH", help="Write the bench manifest to a JSON file")
    ap.add_argument("--verify", action="store_true", help="Check the architectural properties")
    opts = ap.parse_args()

    if opts.export:
        document = export_manifest()
        Path(opts.export).write_text(json.dumps(document, indent=2, default=str))
        print(f"Wrote {opts.export}: {len(document['devices'])} devices, "
              f"{len(document['device_classes'])} classes")
        return 0

    if opts.verify:
        report = verify_alignment()
        print(f"Devices:  {report['devices']}")
        print(f"Checks:   {report['checks']}")
        if report["aligned"]:
            print("Result:   aligned — every device self-describes, every writable state "
                  "declares a limit,\n          and every hardware-moving procedure refuses "
                  "without confirmation.")
        else:
            print(f"Result:   {len(report['findings'])} finding(s)")
            for finding in report["findings"]:
                print("   -", finding)
        print(f"\n{report['note']}")
        return 0 if report["aligned"] else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
