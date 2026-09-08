"""Benchtop device drivers and the shared device abstraction."""

from .core import (Device, DeviceRegistry, Procedure, SafetyLimit,
                   SafetyViolation, State)
from .plate_reader import PlateReader
from .plate_handling import (BarcodeReader, Incubator, PlateCentrifuge,
                             PlateHotel, PlatePeeler, PlateSealer, Thermocycler)


def default_registry() -> DeviceRegistry:
    """The bench a laboratory is assumed to have until told otherwise.

    Everything is simulated. Nothing here contacts hardware.
    """
    registry = DeviceRegistry()
    registry.add(PlateReader("reader1", profile="clariostar"))
    registry.add(PlateReader("nanodrop1", profile="nanodrop"))
    registry.add(PlateSealer("sealer1"))
    registry.add(PlatePeeler("peeler1"))
    registry.add(PlateCentrifuge("spinner1"))
    registry.add(Incubator("incubator1"))
    registry.add(Thermocycler("cycler1"))
    registry.add(BarcodeReader("scanner1"))
    registry.add(PlateHotel("hotel1"))
    return registry


__all__ = ["Device", "DeviceRegistry", "Procedure", "SafetyLimit", "SafetyViolation",
           "State", "PlateReader", "PlateSealer", "PlatePeeler", "PlateCentrifuge",
           "Incubator", "Thermocycler", "BarcodeReader", "PlateHotel", "default_registry"]
