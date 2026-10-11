"""The haul session: where you are in the plan, and what the journal says about it.

A plan is a list of Runs. A Run is a list of Stops followed by a delivery at the
Sell Location. This module decides which Stop is next, ticks Stops off as
`MarketBuy` events arrive, ticks the Run off as deliveries arrive, and keeps the
clock that the ETA is worked out from.

It imports nothing from EDMC or tkinter. Every method that changes something
returns a list of `Effect`s for load.py to carry out (copy to the clipboard,
redraw, save), which is what lets tests/test_core.py drive a whole haul in-process.

Two clocks are in play and they are never subtracted from one another:

- Journal timestamps are used only to REJECT replayed events. EDMC re-reads the
  tail of the current journal when it loads, and a Start pressed while that is
  draining would otherwise credit a purchase made forty minutes ago.
- Elapsed time is wall clock, injected as `now`. The journal falls silent while
  you are docked, so a journal-clocked stopwatch would freeze exactly when you
  are sitting looking at it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import edha_commodities
import edha_plan

# --- display roles -----------------------------------------------------------

OFF = "off"      # stopped, or nothing to guide: the overlay stays blank
BUY = "buy"      # heading for a Stop
SELL = "sell"    # every Stop dealt with; heading for the Sell Location

# --- line roles in overview(), the pop-out view of the whole plan ------------

NOTE = "note"        # the heading and statistics
DONE = "done"        # a run already behind you
CURRENT = "current"  # the run you are on
TODO = "todo"        # a run still to come

# --- effects handed back to load.py ------------------------------------------

CLIPBOARD = "clipboard"   # payload: the system name to copy
REDRAW = "redraw"
SAVE = "save"
FLASH = "flash"           # payload: lines to show once on the overlay

STATE_VERSION = 1

#: How far behind the Start press a journal timestamp may be and still count.
#: Covers the game's clock and this machine's disagreeing by a little.
REPLAY_SLACK = timedelta(seconds=60)

#: Item lines wrap at about this width so a ten-commodity Stop stays readable.
ITEM_LINE_WIDTH = 44

STOP_KIND = "stop"
SELL_KIND = "sell"


@dataclass(frozen=True)
class Effect:
    kind: str
    payload: Any = None


@dataclass
class SellLocation:
    """Where a Run is delivered. The station is optional: without one, a
    delivery anywhere in the system counts."""

    system: str = ""
    station: str = ""
    market_id: Optional[int] = None

    def label(self) -> str:
        system = self.system.strip() or "(sell system not set)"
        return "{} / {}".format(system, self.station) if self.station else system


@dataclass
class Stats:
    runs_done: int = 0
    runs_total: int = 0
    delivered: int = 0
    planned: int = 0
    active_seconds: float = 0.0
    avg_run_seconds: Optional[float] = None
    tonnes_per_hour: Optional[float] = None
    eta_seconds: Optional[float] = None
    shortfall: Dict[str, int] = field(default_factory=dict)


def _now(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _same(a: Optional[str], b: Optional[str]) -> bool:
    """Names match ignoring case and stray spaces. Two blanks do not match."""
    left = (a or "").strip().casefold()
    return bool(left) and left == (b or "").strip().casefold()


def parse_timestamp(text: Any) -> Optional[datetime]:
    """A journal timestamp ('2026-10-11T13:45:44Z') as an aware datetime."""
    if not isinstance(text, str) or not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--"
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    return "{}h {:02d}m".format(hours, minutes) if hours else "{}m".format(minutes)


def _count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class Session:
    def __init__(self) -> None:
        self.plan: Optional[edha_plan.Plan] = None
        self.warnings: List[str] = []
        self.sell = SellLocation()
        self.running = False
        #: Wall clock at the start of the live segment: the Start press, or the
        #: completion of the previous run if that came later.
        self.started_at: Optional[datetime] = None
        self.message = ""

        # Where the commander is, tracked whether or not a session is running so
        # that "Here" and the first purchase after Start both have it.
        self.loc_system = ""
        self.loc_station = ""
        self.loc_market_id: Optional[int] = None

        self.run_index = 0
        self.history: List[Dict[str, Any]] = []
        #: Banked seconds; the live segment is added on top by _live().
        self.active_seconds = 0.0
        self.run_seconds = 0.0
        self.bought: List[Dict[str, int]] = []
        self.skipped: Set[int] = set()
        self.delivered: Dict[str, int] = {}
        self.delivered_any = False

    # --- where we are in the plan -------------------------------------------

    @property
    def complete(self) -> bool:
        return self.plan is not None and self.run_index >= len(self.plan.runs)

    @property
    def current_run(self) -> Optional[edha_plan.Run]:
        if self.plan is None or self.complete:
            return None
        return self.plan.runs[self.run_index]

    @property
    def has_progress(self) -> bool:
        return bool(self.run_index or self.history or self.skipped or self.delivered
                    or any(self.bought))

    def stop_done(self, index: int) -> bool:
        run = self.current_run
        if run is None:
            return False
        got = self.bought[index]
        return all(got.get(item.key, 0) >= item.quantity for item in run.stops[index].items)

    def stop_closed(self, index: int) -> bool:
        return index in self.skipped or self.stop_done(index)

    def target(self) -> Optional[Tuple[str, int]]:
        """What to fly to next: the first open Stop in plan order, else the Sell
        Location. None when there is no run to guide."""
        run = self.current_run
        if run is None:
            return None
        for index in range(len(run.stops)):
            if not self.stop_closed(index):
                return (STOP_KIND, index)
        return (SELL_KIND, -1)

    def _position(self) -> Tuple[int, Optional[Tuple[str, int]]]:
        """Run and target together. The target alone cannot tell the first Stop
        of one run from the first Stop of the next."""
        return (self.run_index, self.target())

    def clipboard_text(self) -> str:
        """The system to paste into the galaxy map."""
        target = self.target()
        if target is None:
            return ""
        if target[0] == SELL_KIND:
            return self.sell.system.strip()
        return self.current_run.stops[target[1]].system

    def _reset_run(self) -> None:
        run = self.current_run
        self.bought = [dict() for _ in run.stops] if run else []
        self.skipped = set()
        self.delivered = {}
        self.delivered_any = False
        self.run_seconds = 0.0

    def _reset_progress(self) -> None:
        self.run_index = 0
        self.history = []
        self.active_seconds = 0.0
        self._reset_run()

    # --- the clock ----------------------------------------------------------

    def _live(self, now: datetime) -> float:
        if not self.running or self.started_at is None:
            return 0.0
        return max(0.0, (now - self.started_at).total_seconds())

    def is_replay(self, stamp: Optional[datetime]) -> bool:
        if stamp is None or self.started_at is None:
            return False
        return stamp < self.started_at - REPLAY_SLACK

    # --- commands from the panel --------------------------------------------

    def load_plan(self, plan: edha_plan.Plan, warnings: Optional[List[str]] = None,
                  now: Optional[datetime] = None) -> List[Effect]:
        """Replace the plan. Progress on the previous one is discarded."""
        self.running = False
        self.started_at = None
        self.plan = plan
        self.warnings = list(warnings or [])
        self._reset_progress()
        self.message = "Loaded {} - {} run{}, {:,} t.".format(
            plan.name or "plan", len(plan.runs), "" if len(plan.runs) == 1 else "s",
            plan.tonnes)
        return [Effect(SAVE), Effect(REDRAW)]

    def start(self, now: Optional[datetime] = None,
              cargo: Optional[Dict[str, int]] = None) -> Tuple[bool, List[Effect]]:
        """Begin guiding. `cargo` is what is in the hold right now (commodity ->
        tonnes, in any spelling) or None if that is not known; anything aboard
        that the run wants and the session has not counted is credited first."""
        if self.plan is None:
            self.message = "Load a haul plan first."
        elif self.complete:
            self.message = "This plan is complete. Reset progress or load another."
        elif not self.sell.system.strip():
            self.message = "Set the sell system first."
        else:
            self.running = True
            self.started_at = _now(now)
            found = self._reconcile(self.current_run, cargo)
            self.message = "Already aboard: {}.".format(", ".join(
                "{} {:,}".format(name, amount) for name, amount in found)) if found else ""
            effects = [Effect(CLIPBOARD, self.clipboard_text()), Effect(REDRAW)]
            return True, ([Effect(SAVE)] + effects) if found else effects
        return False, [Effect(REDRAW)]

    def stop(self, now: Optional[datetime] = None) -> List[Effect]:
        if self.running:
            live = self._live(_now(now))
            self.active_seconds += live
            self.run_seconds += live
        self.running = False
        self.started_at = None
        return [Effect(SAVE), Effect(REDRAW)]

    def skip_stop(self, now: Optional[datetime] = None) -> List[Effect]:
        """Give up on the current Stop and move guidance on. What was bought
        there still counts; what was not becomes shortfall when the run ends."""
        target = self.target()
        if target is None or target[0] != STOP_KIND:
            return []
        before = self._position()
        self.skipped.add(target[1])
        return self._after_change(before, _now(now))

    def next_run(self, now: Optional[datetime] = None) -> List[Effect]:
        """Close the current run by hand - e.g. the site took less than you
        carried, so the cargo the plugin is waiting on will never be delivered."""
        if self.current_run is None:
            return []
        before = self._position()
        self._complete_run(_now(now), manual=True)
        return self._after_change(before, _now(now))

    def goto_run(self, number: int, now: Optional[datetime] = None) -> List[Effect]:
        """Move to run `number` (1-based).

        Forward is for picking a plan up part-way through: the runs passed over
        were flown without the plugin, so they are recorded as delivered in full
        rather than as shortfall. Backward discards what was recorded from that
        run onward, which undoes a jump or a Next run pressed by mistake.
        """
        if self.plan is None:
            return []
        index = number - 1
        if not 0 <= index < len(self.plan.runs) or index == self.run_index:
            return []
        when = _now(now)
        before = self._position()
        self.active_seconds += self._live(when)
        if self.running:
            self.started_at = when
        if index > self.run_index:
            for passed in range(self.run_index, index):
                run = self.plan.runs[passed]
                self.history.append({
                    "run": passed + 1, "seconds": 0.0, "planned": run.tonnes,
                    "delivered": run.tonnes, "shortfall": {}, "manual": True,
                    "external": True,
                })
            self.message = "Run{} marked as already delivered.".format(
                " {}".format(index) if index - self.run_index == 1
                else "s {}-{}".format(self.run_index + 1, index))
        else:
            self.history = [done for done in self.history
                            if _count(done.get("run")) <= index]
            self.message = "Back at run {}.".format(number)
        self.run_index = index
        self._reset_run()
        return self._after_change(before, when)

    def reset(self) -> List[Effect]:
        self.running = False
        self.started_at = None
        self._reset_progress()
        self.message = "Progress reset." if self.plan is not None else ""
        return [Effect(SAVE), Effect(REDRAW)]

    def set_sell(self, system: str, station: str = "",
                 market_id: Optional[int] = None) -> List[Effect]:
        before = self.clipboard_text()
        if not self.complete:
            self.message = ""      # most likely "Set the sell system first."
        self.sell = SellLocation(system=(system or "").strip(), station=(station or "").strip(),
                                 market_id=market_id if station else None)
        effects = [Effect(REDRAW)]
        if self.running and self.clipboard_text() != before:
            effects.insert(0, Effect(CLIPBOARD, self.clipboard_text()))
        return effects

    # --- the journal --------------------------------------------------------

    def on_journal(self, entry: Dict[str, Any], system: Optional[str] = None,
                   station: Optional[str] = None,
                   now: Optional[datetime] = None) -> List[Effect]:
        event = entry.get("event")
        self._track_location(event, entry, system, station)

        run = self.current_run
        if not self.running or run is None:
            return []
        if event not in ("MarketBuy", "MarketSell", "ColonisationContribution"):
            return []
        if self.is_replay(parse_timestamp(entry.get("timestamp"))):
            return []

        when = _now(now)
        before = self._position()
        if event == "MarketBuy":
            changed = self._on_buy(run, entry)
        elif event == "ColonisationContribution":
            changed = self._at_sell(entry) and self._on_delivery(run, [
                (self._key(run, item.get("Name"), item.get("Name_Localised")),
                 _count(item.get("Amount")))
                for item in entry.get("Contributions") or () if isinstance(item, dict)], when)
        elif self._at_sell(entry):
            changed = self._on_delivery(run, [
                (self._key(run, entry.get("Type"), entry.get("Type_Localised")),
                 _count(entry.get("Count")))], when)
        else:
            changed = self._on_sell_back(run, entry)
        return self._after_change(before, when) if changed else []

    def _after_change(self, before: Tuple[int, Optional[Tuple[str, int]]],
                      now: datetime) -> List[Effect]:
        effects = [Effect(SAVE), Effect(REDRAW)]
        if self.complete:
            effects.append(Effect(FLASH, self._complete_lines(now)))
        elif self.running and self._position() != before:
            effects.insert(0, Effect(CLIPBOARD, self.clipboard_text()))
        return effects

    def _track_location(self, event: Any, entry: Dict[str, Any], system: Optional[str],
                        station: Optional[str]) -> None:
        if system:
            self.loc_system = system
        if event in ("Docked", "Location"):
            if entry.get("StarSystem"):
                self.loc_system = entry["StarSystem"]
            if event == "Docked" or entry.get("Docked"):
                self.loc_station = entry.get("StationName") or station or ""
                self.loc_market_id = entry.get("MarketID")
            else:
                self.loc_station, self.loc_market_id = "", None
        elif event in ("Undocked", "FSDJump", "SupercruiseEntry"):
            self.loc_station, self.loc_market_id = "", None
        else:
            # EDMC started while we were already docked: there was no Docked
            # event to read, but EDMC knows the station and market events carry
            # the id.
            if station and not self.loc_station:
                self.loc_station = station
            if self.loc_station and self.loc_market_id is None and entry.get("MarketID"):
                self.loc_market_id = entry["MarketID"]

    def _key(self, run: edha_plan.Run, symbol: Any, localised: Any) -> Optional[str]:
        """The run's commodity key for a journal commodity, or None if the run
        does not involve it. The symbol is tried first; the localised name is
        what lets a commodity missing from the bundled table still track."""
        planned = run.planned()
        for spelling in (symbol, localised):
            key = edha_commodities.lookup_key(spelling) if isinstance(spelling, str) else ""
            if key and key in planned:
                return key
        return None

    def _at_stop(self, stop: edha_plan.Stop) -> bool:
        if not _same(self.loc_station, stop.station):
            return False
        return not self.loc_system or _same(self.loc_system, stop.system)

    def _at_sell(self, entry: Dict[str, Any]) -> bool:
        if self.sell.station:
            market_id = entry.get("MarketID") or self.loc_market_id
            if self.sell.market_id is not None and market_id is not None:
                return market_id == self.sell.market_id
            return (_same(self.loc_station, self.sell.station)
                    and _same(self.loc_system, self.sell.system))
        return _same(self.loc_system, self.sell.system)

    def _stops_with(self, run: edha_plan.Run, key: str) -> List[int]:
        """Stops listing a commodity: the one we are docked at first, then the
        rest in plan order."""
        wanted = [index for index, stop in enumerate(run.stops)
                  if any(item.key == key for item in stop.items)]
        return sorted(wanted, key=lambda index: not self._at_stop(run.stops[index]))

    @staticmethod
    def _quantity(stop: edha_plan.Stop, key: str) -> int:
        return next((item.quantity for item in stop.items if item.key == key), 0)

    def _credit(self, run: edha_plan.Run, key: str, amount: int, order: List[int]) -> int:
        """Credit tonnes to the Stops wanting a commodity, in the order given.
        Returns how much was credited. Anything beyond what the run asks for is
        credited nowhere: the plan is the measure, not the hold."""
        left = amount
        for index in order:
            got = self.bought[index].get(key, 0)
            take = min(left, self._quantity(run.stops[index], key) - got)
            if take <= 0:
                continue
            self.bought[index][key] = got + take
            left -= take
            if not left:
                break
        return amount - left

    def _on_buy(self, run: edha_plan.Run, entry: Dict[str, Any]) -> bool:
        key = self._key(run, entry.get("Type"), entry.get("Type_Localised"))
        count = _count(entry.get("Count"))
        if key is None or count <= 0:
            return False
        return self._credit(run, key, count, self._stops_with(run, key)) > 0

    def _reconcile(self, run: edha_plan.Run,
                   cargo: Optional[Dict[str, int]]) -> List[Tuple[str, int]]:
        """Credit cargo that is in the hold but not in the ledger - bought before
        the plugin was in use, or while the session was stopped. Stops fill in
        plan order. Returns what was credited, as (name, tonnes).

        It only ever credits. A hold with LESS than the ledger expects could
        mean a delivery made while stopped or cargo sold elsewhere, and the hold
        cannot say which, so that case is left for Next run.
        """
        if not cargo:
            return []
        hold: Dict[str, int] = {}
        for spelling, amount in cargo.items():
            key = edha_commodities.lookup_key(spelling) if isinstance(spelling, str) else ""
            if key:
                hold[key] = hold.get(key, 0) + _count(amount)
        names = run.names()
        found = []
        for key in run.planned():
            unseen = hold.get(key, 0) - max(0, self._aboard(key))
            if unseen <= 0:
                continue
            in_plan_order = [index for index, stop in enumerate(run.stops)
                             if any(item.key == key for item in stop.items)]
            credited = self._credit(run, key, unseen, in_plan_order)
            if credited:
                found.append((names[key], credited))
        return found

    def _on_sell_back(self, run: edha_plan.Run, entry: Dict[str, Any]) -> bool:
        """A sale away from the Sell Location undoes a purchase - typically the
        wrong thing bought and sold straight back."""
        key = self._key(run, entry.get("Type"), entry.get("Type_Localised"))
        left = _count(entry.get("Count"))
        if key is None or left <= 0:
            return False
        order = self._stops_with(run, key)
        here = [index for index in order if self._at_stop(run.stops[index])]
        elsewhere = [index for index in reversed(order) if index not in here]
        undone = False
        for index in here + elsewhere:
            got = self.bought[index].get(key, 0)
            take = min(left, got)
            if take <= 0:
                continue
            self.bought[index][key] = got - take
            left -= take
            undone = True
            if not left:
                break
        return undone

    def _aboard(self, key: str) -> int:
        return sum(got.get(key, 0) for got in self.bought) - self.delivered.get(key, 0)

    def _on_delivery(self, run: edha_plan.Run, lots: List[Tuple[Optional[str], int]],
                     now: datetime) -> bool:
        # A delivery only means something once this run has cargo to deliver.
        # Without this the tail of the PREVIOUS run's unloading - a second
        # MarketSell arriving after the first completed it - would be booked
        # against the next run and could finish it before it had begun.
        ready = all(self.stop_closed(index) for index in range(len(run.stops)))
        if not ready and not any(any(got.values()) for got in self.bought):
            return False
        counted = False
        for key, amount in lots:
            if key is None or amount <= 0:
                continue
            self.delivered[key] = self.delivered.get(key, 0) + amount
            counted = True
        if not counted:
            return False
        self.delivered_any = True
        if all(self._aboard(key) <= 0 for key in run.planned()):
            self._complete_run(now)
        return True

    def _delivered_tonnes(self, run: edha_plan.Run) -> int:
        """Delivered against the plan: an over-delivery of one commodity does
        not make up for a shortfall in another."""
        return sum(min(self.delivered.get(key, 0), quantity)
                   for key, quantity in run.planned().items())

    def _complete_run(self, now: datetime, manual: bool = False) -> None:
        run = self.current_run
        names = run.names()
        live = self._live(now)
        shortfall = {names[key]: quantity - self.delivered.get(key, 0)
                     for key, quantity in run.planned().items()
                     if self.delivered.get(key, 0) < quantity}
        self.history.append({
            "run": self.run_index + 1,
            "seconds": round(self.run_seconds + live, 1),
            "planned": run.tonnes,
            "delivered": self._delivered_tonnes(run),
            "shortfall": shortfall,
            "manual": manual,
        })
        self.active_seconds += live
        if self.running:
            self.started_at = now      # the live segment now belongs to the next run
        self.run_index += 1
        self._reset_run()
        if self.complete:
            self.running = False
            self.started_at = None
            self.message = ""

    # --- statistics ---------------------------------------------------------

    def stats(self, now: Optional[datetime] = None) -> Stats:
        if self.plan is None:
            return Stats()
        live = self._live(_now(now))
        run = self.current_run
        delivered = sum(_count(done.get("delivered")) for done in self.history)
        if run is not None:
            delivered += self._delivered_tonnes(run)
        shortfall: Dict[str, int] = {}
        for done in self.history:
            for name, amount in (done.get("shortfall") or {}).items():
                shortfall[name] = shortfall.get(name, 0) + _count(amount)

        # A run closed by hand with nothing delivered took no time worth
        # averaging; counting it would make every remaining run look instant.
        timed = [float(done["seconds"]) for done in self.history
                 if _count(done.get("delivered")) > 0 and float(done.get("seconds") or 0) > 0]
        average = sum(timed) / len(timed) if timed else None
        active = self.active_seconds + live

        eta = None
        if average is not None and run is not None:
            remaining = len(self.plan.runs) - self.run_index
            eta = average * remaining - min(self.run_seconds + live, average)
        # Runs flown without the plugin count as delivered but took none of the
        # time on this clock, so they are kept out of the rate.
        tracked = delivered - sum(_count(done.get("delivered")) for done in self.history
                                  if done.get("external"))
        rate = tracked / (active / 3600.0) if active >= 60 and tracked > 0 else None
        return Stats(runs_done=min(self.run_index, len(self.plan.runs)),
                     runs_total=len(self.plan.runs), delivered=delivered,
                     planned=self.plan.tonnes, active_seconds=active,
                     avg_run_seconds=average, tonnes_per_hour=rate, eta_seconds=eta,
                     shortfall=shortfall)

    def stats_lines(self, now: Optional[datetime] = None) -> List[str]:
        if self.plan is None:
            return []
        stats = self.stats(now)
        lines = ["Runs {}/{}   {:,} / {:,} t   Active {}".format(
            stats.runs_done, stats.runs_total, stats.delivered, stats.planned,
            format_duration(stats.active_seconds))]
        pace = []
        if stats.avg_run_seconds is not None:
            pace.append("{} per run".format(format_duration(stats.avg_run_seconds)))
        if stats.tonnes_per_hour is not None:
            pace.append("{:,.0f} t/h".format(stats.tonnes_per_hour))
        if not self.complete:
            pace.append("ETA {}".format(format_duration(stats.eta_seconds)))
        if pace:
            lines.append("   ".join(pace))
        if stats.shortfall:
            lines.append("Shortfall: " + ", ".join(
                "{} {:,}".format(name, amount) for name, amount in sorted(stats.shortfall.items())))
        return lines

    # --- what to show -------------------------------------------------------

    def status_line(self) -> str:
        if self.plan is None:
            return "No plan"
        if self.complete:
            return "Complete"
        return "Running" if self.running else "Stopped"

    def _item_lines(self, index: int, stop: edha_plan.Stop) -> List[str]:
        got = self.bought[index]
        parts = []
        for item in stop.items:
            have = got.get(item.key, 0)
            parts.append("{} {:,}".format(item.name, item.quantity) if have >= item.quantity
                         else "{} {:,}/{:,}".format(item.name, have, item.quantity))
        return self._wrap_items(parts)

    @staticmethod
    def _wrap_items(parts: List[str]) -> List[str]:
        lines: List[str] = []
        current = ""
        for part in parts:
            if current and len(current) + 3 + len(part) > ITEM_LINE_WIDTH:
                lines.append("      " + current)
                current = part
            else:
                current = "{}   {}".format(current, part) if current else part
        if current:
            lines.append("      " + current)
        return lines

    def _complete_lines(self, now: Optional[datetime] = None) -> List[str]:
        stats = self.stats(now)
        lines = ["HAUL  Plan complete   {0}/{0} runs".format(stats.runs_total),
                 "Delivered {:,} of {:,} t in {}".format(
                     stats.delivered, stats.planned, format_duration(stats.active_seconds))]
        if stats.shortfall:
            lines.append("Shortfall: " + ", ".join(
                "{} {:,}".format(name, amount) for name, amount in sorted(stats.shortfall.items())))
        return lines

    def display_lines(self, now: Optional[datetime] = None) -> Tuple[List[str], str]:
        """The block for the overlay and the panel, and the role that colours it.
        The role is OFF whenever nothing may be drawn in the game."""
        if self.plan is None:
            return [], OFF
        if self.complete:
            return self._complete_lines(now), OFF
        run = self.current_run
        target = self.target()
        stats = self.stats(now)
        lines = ["HAUL  Run {}/{}   ETA {}".format(
            self.run_index + 1, len(self.plan.runs), format_duration(stats.eta_seconds))]
        for index, stop in enumerate(run.stops):
            if self.stop_done(index):
                marker = "[x]"
            elif target == (STOP_KIND, index):
                marker = ">> "
            elif index in self.skipped:
                marker = "[-]"
            else:
                marker = "[ ]"
            lines.append("{} {} / {}".format(marker, stop.system, stop.station))
            lines.extend(self._item_lines(index, stop))

        selling = target[0] == SELL_KIND
        if selling:
            lines.append(">>  SELL -> {}".format(self.sell.label()))
            if self.delivered_any:
                aboard = sum(max(0, self._aboard(key)) for key in run.planned())
                lines.append("      Delivered {:,} t   {:,} t still aboard".format(
                    sum(self.delivered.values()), aboard))
        else:
            lines.append("then SELL -> {}".format(self.sell.label()))
        if not self.running:
            return lines, OFF
        return lines, SELL if selling else BUY

    # --- the whole plan -----------------------------------------------------

    @staticmethod
    def _verdict(done: Optional[Dict[str, Any]]) -> str:
        """How a finished run ended, from its history record."""
        if done is None:
            return "done"
        if done.get("external"):
            return "delivered before joining"
        text = "closed by hand" if done.get("manual") else "done in {}".format(
            format_duration(float(done.get("seconds") or 0)))
        shortfall = done.get("shortfall") or {}
        if shortfall:
            text += "   short: " + ", ".join(
                "{} {:,}".format(name, _count(amount)) for name, amount in sorted(shortfall.items()))
        return text

    def overview(self, now: Optional[datetime] = None) -> List[Tuple[str, str]]:
        """Every run and stop in the plan, for the pop-out window: (line, role)
        pairs, the role being NOTE, DONE, CURRENT or TODO. The current run is
        shown exactly as the overlay shows it; the others as planned."""
        if self.plan is None:
            return []
        total = len(self.plan.runs)
        rows: List[Tuple[str, str]] = [(self.plan.name or "Haul plan", NOTE),
                                       ("Sell at {}".format(self.sell.label()), NOTE)]
        rows.extend((line, NOTE) for line in self.stats_lines(now))
        records = {_count(done.get("run")): done for done in self.history}
        for index, run in enumerate(self.plan.runs):
            rows.append(("", NOTE))
            head = "Run {}/{}".format(index + 1, total)
            if index == self.run_index:
                rows.append(("{}   {}".format(head, "in progress" if self.running else "next"),
                             CURRENT))
                rows.extend((line, CURRENT) for line in self.display_lines(now)[0][1:])
                continue
            done = index < self.run_index
            role = DONE if done else TODO
            rows.append(("{}   {}".format(head, self._verdict(records.get(index + 1)) if done
                                          else "{:,} t".format(run.tonnes)), role))
            for stop in run.stops:
                rows.append(("    {} / {}".format(stop.system, stop.station), role))
                rows.extend((line, role) for line in self._wrap_items(
                    ["{} {:,}".format(item.name, item.quantity) for item in stop.items]))
        return rows

    # --- persistence --------------------------------------------------------

    def to_dict(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Everything needed to resume. The live segment is folded in, because
        whoever reads this back starts stopped."""
        live = self._live(_now(now))
        return {
            "version": STATE_VERSION,
            "plan": self.plan.raw if self.plan is not None else None,
            "run_index": self.run_index,
            "bought": [dict(got) for got in self.bought],
            "skipped": sorted(self.skipped),
            "delivered": dict(self.delivered),
            "delivered_any": self.delivered_any,
            "history": list(self.history),
            "active_seconds": round(self.active_seconds + live, 1),
            "run_seconds": round(self.run_seconds + live, 1),
        }

    def restore(self, data: Optional[Dict[str, Any]]) -> bool:
        """Resume from to_dict() output. Always comes back stopped. Anything
        that does not fit the plan is dropped rather than trusted."""
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            return False
        if data.get("plan") is None:
            return False
        try:
            plan, warnings = edha_plan.parse(data["plan"])
        except edha_plan.PlanError as exc:
            self.message = "Saved plan could not be read: {}".format(exc)
            return False

        self.running = False
        self.started_at = None
        self.plan = plan
        self.warnings = warnings
        self._reset_progress()
        self.run_index = max(0, min(_count(data.get("run_index")), len(plan.runs)))
        self._reset_run()
        history = data.get("history")
        self.history = [done for done in history if isinstance(done, dict)] \
            if isinstance(history, list) else []
        try:
            self.active_seconds = max(0.0, float(data.get("active_seconds") or 0))
            self.run_seconds = max(0.0, float(data.get("run_seconds") or 0))
        except (TypeError, ValueError):
            self.active_seconds = self.run_seconds = 0.0

        run = self.current_run
        if run is None:
            return True
        bought = data.get("bought")
        if isinstance(bought, list) and len(bought) == len(run.stops):
            for index, (got, stop) in enumerate(zip(bought, run.stops)):
                if isinstance(got, dict):
                    self.bought[index] = {
                        item.key: min(_count(got.get(item.key)), item.quantity)
                        for item in stop.items if _count(got.get(item.key)) > 0}
        skipped = data.get("skipped")
        if isinstance(skipped, list):
            self.skipped = {index for index in skipped
                            if isinstance(index, int) and 0 <= index < len(run.stops)}
        delivered = data.get("delivered")
        if isinstance(delivered, dict):
            planned = run.planned()
            self.delivered = {key: _count(amount) for key, amount in delivered.items()
                              if key in planned and _count(amount) > 0}
        self.delivered_any = bool(data.get("delivered_any")) and bool(self.delivered)
        return True


