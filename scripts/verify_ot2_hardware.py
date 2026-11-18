#!/usr/bin/env python3
"""
Run OpenLabAI's own OT-2 MCP server against a physical Opentrons OT-2 and write
down exactly what happened.

    python scripts/verify_ot2_hardware.py --host 169.254.x.x --operator "Your Name"

The script starts mcp_servers/ot2_server.py as a subprocess and calls its tools
over the Model Context Protocol, the same path an agent uses. Nothing here talks
to the robot in place of the server except two things it records separately: the
robot's own /health identity and its OpenAPI path list, so the log shows which
robot answered and which endpoints that robot actually has.

Tiers (each is off unless you ask for it):

    default            read_deck, get_run_status, create_protocol. No writes to the
                       robot and no motion.
    --allow-upload     adds upload_protocol with a home-only protocol, checks the
                       robot's own analysis, then deletes the idle run and protocol.
                       Nothing moves.
    --allow-motion     adds home_robot. The robot homes all axes. You must type HOME
                       to confirm that the deck and the robot's path are clear.

NOT exercised by this script, whatever flags you pass: start_run, pause_run,
resume_run, stop_run. Their hardware status stays "not yet run" until someone runs
them under a separate, supervised procedure.

Honesty rules the script enforces:
  * If no robot answers, it writes no log at all.
  * read_deck passes only if the server reports connected=true. The server falls
    back to a mock deck when it cannot read the robot, and that is a failure here.
  * A test double (mcp_servers/fake_ot2.py) is detected from /health, and its runs
    are never written into hardware_logs/.
  * Logs are written once and not edited. Failed runs are logged too.

Exit codes: 0 every requested step passed, 1 a step failed, 2 no robot answered,
3 refused to file a simulated run as a hardware log.
"""

import argparse
import ast
import asyncio
import datetime as dt
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "mcp_servers" / "ot2_server.py"
HOME_ONLY = REPO / "scripts" / "verification_protocols" / "home_only.py"
DEFAULT_LOG_DIR = REPO / "hardware_logs"
HEADERS = {"opentrons-version": "3"}
SCRIPT_VERSION = 1

ALL_TOOLS = {"read_deck", "get_run_status", "home_robot", "create_protocol", "upload_protocol",
             "start_run", "pause_run", "resume_run", "stop_run"}
NEVER_EXERCISED = ["start_run", "pause_run", "resume_run", "stop_run"]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_state() -> dict:
    def git(*a):
        try:
            return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    return {"commit": git("rev-parse", "HEAD") or None,
            "uncommitted_changes": bool(git("status", "--porcelain", "--", ".", ":(exclude)hardware_logs"))}


def probe_robot(base: str) -> tuple[dict, bool]:
    """Ask the robot who it is. Raises if nothing answers."""
    r = httpx.get(f"{base}/health", headers=HEADERS, timeout=5)
    r.raise_for_status()
    h = r.json()
    serial = str(h.get("robot_serial") or "")
    identity = {
        "name": h.get("name"), "robot_model": h.get("robot_model"),
        "api_version": h.get("api_version"), "fw_version": h.get("fw_version"),
        "system_version": h.get("system_version"),
        # The serial number itself is not published; a hash lets a log be tied to one robot.
        "robot_serial_sha256_12": hashlib.sha256(serial.encode()).hexdigest()[:12] if serial else None,
    }
    simulated = ("test double" in str(h.get("name", "")).lower()
                 or "simulated" in str(h.get("robot_model", "")).lower())
    return identity, simulated


def read_openapi(base: str) -> dict:
    """Which endpoints does this robot say it has? Evidence, not a pass/fail."""
    try:
        r = httpx.get(f"{base}/openapi.json", headers=HEADERS, timeout=10)
        r.raise_for_status()
        paths = list(r.json().get("paths", {}))
        return {"available": True, "path_count": len(paths),
                "has_labware_path": "/labware" in paths, "has_runs_path": "/runs" in paths,
                "has_protocols_path": "/protocols" in paths, "has_robot_home_path": "/robot/home" in paths}
    except Exception as exc:
        return {"available": False, "error": str(exc)[:200]}


