"""
Send a Lab Agent workflow to OpenLabAI over the Model Context Protocol, the way a voice front end would.

    python examples/lab_agent_to_openlabai.py

It starts mcp_servers/plr_server.py (simulation, no hardware), plans the 58 real ELISA transfers from
mcp_servers/fixtures/lab_agent/, prints what a voice layer would say, shows that the plan cannot run
without a person's approval, then approves it (as the person's "confirm" would) and runs it in simulation.
The whole workflow, with its incubations, is refused. Nothing physical happens.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "mcp_servers" / "fixtures" / "lab_agent" / "elisa_first_62_ops.json"


async def call(session, tool, **arguments):
    result = await session.call_tool(tool, arguments)
    return json.loads(result.content[0].text)


async def main():
    workflow = json.loads(FIXTURE.read_text())
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_servers" / "plr_server.py")])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            whole = await call(session, "plan_workflow", workflow=workflow)
            print("1. The whole workflow, with its incubation:")
            print("   SAYS:", whole["readback"], "\n")

            workflow["operations"] = workflow["operations"][:whole["transfers_before_first_unsupported"]]
            card = await call(session, "plan_workflow", workflow=workflow)
            print("2. Only the transfers before the incubation:")
            print("   SAYS:", card["readback"])
            print("   plan", card["plan_id"], "| approval token", card["approval_token"], "| tips used", card["summary"]["tips_used"], "\n")

            tried = await call(session, "execute_plan", plan_id=card["plan_id"], approval_token=card["approval_token"], confirm=False)
            print("3. Trying to run without a person's approval:", tried["reason"], "\n")

            ran = await call(session, "execute_plan", plan_id=card["plan_id"], approval_token=card["approval_token"], confirm=True)
            print("4. After the person says confirm: executed =", ran["executed"], "| hardware touched =", ran["hardware_touched"])


if __name__ == "__main__":
    asyncio.run(main())
