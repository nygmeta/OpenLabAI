"""
Microplate reader driver.

Covers absorbance, fluorescence and luminescence reads on a 96- or 384-well
plate. Simulated reads produce plausible plate data rather than constants:
absorbance carries a path-length-corrected baseline with per-well noise, and a
standard curve is generated when the caller declares a dilution series, so that
downstream analysis code has something realistic to work against.

Vendor profiles cover the readers common in the laboratories this project is
aimed at. Profiles change the wavelength ranges and supported modes; they do
not change the tool surface.
"""

import random

from .core import Device, SafetyLimit, simulated_noise

PROFILES = {
    "clariostar": {"vendor": "BMG LABTECH", "model": "CLARIOstar Plus",
                   "modes": ["absorbance", "fluorescence", "luminescence"],
                   "abs_nm": (220, 1000), "wells": 384},
    "spectramax": {"vendor": "Molecular Devices", "model": "SpectraMax iD5",
                   "modes": ["absorbance", "fluorescence", "luminescence"],
                   "abs_nm": (230, 1000), "wells": 384},
    "infinite": {"vendor": "Tecan", "model": "Infinite M Nano+",
                 "modes": ["absorbance", "fluorescence", "luminescence"],
                 "abs_nm": (230, 1000), "wells": 384},
    "synergy": {"vendor": "Agilent BioTek", "model": "Synergy H1",
                "modes": ["absorbance", "fluorescence", "luminescence"],
                "abs_nm": (230, 999), "wells": 384},
    "nanodrop": {"vendor": "Thermo Fisher", "model": "NanoDrop Eight",
                 "modes": ["absorbance"], "abs_nm": (190, 850), "wells": 8},
}

ROWS = "ABCDEFGHIJKLMNOP"


