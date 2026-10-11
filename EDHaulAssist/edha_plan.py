"""The haul plan: parsing and validating the JSON a planning tool exports.

EDHaulAssist does no planning of its own. Something else decides what to buy
where; this module only reads that decision and refuses anything it cannot follow
exactly, because a plan misread is a round trip wasted.

Schema 1:

    {
      "schema": 1,
      "name": "Orbital outpost - HIP 1234",          (optional)
      "runs": [
        { "stops": [
            { "system": "Sol", "station": "Daedalus",
              "commodities": [ {"name": "Steel", "quantity": 400} ] }
        ] }
      ]
    }

A Run is one round trip ending at the Sell Location, which is NOT in the file -
it is configured in the plugin. A Stop is a station at which one or more
commodities are bought in a stated quantity.

Nothing here imports EDMC or tkinter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import edha_commodities

SCHEMA = 1


class PlanError(ValueError):
    """The file cannot be followed. The message says where and why."""


@dataclass(frozen=True)
class Item:
    name: str          # for display
    key: str           # edha_commodities.lookup_key of the journal symbol
    quantity: int


@dataclass(frozen=True)
class Stop:
    system: str
    station: str
    items: Tuple[Item, ...]

    @property
    def tonnes(self) -> int:
        return sum(item.quantity for item in self.items)


@dataclass(frozen=True)
class Run:
    stops: Tuple[Stop, ...]

    @property
    def tonnes(self) -> int:
        return sum(stop.tonnes for stop in self.stops)

    def planned(self) -> Dict[str, int]:
        """Tonnes of each commodity across the whole run, by key."""
        totals: Dict[str, int] = {}
        for stop in self.stops:
            for item in stop.items:
                totals[item.key] = totals.get(item.key, 0) + item.quantity
        return totals

    def names(self) -> Dict[str, str]:
        """Display name for each commodity key in the run."""
        return {item.key: item.name for stop in self.stops for item in stop.items}


@dataclass(frozen=True)
class Plan:
    name: str
    runs: Tuple[Run, ...]
    #: The document as imported. It is what gets persisted, so a saved session
    #: is re-read by the same code that read the file.
    raw: Dict[str, Any]

    @property
    def tonnes(self) -> int:
        return sum(run.tonnes for run in self.runs)


def _text(value: Any, where: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError("{}: {} is missing.".format(where, what))
    return value.strip()


def _quantity(value: Any, where: str) -> int:
    # bool is an int in Python; `true` tonnes is a mistake, not one tonne.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError("{}: quantity must be a number.".format(where))
    if value != int(value) or value <= 0:
        raise PlanError("{}: quantity must be a whole number above zero.".format(where))
    return int(value)


def _item(data: Any, where: str, warnings: List[str]) -> Item:
    if not isinstance(data, dict):
        raise PlanError("{}: a commodity must be an object with name and quantity.".format(where))
    name = _text(data.get("name"), where, "the commodity name")
    quantity = _quantity(data.get("quantity"), "{} ({})".format(where, name))
    resolution = edha_commodities.resolve(name)
    if resolution.ok:
        return Item(name=resolution.commodity.name,
                    key=edha_commodities.lookup_key(resolution.commodity.symbol),
                    quantity=quantity)
    # Not fatal: a commodity newer than the bundled table still tracks, because
    # the journal carries the localised name alongside its symbol.
    hint = ""
    if resolution.suggestions:
        hint = " Did you mean {}?".format(" / ".join(resolution.suggestions[:3]))
    warnings.append("{}: unknown commodity {!r}.{}".format(where, name, hint))
    return Item(name=name, key=edha_commodities.lookup_key(name), quantity=quantity)


def _stop(data: Any, where: str, warnings: List[str]) -> Stop:
    if not isinstance(data, dict):
        raise PlanError("{}: a stop must be an object.".format(where))
    system = _text(data.get("system"), where, "the system")
    station = _text(data.get("station"), where, "the station")
    listed = data.get("commodities")
    if not isinstance(listed, list) or not listed:
        raise PlanError("{}: no commodities to buy.".format(where))
    merged: Dict[str, Item] = {}
    for item in (_item(entry, where, warnings) for entry in listed):
        # The same commodity listed twice at one stop is one purchase.
        seen = merged.get(item.key)
        merged[item.key] = item if seen is None else Item(
            item.name, item.key, seen.quantity + item.quantity)
    return Stop(system=system, station=station, items=tuple(merged.values()))


def parse(data: Any) -> Tuple[Plan, List[str]]:
    """Validate a decoded plan document. Returns the plan and any warnings."""
    if not isinstance(data, dict):
        raise PlanError("The plan must be a JSON object.")
    if data.get("schema") != SCHEMA:
        raise PlanError("Unsupported plan schema {!r}; this plugin reads schema {}.".format(
            data.get("schema"), SCHEMA))
    listed = data.get("runs")
    if not isinstance(listed, list) or not listed:
        raise PlanError("The plan has no runs.")

    warnings: List[str] = []
    runs: List[Run] = []
    for number, run in enumerate(listed, start=1):
        where = "Run {}".format(number)
        stops = run.get("stops") if isinstance(run, dict) else None
        if not isinstance(stops, list) or not stops:
            raise PlanError("{}: no stops.".format(where))
        runs.append(Run(stops=tuple(
            _stop(stop, "{} stop {}".format(where, index), warnings)
            for index, stop in enumerate(stops, start=1))))

    name = data.get("name")
    name = name.strip() if isinstance(name, str) else ""
    return Plan(name=name, runs=tuple(runs), raw=data), warnings


def load_file(path: str) -> Tuple[Plan, List[str]]:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:   # exporters may write a BOM
            data = json.load(handle)
    except OSError as exc:
        raise PlanError("Could not read the plan: {}".format(exc.strerror or exc))
    except ValueError as exc:
        raise PlanError("The plan is not valid JSON: {}".format(exc))
    return parse(data)


def _self_test() -> None:
    good = {"schema": 1, "name": " Demo ", "runs": [{"stops": [
        {"system": "Sol", "station": "Daedalus", "commodities": [
            {"name": "steel", "quantity": 400},
            {"name": "Land Enrichment Systems", "quantity": 20},
            {"name": "Steel", "quantity": 100}]}]}]}
    plan, warnings = parse(good)
    assert not warnings and plan.name == "Demo" and plan.tonnes == 520
    stop = plan.runs[0].stops[0]
    assert [(i.name, i.key, i.quantity) for i in stop.items] == [
        ("Steel", "steel", 500),
        ("Land Enrichment Systems", "terrainenrichmentsystems", 20)], stop.items

    odd = {"schema": 1, "runs": [{"stops": [{"system": "A", "station": "B", "commodities": [
        {"name": "Titanum", "quantity": 5}]}]}]}
    plan, warnings = parse(odd)
    assert len(warnings) == 1 and "Titanium" in warnings[0], warnings
    assert plan.runs[0].stops[0].items[0].key == "titanum"

    for broken in ([], {"schema": 2, "runs": []}, {"schema": 1, "runs": []},
                   {"schema": 1, "runs": [{"stops": []}]},
                   {"schema": 1, "runs": [{"stops": [{"system": "A", "station": "",
                                                       "commodities": []}]}]},
                   {"schema": 1, "runs": [{"stops": [{"system": "A", "station": "B",
                                                       "commodities": [
                                                           {"name": "Steel", "quantity": 0}]}]}]}):
        try:
            parse(broken)
        except PlanError:
            continue
        raise AssertionError("accepted {!r}".format(broken))

    print("edha_plan self-test: OK")


if __name__ == "__main__":
    _self_test()
