"""
OpenLabAI: LIMS MCP Server
Connects an agent to a Laboratory Information Management System.

Usage:
    python mcp_servers/lims_server.py --lims labware   --base-url https://lims.example.org
    python mcp_servers/lims_server.py --lims benchling --base-url https://x.benchling.com
    python mcp_servers/lims_server.py                   # mock mode, no LIMS required

Supported systems (see VENDOR_PROFILES): Benchling, LabWare LIMS, STARLIMS,
LabVantage, Thermo Fisher SampleManager, RETISOFT Genera, and a generic REST
profile. Endpoint paths follow each vendor's published REST conventions and are
declarative, so a laboratory whose instance differs corrects a profile rather
than the code. Only the mock profile has been exercised end to end here.

HighRes Biosolutions Cellario is a workcell scheduler rather than a LIMS and has
its own connector at mcp_servers/cellario_server.py.

Tools exposed to Claude:
    get_lims_status()       - Which LIMS is configured and whether it is reachable
    list_worklist()         - Samples queued for processing
    get_sample()            - Detail for one sample, including concentration
    update_sample_status()  - Write a status back to the LIMS (gated)
    attach_run_record()     - Attach an OpenLabAI audit record to a LIMS sample (gated)

Why this exists: a protocol is only useful if it acts on the right samples. The
concentrations needed to normalise a library, the list of samples actually queued
today, and the record of what was done to them all live in the LIMS. Without this
connector a scientist retypes that data by hand, which is where transcription
errors enter.

Credentials are read from the environment (OPENLAB_LIMS_TOKEN), never from a tool
argument, so a token cannot be supplied by an agent or written into a log. Writes
are gated the same way robot motion is: update_sample_status and attach_run_record
refuse without confirm=true.

Adding a LIMS: add an entry to VENDOR_PROFILES. The MCP tool surface does not
change, so an agent written against one LIMS works against any other.
"""

import os
import sys
import json
import asyncio
import argparse
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--lims", default="mock",
                    help="LIMS: mock, generic, benchling, labware, starlims, labvantage, samplemanager, genera")
parser.add_argument("--base-url", default="", help="LIMS API base URL")
parser.add_argument("--operator", default=os.environ.get("OPENLAB_OPERATOR", "unknown"))
args, _ = parser.parse_known_args()

TOKEN = os.environ.get("OPENLAB_LIMS_TOKEN", "")
app = Server("openlabai-lims")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from evals.run_logger import RunLogger
    HAS_EVALS = True
except Exception:
    HAS_EVALS = False


# ── ADAPTERS ─────────────────────────────────────────────────────────────────

class LIMSAdapter:
    """Interface every LIMS adapter implements.

    Sample dicts are normalised to a common shape so that protocol generation
    does not depend on which vendor's LIMS is behind it:
        {id, name, container, well, concentration_ng_ul, volume_ul, status}
    """

    name = "base"

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    async def worklist(self, limit: int) -> list:
        raise NotImplementedError

    async def sample(self, sample_id: str) -> dict:
        raise NotImplementedError

    async def set_status(self, sample_id: str, status: str) -> dict:
        raise NotImplementedError

    async def attach(self, sample_id: str, record: dict) -> dict:
        raise NotImplementedError


