"""
Tests for the device abstraction and its safety model.

Run:  python mcp_servers/devices/test_devices.py
Exits non-zero on failure.

The properties these protect are the ones an agent relies on when it operates a
device it has never seen: the manifest describes the device honestly, a value
outside a limit is refused by the device rather than by the caller, and nothing
that moves runs without a human.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp_servers.devices import default_registry
from mcp_servers.devices.core import SafetyViolation
from mcp_servers.mhs_bridge import export_manifest, verify_alignment


def main() -> int:
    failures, checks = [], 0
    reg = default_registry()

    # Discovery
    checks += 1
    if len(reg.all()) < 9:
        failures.append(f"expected at least 9 devices, found {len(reg.all())}")
    checks += 1
    if "plate_reader" not in reg.classes() or "sealer" not in reg.classes():
        failures.append(f"expected plate_reader and sealer classes, got {reg.classes()}")

    # Every manifest is self-describing
    for device in reg.all():
        checks += 1
        m = device.manifest()
        if not m["description"] or "device_id" not in m:
            failures.append(f"{device.device_id}: manifest incomplete")

    # A limit is refused by the device
    sealer = reg.get("sealer1")
    checks += 1
    try:
        sealer.write("set_temperature", 260)
        failures.append("sealer accepted 260 C, above its 200 C maximum")
    except SafetyViolation:
        pass
    checks += 1
    try:
        sealer.write("set_temperature", 165)
    except SafetyViolation:
        failures.append("sealer refused 165 C, which is inside its range")

    # Read-only states cannot be written
    checks += 1
    try:
        sealer.write("temperature", 999)
        failures.append("a read-only state was writable")
    except SafetyViolation:
        pass

    # Hardware-moving procedures refuse without confirmation
    checks += 1
    if not sealer.run("seal_plate", {}, confirm=False).get("refused"):
        failures.append("seal_plate ran without confirmation")

    # Limits are checked BEFORE the confirmation gate: an unsafe argument is
    # refused as unsafe even when the caller confirms.
    spinner = reg.get("spinner1")
    checks += 1
    try:
        spinner.run("spin", {"speed_rpm": 9000}, confirm=True)
        failures.append("centrifuge accepted 9000 rpm above its 3000 rpm maximum")
    except SafetyViolation:
        pass

    # Interlocks hold even when confirmed
    checks += 1
    spinner.states["buckets_loaded"].value = 1
    out = spinner.run("spin", {"speed_rpm": 1000, "duration_s": 30}, confirm=True)
    if not out.get("error"):
        failures.append("centrifuge spun with an unbalanced single plate")
    checks += 1
    spinner.states["buckets_loaded"].value = 2
    out = spinner.run("spin", {"speed_rpm": 1000, "duration_s": 30}, confirm=True)
    if not out.get("spun"):
        failures.append(f"balanced rotor refused to spin: {out}")

    # Measurement is ungated, but needs a plate
    reader = reg.get("reader1")
    checks += 1
    if not reader.run("quantify_dna", {}, confirm=False).get("error"):
        failures.append("reader quantified with no plate present")
    reader.states["plate_present"].value = True
    checks += 1
    out = reader.run("quantify_dna", {"wells": "A1:B3"}, confirm=False)
    if out.get("well_count") != 6:
        failures.append(f"expected 6 wells for A1:B3, got {out.get('well_count')}")
    checks += 1
    if "simulated" not in json_dump(out).lower():
        failures.append("simulated reader output is not labelled as simulated")

    # Thermocycler interlock
    cycler = reg.get("cycler1")
    cycler.states["plate_present"].value = True
    checks += 1
    if not cycler.run("run_program", {"cycles": 30}, confirm=True).get("error"):
        failures.append("thermocycler ran with the lid open")
    checks += 1
    try:
        cycler.run("run_program", {"denature_c": 120}, confirm=True)
        failures.append("thermocycler accepted 120 C denaturation")
    except SafetyViolation:
        pass

    # Architectural alignment
    checks += 1
    report = verify_alignment(default_registry())
    if not report["aligned"]:
        failures.append(f"alignment findings: {report['findings']}")

    checks += 1
    doc = export_manifest(default_registry())
    if not doc["devices"] or "NOT a conformant" not in doc["alignment"]:
        failures.append("exported manifest missing devices or the non-conformance note")

    if failures:
        print(f"FAILED ({len(failures)} of {checks} checks)")
        for f in failures:
            print("  -", f)
        return 1
    print(f"OK — {checks} checks passed")
    return 0


def json_dump(obj) -> str:
    import json
    return json.dumps(obj, default=str)


if __name__ == "__main__":
    raise SystemExit(main())
