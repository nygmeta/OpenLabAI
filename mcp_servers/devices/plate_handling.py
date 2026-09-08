"""
Benchtop plate-handling instruments.

Each driver is a description of one instrument: what it can report, what can be
adjusted, and what it can be asked to do. The safety model lives in core.py, so
every driver here inherits the same behaviour — limits refused at the device,
and any procedure that moves hardware or applies heat gated on confirmation.

Vendor names identify the instrument classes these drivers model. Simulated
behaviour is representative, not a reproduction of any vendor's firmware.
"""

import random

from .core import Device, SafetyLimit, simulated_noise


class PlateSealer(Device):
    device_class = "sealer"
    vendor = "Azenta / Agilent class"
    model = "heat sealer"
    description = ("Applies heat-seal film to a microplate. Sealing applies heat and "
                   "compresses the plate, so it is gated.")

    def build(self) -> None:
        self.add_state("status", "idle", description="idle, sealing, heating, or error")
        self.add_state("plate_present", False, description="Whether a plate is in the drawer")
        self.add_state("drawer_open", False, description="Whether the drawer is extended")
        self.add_state("temperature", 20.0, unit="C", description="Current sealing plate temperature")
        self.add_state("set_temperature", 165.0, unit="C", writable=True,
                       description="Target seal temperature. Foil needs more heat than clear film.",
                       limit=SafetyLimit(minimum=100.0, maximum=200.0))
        self.add_state("seal_time", 2.0, unit="s", writable=True,
                       description="Dwell time under the heated platen",
                       limit=SafetyLimit(minimum=0.5, maximum=10.0))
        self.add_state("film_remaining", 480, unit="plates",
                       description="Estimated plates remaining on the film roll")

        self.add_procedure(
            "seal_plate",
            ("Seal the plate currently in the drawer at the configured temperature and "
             "dwell time. Applies heat and compresses the plate."),
            self._seal, parameters={}, physical=True, duration_s=8,
        )
        self.add_procedure(
            "open_drawer", "Extend the drawer to load or unload a plate.",
            self._open, parameters={}, physical=True, duration_s=3,
        )
        self.add_procedure(
            "close_drawer", "Retract the drawer.",
            self._close, parameters={}, physical=True, duration_s=3,
        )

    def _seal(self) -> dict:
        if not self.states["plate_present"].value:
            return {"error": "No plate in the drawer; nothing to seal."}
        if self.states["drawer_open"].value:
            return {"error": "Drawer is open. Close it before sealing."}
        if self.states["film_remaining"].value <= 0:
            return {"error": "Film roll is empty. Replace the film before sealing."}
        self.states["temperature"].value = self.states["set_temperature"].value
        self.states["film_remaining"].value -= 1
        self.states["status"].value = "idle"
        return {"sealed": True,
                "temperature_c": self.states["set_temperature"].value,
                "seal_time_s": self.states["seal_time"].value,
                "film_remaining": self.states["film_remaining"].value}

    def _open(self) -> dict:
        self.states["drawer_open"].value = True
        return {"drawer_open": True}

    def _close(self) -> dict:
        self.states["drawer_open"].value = False
        return {"drawer_open": False}


class PlatePeeler(Device):
    device_class = "peeler"
    vendor = "Azenta / Agilent class"
    model = "seal peeler"
    description = "Removes heat-seal film from a microplate. Moves hardware."

    def build(self) -> None:
        self.add_state("status", "idle", description="idle, peeling, or error")
        self.add_state("plate_present", False, description="Whether a plate is loaded")
        self.add_state("waste_full", False, description="Whether the peel-off waste bin is full")
        self.add_procedure(
            "peel_plate", "Remove the seal from the loaded plate.",
            self._peel, parameters={}, physical=True, duration_s=6,
        )

    def _peel(self) -> dict:
        if not self.states["plate_present"].value:
            return {"error": "No plate loaded; nothing to peel."}
        if self.states["waste_full"].value:
            return {"error": "Waste bin is full. Empty it before peeling."}
        return {"peeled": True}