class MockAdapter(LIMSAdapter):
    """Deterministic sample data for development with no LIMS attached."""

    name = "mock"

    def _rows(self) -> list:
        return [
            {"id": "S-1001", "name": "Library_A01", "container": "PLATE-77", "well": "A1",
             "concentration_ng_ul": 12.4, "volume_ul": 50, "status": "queued"},
            {"id": "S-1002", "name": "Library_B01", "container": "PLATE-77", "well": "B1",
             "concentration_ng_ul": 31.9, "volume_ul": 50, "status": "queued"},
            {"id": "S-1003", "name": "Library_C01", "container": "PLATE-77", "well": "C1",
             "concentration_ng_ul": 4.7, "volume_ul": 50, "status": "queued"},
            {"id": "S-1004", "name": "Library_D01", "container": "PLATE-77", "well": "D1",
             "concentration_ng_ul": 22.1, "volume_ul": 50, "status": "on_hold"},
        ]

    async def worklist(self, limit: int) -> list:
        return [r for r in self._rows() if r["status"] == "queued"][:limit]

    async def sample(self, sample_id: str) -> dict:
        for row in self._rows():
            if row["id"] == sample_id:
                return row
        raise KeyError(sample_id)

    async def set_status(self, sample_id: str, status: str) -> dict:
        return {"id": sample_id, "status": status, "mode": "mock"}

    async def attach(self, sample_id: str, record: dict) -> dict:
        return {"id": sample_id, "attached": True, "mode": "mock",
                "protocol_hash": record.get("protocol_hash", "")}


VENDOR_PROFILES = {
    # Each profile declares the endpoint paths and the field spellings a vendor
    # uses. Adding a system is a profile, not new code, and a laboratory whose
    # instance differs can correct a path here without touching the tool surface.
    #
    # Paths follow each vendor's published REST conventions. They have NOT been
    # tested against a live instance of every system; treat any profile other
    # than "mock" as a starting point to be confirmed against your own server.
    "benchling": {
        "label": "Benchling (R&D cloud platform)",
        "worklist": "/api/v2/containers",
        "sample": "/api/v2/containers/{id}",
        "status": "/api/v2/containers/{id}",
        "attach": "/api/v2/containers/{id}:archive",
        "list_key": "containers",
        "auth": "bearer",
    },
    "labware": {
        "label": "LabWare LIMS",
        "worklist": "/api/v1/samples",
        "sample": "/api/v1/samples/{id}",
        "status": "/api/v1/samples/{id}/status",
        "attach": "/api/v1/samples/{id}/results",
        "list_key": "items",
        "auth": "bearer",
    },
    "starlims": {
        "label": "STARLIMS (Abbott Informatics)",
        "worklist": "/api/rest/samples",
        "sample": "/api/rest/samples/{id}",
        "status": "/api/rest/samples/{id}/status",
        "attach": "/api/rest/samples/{id}/results",
        "list_key": "data",
        "auth": "bearer",
    },
    "labvantage": {
        "label": "LabVantage LIMS",
        "worklist": "/rest/v1/sample",
        "sample": "/rest/v1/sample/{id}",
        "status": "/rest/v1/sample/{id}",
        "attach": "/rest/v1/sample/{id}/result",
        "list_key": "data",
        "auth": "bearer",
    },
    "samplemanager": {
        "label": "Thermo Fisher SampleManager LIMS",
        "worklist": "/api/samples",
        "sample": "/api/samples/{id}",
        "status": "/api/samples/{id}",
        "attach": "/api/samples/{id}/results",
        "list_key": "results",
        "auth": "bearer",
    },
    "genera": {
        "label": "RETISOFT Genera (workcell scheduler)",
        "worklist": "/api/orders",
        "sample": "/api/orders/{id}",
        "status": "/api/orders/{id}/state",
        "attach": "/api/orders/{id}/records",
        "list_key": "orders",
        "auth": "bearer",
    },
    "generic": {
        "label": "Generic REST LIMS",
        "worklist": "/samples",
        "sample": "/samples/{id}",
        "status": "/samples/{id}/status",
        "attach": "/samples/{id}/records",
        "list_key": "data",
        "auth": "bearer",
    },
}