def _self_test() -> None:
    plan, _ = edha_plan.parse({"schema": 1, "runs": [
        {"stops": [{"system": "Sol", "station": "Daedalus",
                    "commodities": [{"name": "Steel", "quantity": 400}]}]},
        {"stops": [{"system": "Sol", "station": "Daedalus",
                    "commodities": [{"name": "Steel", "quantity": 100}]}]}]})
    start = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)

    def at(minutes):
        return start + timedelta(minutes=minutes)

    session = Session()
    session.load_plan(plan)
    session.set_sell("HIP 1234")
    ok, effects = session.start(now=start)
    assert ok and effects[0] == Effect(CLIPBOARD, "Sol"), effects

    session.on_journal({"event": "Docked", "StationName": "Daedalus", "StarSystem": "Sol",
                        "MarketID": 1}, system="Sol", station="Daedalus", now=at(5))
    effects = session.on_journal({"event": "MarketBuy", "Type": "steel", "Count": 400,
                                  "timestamp": "2026-10-11T12:06:00Z"}, system="Sol", now=at(6))
    assert effects[0] == Effect(CLIPBOARD, "HIP 1234"), effects
    assert session.display_lines(now=at(6))[1] == SELL

    session.on_journal({"event": "Docked", "StationName": "Depot", "StarSystem": "HIP 1234",
                        "MarketID": 2}, system="HIP 1234", station="Depot", now=at(19))
    effects = session.on_journal({"event": "ColonisationContribution", "MarketID": 2,
                                  "timestamp": "2026-10-11T12:20:00Z", "Contributions": [
                                      {"Name": "$Steel_name;", "Amount": 400}]},
                                 system="HIP 1234", now=at(20))
    assert session.run_index == 1 and effects[0] == Effect(CLIPBOARD, "Sol"), effects
    stats = session.stats(now=at(20))
    assert stats.delivered == 400 and stats.avg_run_seconds == 1200.0
    assert stats.eta_seconds == 1200.0, stats

    resumed = Session()
    assert resumed.restore(session.to_dict(now=at(25)))
    assert resumed.run_index == 1 and not resumed.running
    assert resumed.run_seconds == 300.0 and resumed.active_seconds == 1500.0
    print("edha_session self-test: OK")


if __name__ == "__main__":
    _self_test()