class PlateCentrifuge(Device):
    device_class = "centrifuge"
    vendor = "Agilent / Hettich class"
    model = "microplate centrifuge"
    description = ("Spins microplates to collect liquid. Spinning is gated: the rotor "
                   "must be balanced and the lid closed.")

    def build(self) -> None:
        self.add_state("status", "idle", description="idle, spinning, decelerating, or error")
        self.add_state("lid_closed", True, description="Whether the rotor lid is closed")
        self.add_state("buckets_loaded", 0, unit="plates", writable=True,
                       description="Plates currently in the rotor",
                       limit=SafetyLimit(minimum=0, maximum=2))
        self.add_state("rotor_speed", 0, unit="rpm", description="Current rotor speed")
        self.add_procedure(
            "spin",
            "Spin the rotor at the given speed for the given time. Refuses an unbalanced rotor.",
            self._spin,
            parameters={
                "speed_rpm": {"type": "number", "unit": "rpm",
                              "description": "Rotor speed",
                              "limit": SafetyLimit(minimum=100, maximum=3000)},
                "duration_s": {"type": "number", "unit": "s",
                               "limit": SafetyLimit(minimum=1, maximum=1800)},
            },
            physical=True, duration_s=60,
        )

    def _spin(self, speed_rpm: float = 1000, duration_s: float = 60) -> dict:
        if not self.states["lid_closed"].value:
            return {"error": "Lid is open. The rotor will not spin with the lid open."}
        loaded = self.states["buckets_loaded"].value
        if loaded == 0:
            return {"error": "No plates loaded; nothing to spin."}
        if loaded == 1:
            return {"error": "Rotor is unbalanced with a single plate. "
                             "Load a balance plate opposite before spinning."}
        self.states["rotor_speed"].value = 0
        return {"spun": True, "speed_rpm": speed_rpm, "duration_s": duration_s,
                "plates": loaded}


class Incubator(Device):
    device_class = "incubator"
    vendor = "LiCONiC / Cytomat class"
    model = "shaking incubator"
    description = ("Holds plates at a controlled temperature, humidity and CO2, with "
                   "optional orbital shaking.")

    def build(self) -> None:
        self.add_state("status", "idle", description="idle, equilibrating, or error")
        self.add_state("temperature", 24.8, unit="C", description="Measured chamber temperature")
        self.add_state("set_temperature", 37.0, unit="C", writable=True,
                       description="Target chamber temperature",
                       limit=SafetyLimit(minimum=4.0, maximum=50.0))
        self.add_state("co2", 5.0, unit="%", writable=True,
                       description="Target CO2 concentration for mammalian culture",
                       limit=SafetyLimit(minimum=0.0, maximum=20.0))
        self.add_state("humidity", 85.0, unit="%", description="Measured relative humidity")
        self.add_state("shaking_rpm", 0, unit="rpm", writable=True,
                       description="Orbital shaking speed",
                       limit=SafetyLimit(minimum=0, maximum=1200))
        self.add_state("occupied_slots", 4, unit="plates", description="Plates currently stored")
        self.add_state("capacity", 44, unit="plates", description="Total plate capacity")
        self._rng = random.Random(self.device_id)

        self.add_procedure(
            "equilibrate",
            "Drive the chamber to the set temperature and report when it is within tolerance.",
            self._equilibrate, parameters={}, physical=True, duration_s=600,
        )
        self.add_procedure(
            "read_conditions", "Report current temperature, CO2 and humidity.",
            self._conditions, parameters={}, physical=False, duration_s=1,
        )

    def _equilibrate(self) -> dict:
        target = self.states["set_temperature"].value
        self.states["temperature"].value = simulated_noise(target, 0.15, self._rng)
        return {"equilibrated": True, "set_temperature_c": target,
                "measured_c": self.states["temperature"].value}

    def _conditions(self) -> dict:
        return {"temperature_c": self.states["temperature"].value,
                "co2_percent": self.states["co2"].value,
                "humidity_percent": self.states["humidity"].value,
                "shaking_rpm": self.states["shaking_rpm"].value}