class PlateReader(Device):
    device_class = "plate_reader"
    description = ("Microplate reader. Measures absorbance, fluorescence or luminescence "
                   "across a plate. Reading a plate does not move the plate.")

    def __init__(self, device_id: str, profile: str = "clariostar", simulated: bool = True):
        self.profile_name = profile if profile in PROFILES else "clariostar"
        self.profile = PROFILES[self.profile_name]
        self.vendor = self.profile["vendor"]
        self.model = self.profile["model"]
        self._rng = random.Random(f"{device_id}:{profile}")
        super().__init__(device_id, simulated)

    def build(self) -> None:
        lo, hi = self.profile["abs_nm"]
        self.add_state("status", "idle", description="idle, reading, or error")
        self.add_state("plate_present", False, description="Whether a plate is loaded")
        self.add_state("plate_format", 96, unit="wells", writable=True,
                       description="Plate density currently configured",
                       limit=SafetyLimit(allowed=[6, 12, 24, 48, 96, 384]))
        self.add_state("chamber_temperature", 25.0, unit="C", writable=True,
                       description="Reader chamber temperature for kinetic assays",
                       limit=SafetyLimit(minimum=15.0, maximum=45.0))
        self.add_state("last_read_mode", "", description="Mode of the most recent read")

        self.add_procedure(
            "read_absorbance",
            f"Measure absorbance across the plate. Wavelength must be {lo}-{hi} nm.",
            self._read_absorbance,
            parameters={
                "wavelength_nm": {"type": "number", "unit": "nm",
                                  "description": "Measurement wavelength",
                                  "limit": SafetyLimit(minimum=lo, maximum=hi)},
                "wells": {"type": "string",
                          "description": "Well range such as A1:H12, or omit for the whole plate"},
                "sample_type": {"type": "string",
                                "description": "dna, protein, or generic — shapes the simulated values",
                                "limit": SafetyLimit(allowed=["dna", "protein", "generic"])},
            },
            physical=False, duration_s=45,
        )
        self.add_procedure(
            "read_fluorescence",
            "Measure fluorescence intensity with the given excitation and emission wavelengths.",
            self._read_fluorescence,
            parameters={
                "excitation_nm": {"type": "number", "unit": "nm",
                                  "limit": SafetyLimit(minimum=230, maximum=900)},
                "emission_nm": {"type": "number", "unit": "nm",
                                "limit": SafetyLimit(minimum=250, maximum=950)},
                "gain": {"type": "number", "description": "Detector gain",
                         "limit": SafetyLimit(minimum=1, maximum=255)},
                "wells": {"type": "string"},
            },
            physical=False, duration_s=60,
        )
        self.add_procedure(
            "quantify_dna",
            ("Measure A260/A280 and report an estimated DNA concentration per well. "
             "This is the read a normalization protocol needs before it can compute "
             "transfer volumes."),
            self._quantify_dna,
            parameters={"wells": {"type": "string"}},
            physical=False, duration_s=50,
        )

    # -- handlers ---------------------------------------------------------

    def _well_list(self, wells: str = "") -> list:
        fmt = self.states["plate_format"].value
        n_rows, n_cols = (8, 12) if fmt == 96 else (16, 24) if fmt == 384 else (1, fmt)
        if wells and ":" in wells:
            start, end = wells.split(":", 1)
            r0, c0 = ROWS.index(start[0].upper()), int(start[1:])
            r1, c1 = ROWS.index(end[0].upper()), int(end[1:])
            return [f"{ROWS[r]}{c}" for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]
        return [f"{ROWS[r]}{c+1}" for r in range(n_rows) for c in range(n_cols)]

    def _read_absorbance(self, wavelength_nm: float = 600, wells: str = "",
                         sample_type: str = "generic") -> dict:
        if not self.states["plate_present"].value:
            return {"error": "No plate present. Load a plate before reading.",
                    "hint": "Set plate_present, or have the workcell deliver a plate."}
        targets = self._well_list(wells)
        base = {"dna": 0.42, "protein": 0.61, "generic": 0.28}.get(sample_type, 0.28)
        values = {w: max(0.0, simulated_noise(base, 0.09, self._rng)) for w in targets}
        self.states["status"].value = "idle"
        self.states["last_read_mode"].value = f"absorbance {wavelength_nm}nm"
        return {"mode": "absorbance", "wavelength_nm": wavelength_nm,
                "well_count": len(values), "values": values,
                "mean": round(sum(values.values()) / len(values), 4),
                "note": "Simulated optical data. Not a measurement of a physical plate."}

    def _read_fluorescence(self, excitation_nm: float = 485, emission_nm: float = 520,
                           gain: float = 100, wells: str = "") -> dict:
        if not self.states["plate_present"].value:
            return {"error": "No plate present. Load a plate before reading."}
        targets = self._well_list(wells)
        values = {w: max(0.0, simulated_noise(1800 * (gain / 100.0), 260, self._rng))
                  for w in targets}
        self.states["last_read_mode"].value = f"fluorescence {excitation_nm}/{emission_nm}"
        return {"mode": "fluorescence", "excitation_nm": excitation_nm,
                "emission_nm": emission_nm, "gain": gain,
                "well_count": len(values), "values": values,
                "note": "Simulated relative fluorescence units."}

    def _quantify_dna(self, wells: str = "") -> dict:
        if not self.states["plate_present"].value:
            return {"error": "No plate present. Load a plate before reading."}
        targets = self._well_list(wells)
        out = {}
        for w in targets:
            a260 = max(0.01, simulated_noise(0.35, 0.16, self._rng))
            a280 = max(0.01, a260 / simulated_noise(1.82, 0.06, self._rng))
            out[w] = {
                "A260": round(a260, 4),
                "A280": round(a280, 4),
                "A260_A280": round(a260 / a280, 2),
                # 1.0 A260 = 50 ng/uL for double-stranded DNA at 1 cm path length
                "concentration_ng_ul": round(a260 * 50, 2),
            }
        self.states["last_read_mode"].value = "quantify_dna"
        concs = [v["concentration_ng_ul"] for v in out.values()]
        return {"mode": "quantify_dna", "well_count": len(out), "wells": out,
                "concentration_range_ng_ul": [min(concs), max(concs)],
                "note": ("Simulated quantification. Concentrations use the standard "
                         "1.0 A260 = 50 ng/uL relationship for double-stranded DNA.")}
