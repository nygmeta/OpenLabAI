# 🔬 Scientist Guide
### Using Lab-Assistant with zero coding experience

This guide is for wet lab scientists who want to use Lab-Assistant to plan and run protocols — no programming knowledge required.

---

## What You Need

- A computer (Windows, Mac, or Linux)
- A Claude API key (get one free at [console.anthropic.com](https://console.anthropic.com))
- Your liquid handling robot (or use demo mode without a robot)

---

## The Easiest Way: Just the GUI

**Step 1:** Download `BiomekAgent.html` from the [gui folder](../gui/)

**Step 2:** Open it in Chrome or Edge (double-click the file)

**Step 3:** Paste your API key into the `API KEY` field in the top bar

**Step 4:** Start talking to your robot!

That's it. No installation, no terminal, no Python.

---

## What Can You Ask the Agent?

Just describe your experiment the way you would describe it to a colleague:

### Reading your deck
```
Read my deck and tell me what's loaded at each position
```
```
What labware do I have available?
```

### Simple transfers
```
Transfer 50 µL from column 1 of P1 to column 1 of P4, new tips each row
```
```
Move 20 µL from every well in P1 to the corresponding well in P4
```

### NGS workflows
```
AMPure bead cleanup: 1.8x beads, incubate 5 min on magnet,
2x 80% ethanol wash, air dry 2 min, elute in 20 µL EB buffer
```
```
Normalize my NGS library to 4 nM. Source plate is P1, diluent is in TR1.
```

### Cell culture
```
Dispense 200 µL of media from the trough into all 96 wells of P4
```
```
Serial dilution: 1:2 across columns 1-11 of P1, starting with 200 µL
```

### Checking parameters
```
What are the current aspirate and dispense heights?
What mix speed is set for the transfer steps?
```

---

## Understanding the Interface

### The Deck Map (right panel, top)
Shows your robot's deck positions color-coded by labware type:
- 🟢 **Green** = plates (P1, P4, P11, P12...)
- 🔵 **Blue** = tip boxes (P7, P8...)
- 🟡 **Amber** = troughs and reagent reservoirs (TL1, TR1)
- ⬜ **Empty** = available positions

Click any position to ask the agent about it.

### The Timeline (right panel, bottom)
When the agent creates a protocol, the Gantt chart shows:
- Each step as a colored bar
- How long each step takes
- The total estimated run time

Color key:
- 🟢 Aspirate steps
- 🔵 Dispense steps
- 🟡 Mix steps
- 🟣 Wash/tip change steps
- 🟩 Transfer steps

### The Run Button
Once a protocol is loaded in the timeline, hit **Run** to simulate or execute it.

---

## Tips for Better Results

**Be specific about volumes:**
> ✅ "Transfer 40 µL AMPure beads"
> ❌ "Add some beads"

**Name your positions:**
> ✅ "Source plate is P1, destination is P4, beads in TL1"
> ❌ "Move from one plate to another"

**Describe the science, not the steps:**
> ✅ "NGS cleanup using AMPure beads at 1.8x ratio"
> ❌ "Step 1: aspirate, Step 2: dispense..."

The agent knows standard lab protocols. You don't need to spell out every step.

**If it gets something wrong**, just correct it conversationally:
```
Actually, use 2x beads instead of 1.8x, and add a third ethanol wash
```

---

## Connecting to a Real Robot

For live robot control, you need the MCP server running on your computer.
See [INSTRUMENT_GUIDE.md](INSTRUMENT_GUIDE.md) for step-by-step setup per instrument.

If the MCP server isn't running, the agent will still work in **demo mode** — it plans and visualizes protocols but doesn't connect to hardware.

---

## Getting Help

- **GitHub Issues:** [github.com/nygmeta/Lab-Assistant/issues](https://github.com/nygmeta/Lab-Assistant/issues)
- **Email:** nygmetainur@gmail.com
- **Common problems:** See the [Troubleshooting section](#troubleshooting) below

---

## Troubleshooting

**"No response" from the agent**
→ Check your API key is entered correctly in the top bar. It should start with `sk-ant-`

**"Cannot reach Claude API"**
→ Make sure you're connected to the internet. If you're on a lab network, check if it blocks external API calls.

**The timeline doesn't appear**
→ Zoom out in your browser (Ctrl + minus) until both panels are visible side by side.

**The protocol looks wrong**
→ Correct it in the chat: "Actually, the source plate should be P4 not P1"

**Tips show as empty when they're not**
→ The deck map shows the configuration we have on file. Tell the agent: "P7 has a full box of 300 µL tips"