class Thermocycler(Device):
    device_class = "thermocycler"
    vendor = "Bio-Rad / Applied Biosystems class"
    model = "96-well thermal cycler"
    description = ("Runs PCR temperature programmes. Heating the block and closing the "
                   "heated lid are gated.")

    def build(self) -> None:
        self.add_state("status", "idle", description="idle, cycling, holding, or error")
        self.add_state("block_temperature", 22.0, unit="C", description="Measured block temperature")
        self.add_state("lid_temperature", 22.0, unit="C", description="Measured heated-lid temperature")
        self.add_state("lid_closed", False, writable=True,
                       description="Whether the heated lid is closed",
                       limit=SafetyLimit(allowed=[True, False]))
        self.add_state("plate_present", False, description="Whether a plate is in the block")
        self.add_procedure(
            "run_program",
            ("Run a PCR programme: initial denaturation, then cycles of denature, anneal "
             "and extend, then a final hold. Applies heat."),
            self._run,
            parameters={
                "denature_c": {"type": "number", "unit": "C",
                               "limit": SafetyLimit(minimum=90, maximum=99)},
                "anneal_c": {"type": "number", "unit": "C",
                             "limit": SafetyLimit(minimum=45, maximum=72)},
                "extend_c": {"type": "number", "unit": "C",
                             "limit": SafetyLimit(minimum=60, maximum=78)},
                "cycles": {"type": "number",
                           "limit": SafetyLimit(minimum=1, maximum=50)},
                "hold_c": {"type": "number", "unit": "C",
                           "limit": SafetyLimit(minimum=4, maximum=25)},
            },
            physical=True, duration_s=5400,
        )

    def _run(self, denature_c: float = 95, anneal_c: float = 60, extend_c: float = 72,
             cycles: float = 30, hold_c: float = 10) -> dict:
        if not self.states["plate_present"].value:
            return {"error": "No plate in the block."}
        if not self.states["lid_closed"].value:
            return {"error": "Heated lid is open. Close the lid before running a programme."}
        self.states["block_temperature"].value = hold_c
        self.states["lid_temperature"].value = 105.0
        estimated_min = round((cycles * (30 + 30 + 60)) / 60 + 5, 1)
        return {"completed": True, "cycles": int(cycles),
                "denature_c": denature_c, "anneal_c": anneal_c, "extend_c": extend_c,
                "hold_c": hold_c, "estimated_minutes": estimated_min}


class BarcodeReader(Device):
    device_class = "barcode_reader"
    vendor = "Ziath / Code class"
    model = "plate and tube barcode reader"
    description = ("Reads plate and tube barcodes. Reading is optical and moves nothing, "
                   "so it is ungated — this is how a workflow confirms it has the right plate.")

    def build(self) -> None:
        self.add_state("status", "idle", description="idle, reading, or error")
        self.add_state("last_barcode", "", description="Most recently decoded barcode")
        self._rng = random.Random(self.device_id)
        self.add_procedure(
            "read_plate_barcode", "Decode the barcode on the plate currently presented.",
            self._read_plate, parameters={}, physical=False, duration_s=2,
        )
        self.add_procedure(
            "read_rack", "Decode every tube barcode in a rack.",
            self._read_rack,
            parameters={"rows": {"type": "number", "limit": SafetyLimit(minimum=1, maximum=16)},
                        "columns": {"type": "number", "limit": SafetyLimit(minimum=1, maximum=24)}},
            physical=False, duration_s=6,
        )

    def _read_plate(self) -> dict:
        code = f"PLATE-{self._rng.randint(10000, 99999)}"
        self.states["last_barcode"].value = code
        return {"barcode": code}

    def _read_rack(self, rows: float = 8, columns: float = 12) -> dict:
        codes = {}
        for r in range(int(rows)):
            for c in range(int(columns)):
                codes[f"{'ABCDEFGHIJKLMNOP'[r]}{c+1}"] = f"TUBE-{self._rng.randint(100000, 999999)}"
        return {"tube_count": len(codes), "barcodes": codes}


class PlateHotel(Device):
    device_class = "storage"
    vendor = "Generic"
    model = "plate hotel"
    description = "Passive plate storage with addressable slots. Presenting a plate moves hardware."

    def build(self) -> None:
        self.add_state("status", "idle", description="idle, moving, or error")
        self.add_state("capacity", 22, unit="slots", description="Total slots")
        self.add_state("occupied", 3, unit="slots", description="Slots currently holding a plate")
        self.add_state("presented_slot", 0, description="Slot currently presented, 0 for none")
        self.add_procedure(
            "present_slot", "Move the requested slot to the transfer position.",
            self._present,
            parameters={"slot": {"type": "number",
                                 "limit": SafetyLimit(minimum=1, maximum=22)}},
            physical=True, duration_s=8,
        )
        self.add_procedure(
            "inventory", "List which slots are occupied.",
            self._inventory, parameters={}, physical=False, duration_s=1,
        )

    def _present(self, slot: float = 1) -> dict:
        self.states["presented_slot"].value = int(slot)
        return {"presented_slot": int(slot)}

    def _inventory(self) -> dict:
        occupied = list(range(1, int(self.states["occupied"].value) + 1))
        return {"capacity": self.states["capacity"].value,
                "occupied_slots": occupied,
                "free_slots": self.states["capacity"].value - len(occupied)}