class Steps:
    def __init__(self):
        self.items = []

    def add(self, step: str, status: str, detail: str = "", **extra):
        self.items.append({"step": step, "status": status, "detail": detail, **extra})
        print(f"  [{status.upper():8}] {step}: {detail}")

    def passed(self, step: str) -> bool:
        return any(s["step"] == step and s["status"] == "pass" for s in self.items)


async def call_tool(session: ClientSession, tool: str, arguments: dict):
    result = await session.call_tool(tool, arguments)
    text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    try:
        return text, json.loads(text)
    except ValueError:
        return text, None


async def run_steps(args, base: str, openapi: dict, steps: Steps, scratch: Path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER), "--host", args.host, "--port", str(args.port), "--operator", args.operator],
        cwd=str(scratch))                       # protocols/ and run_logs/ land in scratch
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            listed = {t.name for t in (await session.list_tools()).tools}
            if ALL_TOOLS <= listed:
                steps.add("list_tools", "pass", f"{len(listed)} tools published")
            else:
                steps.add("list_tools", "fail", f"missing: {sorted(ALL_TOOLS - listed)}")

            text, deck = await call_tool(session, "read_deck", {})
            if isinstance(deck, dict) and deck.get("connected") is True:
                steps.add("read_deck", "pass", f"{len(deck.get('labware', []))} labware item(s) reported",
                          labware_count=len(deck.get("labware", [])))
            else:
                why = "the server returned its mock deck, so nothing was read from the robot"
                if openapi.get("available") and openapi.get("has_labware_path") is False:
                    why += "; this robot's own API lists no /labware path, so read_deck needs a different endpoint"
                steps.add("read_deck", "fail", why, response_excerpt=text[:300])

            text, status = await call_tool(session, "get_run_status", {})
            if isinstance(status, dict) and ("status" in status):
                steps.add("get_run_status", "pass", f"status: {status.get('status')}")
            else:
                steps.add("get_run_status", "fail", f"unexpected response: {text[:200]}")

            text, made = await call_tool(session, "create_protocol", {
                "name": "verification", "description": "OpenLabAI hardware verification, never uploaded",
                "steps": [{"type": "pick_up_tips"}, {"type": "drop_tips"}]})
            try:
                source = Path(scratch / made["saved_to"]).read_text()
                ast.parse(source)
                steps.add("create_protocol", "pass", "wrote a protocol file that parses",
                          protocol_sha256=hashlib.sha256(source.encode()).hexdigest())
            except Exception as exc:
                steps.add("create_protocol", "fail", f"{type(exc).__name__}: {exc}"[:200])

            if args.allow_upload:
                text, up = await call_tool(session, "upload_protocol", {"path": str(HOME_ONLY)})
                good = (isinstance(up, dict) and up.get("run_id") and up.get("protocol_id")
                        and up.get("analysis_status") not in (None, "timeout")
                        and not up.get("analysis_errors") and up.get("can_start") is True)
                if good:
                    steps.add("upload_protocol", "pass",
                              f"robot analysed the protocol ({up.get('analysis_status')}); run left idle",
                              analysis_status=up.get("analysis_status"), commands_analysed=up.get("commands_analysed"))
                else:
                    steps.add("upload_protocol", "fail", f"unexpected response: {text[:300]}")
                if isinstance(up, dict) and up.get("run_id"):
                    problems = []
                    for kind, ident in (("runs", up.get("run_id")), ("protocols", up.get("protocol_id"))):
                        if not ident:
                            continue
                        try:
                            r = httpx.delete(f"{base}/{kind}/{ident}", headers=HEADERS, timeout=15)
                            if r.status_code >= 300:
                                problems.append(f"{kind}/{ident}: HTTP {r.status_code}")
                        except Exception as exc:
                            problems.append(f"{kind}/{ident}: {exc}")
                    if problems:
                        steps.add("cleanup", "fail", "delete these from the robot by hand: " + "; ".join(problems))
                    else:
                        steps.add("cleanup", "pass", "deleted the idle run and the uploaded protocol")
            else:
                steps.add("upload_protocol", "skipped", "not requested (--allow-upload)")

            if args.allow_motion:
                print("\n  The robot is about to HOME ALL AXES. Clear the deck and keep hands out of the robot.")
                answer = input("  Type HOME to continue, anything else to skip: ").strip()
                if answer == "HOME":
                    text, homed = await call_tool(session, "home_robot", {})
                    if isinstance(homed, dict) and homed.get("status") == "homed":
                        steps.add("home_robot", "pass", "the server reported the robot homed")
                    else:
                        steps.add("home_robot", "fail", f"unexpected response: {text[:200]}")
                else:
                    steps.add("home_robot", "declined", "operator did not confirm; the robot was not moved")
            else:
                steps.add("home_robot", "skipped", "not requested (--allow-motion)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", required=True, help="OT-2 IP address")
    ap.add_argument("--port", type=int, default=31950)
    ap.add_argument("--operator", required=True, help="Your name, recorded in the log")
    ap.add_argument("--allow-upload", action="store_true", help="also test upload_protocol (nothing moves)")
    ap.add_argument("--allow-motion", action="store_true", help="also test home_robot (the robot homes)")
    ap.add_argument("--notes", default="", help="what you saw: lights, sounds, motion, anything unexpected")
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    args = ap.parse_args()

    base = f"http://{args.host}:{args.port}"
    started = utc_now()
    print(f"Checking for a robot at {base} ...")
    try:
        identity, simulated = probe_robot(base)
    except Exception as exc:
        print(f"\nNo robot answered at {base} ({type(exc).__name__}). Nothing was run and no log was written.")
        return 2

    log_dir = Path(args.log_dir).resolve()
    if simulated and log_dir == DEFAULT_LOG_DIR.resolve():
        print(f"\n'{identity['name']}' is a test double, not a robot. Refusing to write it into "
              f"hardware_logs/. Pass --log-dir somewhere else if you want the record.")
        return 3

    print(f"Robot: {identity['name']} | {identity['robot_model']} | API {identity['api_version']} | firmware {identity['fw_version']}")
    openapi = read_openapi(base)
    steps = Steps()
    with tempfile.TemporaryDirectory(prefix="openlabai_verify_") as scratch:
        asyncio.run(run_steps(args, base, openapi, steps, Path(scratch)))

    failed = [s for s in steps.items if s["status"] == "fail"]
    verdict = "failed" if failed else ("simulated: a test double, not hardware evidence" if simulated else "verified")
    tools_passed = [t for t in ALL_TOOLS if steps.passed(t)]
    log = {
        "schema": 1, "script": "scripts/verify_ot2_hardware.py", "script_version": SCRIPT_VERSION,
        "started_at": started, "finished_at": utc_now(), "operator": args.operator, "notes": args.notes,
        "host": args.host, "port": args.port, "simulated": simulated, "verdict": verdict,
        "robot": identity, "robot_openapi": openapi,
        "openlabai": {**git_state(), "mcp_version": importlib.metadata.version("mcp"),
                      "python": platform.python_version(), "platform": platform.platform()},
        "tiers_requested": ["read_only"] + (["upload"] if args.allow_upload else []) + (["motion"] if args.allow_motion else []),
        "steps": steps.items,
        "tools_passed": sorted(tools_passed),
        "tools_not_exercised": sorted(set(ALL_TOOLS) - set(tools_passed)),
        "never_exercised_by_this_script": NEVER_EXERCISED,
        "statement": ("Tools were called through OpenLabAI's own MCP server (mcp_servers/ot2_server.py) over stdio against "
                      "the robot identified above. This log records the tool responses. It does not show what the robot "
                      "physically did; the operator's observations are in 'notes'."),
    }
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"ot2_verification_{started.replace(':', '').replace('-', '')}{'_SIMULATED' if simulated else ''}.json"
    path.write_text(json.dumps(log, indent=2) + "\n")

    print(f"\nVerdict: {verdict}")
    print(f"Tools that passed: {', '.join(sorted(tools_passed)) or 'none'}")
    print(f"Not exercised: {', '.join(sorted(set(ALL_TOOLS) - set(tools_passed)))}")
    print(f"Log written: {path}")
    if not simulated:
        print("Commit the log exactly as written. Do not edit it, and do not change the README status "
              "to claim more than the tools that passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
