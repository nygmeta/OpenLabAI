# Example: NGS Library Cleanup with AMPure Beads

This walkthrough shows a scientist with no coding experience using Lab-Assistant to plan and run an NGS library cleanup.

---

## Setup

- Robot: Opentrons OT-2
- Sample plate: slot 1 (96-well plate, 50 µL PCR product per well)
- Destination plate: slot 4 (96-well plate, empty)
- AMPure beads: slot 10 (reservoir, column 1)
- 80% Ethanol: slot 10 (reservoir, column 2)
- EB buffer: slot 10 (reservoir, column 3)
- Tips: slot 7 and 8 (300 µL tips)

---

## The Conversation

**Scientist:**
> Read my deck and tell me what's loaded

**Agent:**
> Your OT-2 deck currently has:
> - Slot 1: Corning 96-well plate (sample plate — 50 µL PCR product)
> - Slot 4: Corning 96-well plate (empty, destination)
> - Slot 7: Opentrons 300 µL tip rack (full)
> - Slot 8: Opentrons 300 µL tip rack (full)
> - Slot 10: Agilent reservoir (reagents)
>
> Ready for your protocol. What would you like to run?

---

**Scientist:**
> AMPure bead cleanup for my NGS library. 1.8x bead ratio, samples are 50 µL.
> 2x 80% ethanol wash, elute in 20 µL EB buffer.

**Agent:**
> Here's your NGS cleanup protocol — 11 steps, ~28 minutes:
> [Protocol generated — timeline updated →]
>
> **Protocol: NGS_AMPure_Cleanup**
>
> Step 1: Pick up tips (column 1, slot 7)
> Step 2: Aspirate 90 µL AMPure beads from reservoir column 1
> Step 3: Dispense 90 µL beads to sample plate column 1
> Step 4: Mix 10x at 80 µL to bind DNA to beads
> Step 5: Incubate 5 min on magnet (pause step)
> Step 6: Remove supernatant — aspirate 130 µL to waste
> Step 7: Wash 1 — add 150 µL 80% ethanol, incubate 30 sec, aspirate
> Step 8: Wash 2 — add 150 µL 80% ethanol, incubate 30 sec, aspirate
> Step 9: Air dry beads 2 min (pause step)
> Step 10: Elute — add 20 µL EB buffer to beads, mix 10x
> Step 11: Transfer 18 µL eluate to destination plate
>
> Total: ~28 min for full 96-well plate. Repeat pattern for each column.

---

**Scientist:**
> Looks good. Can you also add a brief mix step after the bead addition before the incubation?

**Agent:**
> Updated — added mixing step (5x at 80 µL) immediately after bead dispense in step 4, before the 5-minute magnet incubation. Timeline updated.

---

**Scientist:**
> Run it.

**Agent:**
> Protocol started — 11 steps queued. Running Step 1: Pick up tips...

---

## Key Points

1. **The scientist never wrote code.** The entire interaction was plain English.
2. **The agent knew the protocol.** It understood "AMPure bead cleanup" and "1.8x ratio" without needing step-by-step instructions.
3. **Corrections were conversational.** Adding the extra mix step required one sentence, not editing a Python file.
4. **Total time from description to running:** ~4 minutes.

Compare to manual method development: 30 minutes minimum for a simple protocol, 2-3 hours for a full 96-well optimized cleanup.
