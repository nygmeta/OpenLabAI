"""
Verification protocol for scripts/verify_ot2_hardware.py.

No pipette, no labware, no liquid. It only homes the gantry, so the robot's own
protocol analysis has something valid to analyse. The verification script uploads
this file to check upload_protocol and then deletes the idle run; it never starts it.
"""

from opentrons import protocol_api

metadata = {
    "protocolName": "OpenLabAI hardware verification (home only)",
    "description": "Homes the robot. No pipette, no labware, no liquid.",
    "apiLevel": "2.13",
}


def run(protocol: protocol_api.ProtocolContext):
    protocol.comment("OpenLabAI hardware verification: this protocol only homes the robot.")
    protocol.home()