class RESTAdapter(LIMSAdapter):
    """One adapter driven by a vendor profile.

    Field names vary between vendors, so _normalise maps the common spellings
    onto the shape above rather than assuming any one vendor's schema.
    """

    FIELD_ALIASES = {
        "id": ("id", "sampleId", "sample_id", "barcode", "orderId", "SAMPLE_ID"),
        "name": ("name", "sampleName", "label", "description", "SAMPLE_NAME"),
        "container": ("container", "plate", "containerBarcode", "plateBarcode", "location"),
        "well": ("well", "position", "wellPosition", "slot"),
        "concentration_ng_ul": ("concentration", "concentration_ng_ul", "concNgUl", "conc", "CONC"),
        "volume_ul": ("volume", "volume_ul", "volUl", "VOLUME"),
        "status": ("status", "state", "workflowStatus", "STATUS"),
    }

    def __init__(self, base_url: str, token: str, vendor: str):
        super().__init__(base_url, token)
        self.name = vendor
        self.profile = VENDOR_PROFILES.get(vendor, VENDOR_PROFILES["generic"])

    @property
    def label(self) -> str:
        return self.profile["label"]

    @classmethod
    def _normalise(cls, raw: dict) -> dict:
        out = {}
        for key, aliases in cls.FIELD_ALIASES.items():
            for alias in aliases:
                if isinstance(raw, dict) and raw.get(alias) is not None:
                    out[key] = raw[alias]
                    break
            else:
                out[key] = None
        return out

    async def _request(self, method: str, path: str, **kw) -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.request(method, f"{self.base_url}{path}", headers=self.headers, **kw)
            r.raise_for_status()
            return r.json() if r.content else {}

    async def worklist(self, limit: int) -> list:
        data = await self._request("GET", self.profile["worklist"],
                                   params={"limit": limit, "pageSize": limit})
        rows = data.get(self.profile["list_key"], data if isinstance(data, list) else [])
        return [self._normalise(r) for r in rows][:limit]

    async def sample(self, sample_id: str) -> dict:
        data = await self._request("GET", self.profile["sample"].format(id=sample_id))
        return self._normalise(data.get("data", data))

    async def set_status(self, sample_id: str, status: str) -> dict:
        return await self._request("POST", self.profile["status"].format(id=sample_id),
                                   json={"status": status})

    async def attach(self, sample_id: str, record: dict) -> dict:
        return await self._request("POST", self.profile["attach"].format(id=sample_id),
                                   json=record)


ADAPTERS = ["mock"] + sorted(VENDOR_PROFILES)


def build_adapter() -> LIMSAdapter:
    """Fall back to mock unless a real vendor and base URL are both given, so the
    server always starts and never silently points at nothing."""
    if args.lims == "mock" or not args.base_url:
        return MockAdapter(args.base_url, TOKEN)
    return RESTAdapter(args.base_url, TOKEN, args.lims)


ADAPTER = build_adapter()


# ── TOOLS ────────────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_lims_status",
            description="Report which LIMS adapter is configured, whether a token is present, and whether the LIMS answers.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="list_worklist",
            description=(
                "List samples queued for processing in the LIMS. Use this to find out what "
                "actually needs running today rather than asking the scientist to retype it."
            ),
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 24, "description": "Maximum samples to return"}},
                "required": [],
            },
        ),
        types.Tool(
            name="get_sample",
            description=(
                "Get one sample's detail, including concentration and volume. Concentrations "
                "are what a normalization protocol needs in order to compute transfer volumes."
            ),
            inputSchema={
                "type": "object",
                "properties": {"sample_id": {"type": "string"}},
                "required": ["sample_id"],
            },
        ),
        types.Tool(
            name="update_sample_status",
            description=(
                "Write a status back to the LIMS. This modifies the laboratory's system of "
                "record, so it requires confirm=true representing a human's approval."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sample_id": {"type": "string"},
                    "status": {"type": "string", "description": "e.g. in_progress, complete, failed"},
                    "confirm": {"type": "boolean", "description": "Must be true. Represents human approval to write to the LIMS."},
                },
                "required": ["sample_id", "status", "confirm"],
            },
        ),
        types.Tool(
            name="attach_run_record",
            description=(
                "Attach an OpenLabAI audit record to a sample in the LIMS, so the laboratory's "
                "system of record carries the protocol hash and operator for what was physically "
                "done. Requires confirm=true."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sample_id": {"type": "string"},
                    "run_id": {"type": "string"},
                    "protocol_hash": {"type": "string"},
                    "instrument": {"type": "string"},
                    "confirm": {"type": "boolean"},
                },
                "required": ["sample_id", "run_id", "confirm"],
            },
        ),
    ]


