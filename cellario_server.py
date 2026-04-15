"""
Lab-Assistant: Cellario Workcell MCP Server
Provides orchestration of integrated robotic workcells via Cellario COM automation.

Requirements:
    - Windows PC running Cellario software (version 6.x+)
    - pip install mcp pywin32
    - Cellario must be open before starting this server

Usage:
    python cellario_server.py

Tools exposed to Claude:
    schedule_run()      - Start a workcell batch from a batch definition
    get_device_status() - Check status of any device (liquid handler, centrifuge, plate reader, hotel)
    query_queue()       - See what's in the automation queue
    get_batch_list()    - List available batch definitions
"""

import json
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

try:
    import win32com.client
    import pythoncom
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

app = Server("lab-assistant-cellario")


def get_cellario():
    if not HAS_WIN32:
        return None
    pythoncom.CoInitialize()
    try:
        return win32com.client.Dispatch("CellarioAutomation.Application")
    except Exception as e:
        raise RuntimeError(
            f"Could not connect to Cellario: {e}\n"
            "Make sure Cellario software is open and in idle state."
        )


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="schedule_run",
            description=(
                "Schedule a workcell batch run in Cellario. "
                "Provide the batch definition name and any required parameters. "
                "The run will be queued and executed by the workcell scheduler."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "batch_name": {"type": "string", "description": "Name of the Cellario batch definition"},
                    "plate_count": {"type": "integer", "description": "Number of plates to process", "default": 1},
                    "priority": {"type": "string", "enum": ["normal", "high", "urgent"], "default": "normal"},
                    "notes": {"type": "string", "description": "Optional run notes"}
                },
                "required": ["batch_name"]
            },
        ),
        types.Tool(
            name="get_device_status",
            description=(
                "Get the current status of a device in the workcell. "
                "Device types include: liquid_handler, centrifuge, plate_reader, "
                "plate_hotel, incubator, sealer, peeler."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "device_name": {"type": "string", "description": "Name of the device to query (e.g. 'Hamilton_1', 'Centrifuge_1')"},
                },
                "required": ["device_name"]
            },
        ),
        types.Tool(
            name="query_queue",
            description="Get the current automation queue — all scheduled, running, and recently completed runs.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_batch_list",
            description="List all available batch definitions in Cellario that can be scheduled.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "schedule_run":
            return await handle_schedule_run(arguments)
        elif name == "get_device_status":
            return await handle_device_status(arguments)
        elif name == "query_queue":
            return await handle_query_queue()
        elif name == "get_batch_list":
            return await handle_batch_list()
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        if not HAS_WIN32:
            return [types.TextContent(type="text", text=json.dumps(mock_response(name, arguments), indent=2))]
        return [types.TextContent(type="text", text=f"Error: {e}")]


async def handle_schedule_run(args: dict) -> list[types.TextContent]:
    if not HAS_WIN32:
        return [types.TextContent(type="text", text=json.dumps(mock_response("schedule_run", args), indent=2))]

    cellario = get_cellario()
    batch_name = args["batch_name"]
    plate_count = args.get("plate_count", 1)
    priority = args.get("priority", "normal")
    notes = args.get("notes", "")

    try:
        scheduler = cellario.Scheduler
        run_id = scheduler.ScheduleRun(batch_name, plate_count, priority, notes)
        result = {
            "scheduled": True,
            "run_id": str(run_id),
            "batch": batch_name,
            "plates": plate_count,
            "priority": priority,
            "message": f"Run {run_id} queued successfully. Monitor with query_queue()."
        }
    except Exception as e:
        result = {"scheduled": False, "error": str(e)}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_device_status(args: dict) -> list[types.TextContent]:
    if not HAS_WIN32:
        return [types.TextContent(type="text", text=json.dumps(mock_response("get_device_status", args), indent=2))]

    cellario = get_cellario()
    device_name = args["device_name"]

    try:
        devices = cellario.Devices
        device = devices.Item(device_name)
        result = {
            "device": device_name,
            "status": device.Status,
            "is_busy": device.IsBusy,
            "current_task": device.CurrentTask if hasattr(device, "CurrentTask") else None,
            "error": device.LastError if hasattr(device, "LastError") else None,
        }
    except Exception as e:
        result = {"device": device_name, "error": str(e)}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_query_queue() -> list[types.TextContent]:
    if not HAS_WIN32:
        return [types.TextContent(type="text", text=json.dumps(mock_response("query_queue", {}), indent=2))]

    cellario = get_cellario()

    try:
        queue = cellario.Queue
        runs = []
        for i in range(queue.Count):
            run = queue.Item(i + 1)
            runs.append({
                "run_id": run.ID,
                "batch": run.BatchName,
                "status": run.Status,
                "priority": run.Priority,
                "queued_at": str(run.QueuedAt),
                "started_at": str(run.StartedAt) if run.StartedAt else None,
            })
        result = {"queue_length": len(runs), "runs": runs}
    except Exception as e:
        result = {"error": str(e)}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_batch_list() -> list[types.TextContent]:
    if not HAS_WIN32:
        return [types.TextContent(type="text", text=json.dumps(mock_response("get_batch_list", {}), indent=2))]

    cellario = get_cellario()

    try:
        batches = cellario.BatchDefinitions
        batch_list = []
        for i in range(batches.Count):
            b = batches.Item(i + 1)
            batch_list.append({
                "name": b.Name,
                "description": b.Description if hasattr(b, "Description") else "",
                "device_requirements": b.Devices if hasattr(b, "Devices") else [],
            })
        result = {"batch_count": len(batch_list), "batches": batch_list}
    except Exception as e:
        result = {"error": str(e)}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


def mock_response(tool: str, args: dict) -> dict:
    if tool == "schedule_run":
        return {
            "mode": "mock",
            "scheduled": True,
            "run_id": "MOCK-RUN-001",
            "batch": args.get("batch_name", "demo_batch"),
            "plates": args.get("plate_count", 1),
            "message": "Mock mode — Cellario not connected. Run would be queued in production."
        }
    elif tool == "get_device_status":
        return {
            "mode": "mock",
            "device": args.get("device_name", "unknown"),
            "status": "idle",
            "is_busy": False,
            "current_task": None,
        }
    elif tool == "query_queue":
        return {
            "mode": "mock",
            "queue_length": 2,
            "runs": [
                {"run_id": "MOCK-001", "batch": "NGS_Cleanup", "status": "running", "priority": "normal"},
                {"run_id": "MOCK-002", "batch": "Normalization", "status": "queued", "priority": "normal"},
            ]
        }
    elif tool == "get_batch_list":
        return {
            "mode": "mock",
            "batch_count": 4,
            "batches": [
                {"name": "NGS_Cleanup", "description": "AMPure bead cleanup for NGS libraries"},
                {"name": "Normalization", "description": "DNA/library normalization to target concentration"},
                {"name": "HitPicking", "description": "Colony or compound hitpicking from source plates"},
                {"name": "Serial_Dilution", "description": "Serial dilution across compound plates"},
            ]
        }
    return {"mode": "mock", "tool": tool}


async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
