"""Convert between common units of length, mass, volume and temperature."""

from taskdeck.core import Field, JobPlugin, Pipeline, QuickAction, Stage, StepError, step
from taskdeck.shared import steps

# unit -> (dimension, factor to SI base unit)
UNITS = {
    "m": ("length", 1.0), "km": ("length", 1000.0), "cm": ("length", 0.01), "mm": ("length", 0.001),
    "mi": ("length", 1609.344), "yd": ("length", 0.9144), "ft": ("length", 0.3048), "in": ("length", 0.0254),
    "kg": ("mass", 1.0), "g": ("mass", 0.001), "lb": ("mass", 0.45359237), "oz": ("mass", 0.028349523125),
    "L": ("volume", 1.0), "mL": ("volume", 0.001), "gal (US)": ("volume", 3.785411784), "cup (US)": ("volume", 0.2365882365),
    "°C": ("temperature", None), "°F": ("temperature", None), "K": ("temperature", None),
}

_TO_K = {"°C": lambda v: v + 273.15, "°F": lambda v: (v - 32) * 5 / 9 + 273.15, "K": lambda v: v}
_FROM_K = {"°C": lambda k: k - 273.15, "°F": lambda k: (k - 273.15) * 9 / 5 + 32, "K": lambda k: k}


@step(Stage.VALIDATE)
def same_dimension(ctx):
    a, b = UNITS[ctx["from_unit"]][0], UNITS[ctx["to_unit"]][0]
    if a != b:
        raise StepError(f"Can't convert {a} to {b}")


@step(Stage.EXECUTE)
def convert(ctx):
    value, src, dst = ctx["value"], ctx["from_unit"], ctx["to_unit"]
    dim = UNITS[src][0]
    if dim == "temperature":
        if _TO_K[src](value) < 0:
            raise StepError("Temperature is below absolute zero")
        out = _FROM_K[dst](_TO_K[src](value))
    else:
        out = value * UNITS[src][1] / UNITS[dst][1]
    ctx["conversion"] = {f"{value:g} {src}": f"{out:,.6g} {dst}", "Dimension": dim}


class UnitConvert(JobPlugin):
    id = "unit_convert"
    title = "Unit Converter"
    description = "Length, mass, volume and temperature conversions."
    category = "Everyday"
    icon = "SWAP_HORIZ"
    tags = ("units", "metric", "imperial", "km", "miles", "celsius", "fahrenheit")
    fields = (
        Field.number("value", "Value", required=True, default=1),
        Field.choice("from_unit", "From", UNITS.keys()),
        Field.choice("to_unit", "To", UNITS.keys(), default="km"),
    )
    quick_actions = (
        QuickAction("Convert: km to mi", {"from_unit": "km", "to_unit": "mi"}),
        QuickAction("Convert: °F to °C", {"from_unit": "°F", "to_unit": "°C"}),
        QuickAction("Convert: lb to kg", {"from_unit": "lb", "to_unit": "kg"}),
    )

    def build_pipeline(self):
        return Pipeline([steps.require("value"), same_dimension, convert, steps.present_rows("conversion")])


PLUGIN = UnitConvert()