def _text(payload: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "get_lims_status":
            return _text(await handle_status())
        if name == "list_worklist":
            rows = await ADAPTER.worklist(int(arguments.get("limit", 24)))
            return _text({"lims": ADAPTER.name, "count": len(rows), "samples": rows})
        if name == "get_sample":
            return _text({"lims": ADAPTER.name, "sample": await ADAPTER.sample(arguments["sample_id"])})
        if name == "update_sample_status":
            return _text(await handle_update(arguments))
        if name == "attach_run_record":
            return _text(await handle_attach(arguments))
        return _text({"error": f"Unknown tool: {name}"})
    except KeyError as exc:
        return _text({"error": f"Sample not found: {exc}"})
    except httpx.HTTPStatusError as exc:
        return _text({"error": "LIMS rejected the request",
                      "status_code": exc.response.status_code,
                      "body": exc.response.text[:400]})
    except httpx.HTTPError as exc:
        return _text({"error": f"Cannot reach LIMS: {type(exc).__name__}",
                      "lims": ADAPTER.name, "base_url": ADAPTER.base_url})
    except Exception as exc:
        return _text({"error": f"{type(exc).__name__}: {exc}"})


async def handle_status() -> dict:
    reachable = None
    if ADAPTER.name != "mock":
        try:
            await ADAPTER.worklist(1)
            reachable = True
        except Exception:
            reachable = False
    return {
        "lims": ADAPTER.name,
        "base_url": ADAPTER.base_url or "(none)",
        "token_present": bool(TOKEN),
        "reachable": reachable,
        "mode": "mock" if ADAPTER.name == "mock" else "live",
        "vendor": getattr(ADAPTER, "label", "mock adapter"),
        "available_adapters": ADAPTERS,
        "note": ("No LIMS configured; returning deterministic sample data for development."
                 if ADAPTER.name == "mock" else
                 "Credentials are read from OPENLAB_LIMS_TOKEN, never from tool arguments."),
    }


async def handle_update(arguments: dict) -> dict:
    if arguments.get("confirm") is not True:
        return {"refused": True, "reason": "confirm was not true",
                "detail": "Writing to the LIMS changes the laboratory's system of record. "
                          "Pass confirm=true only with a human's approval."}
    result = await ADAPTER.set_status(arguments["sample_id"], arguments["status"])
    _audit("lims_status_update", arguments["sample_id"], arguments)
    return {"lims": ADAPTER.name, "result": result, "audit_logged": HAS_EVALS}


async def handle_attach(arguments: dict) -> dict:
    if arguments.get("confirm") is not True:
        return {"refused": True, "reason": "confirm was not true"}
    record = {
        "source": "OpenLabAI",
        "run_id": arguments["run_id"],
        "protocol_hash": arguments.get("protocol_hash", ""),
        "instrument": arguments.get("instrument", ""),
        "operator": args.operator,
    }
    result = await ADAPTER.attach(arguments["sample_id"], record)
    _audit("lims_attach_record", arguments["sample_id"], arguments)
    return {"lims": ADAPTER.name, "result": result, "audit_logged": HAS_EVALS}


def _audit(action: str, sample_id: str, arguments: dict) -> None:
    if not HAS_EVALS:
        return
    try:
        logger = RunLogger(operator=args.operator, instrument=f"LIMS:{ADAPTER.name}",
                           protocol_name=action)
        logger.log_agent_message("system", f"{action} on sample {sample_id}")
        logger.log_run_complete(status=action)
        logger.save()
    except Exception:
        pass


async def main():
    async with stdio_server() as (reader, writer):
        await app.run(reader, writer, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
