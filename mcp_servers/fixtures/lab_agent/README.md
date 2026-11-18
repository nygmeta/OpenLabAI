# Lab Agent workflow fixtures

Real Workflow JSON, compiled by Lab Agent (https://github.com/nygmeta/Claude_Life_sciences, MIT license,
"Lab Agent contributors") from its offline demo planner. They are used to test that `lab_agent_bridge.py` reads
Lab Agent's real format and refuses what the connector cannot do.

* `elisa_first_62_ops.json`: the first 62 operations of the ELISA workflow (58 transfers, then an incubation).
* `serial_dilution.json`: the nanolitre serial dilution (Lab Agent routes this one to an acoustic dispenser).

Regenerate with Lab Agent's `app.agent.planner` and `app.compiler.plan_to_ops.compile_plan`.
