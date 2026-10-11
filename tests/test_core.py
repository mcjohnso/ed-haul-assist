"""Pure-logic tests for EDHaulAssist. No EDMC, no tkinter, no network.

Run with:   python tests/test_core.py

Every time-dependent call is given an explicit `now`, so nothing here depends on
the wall clock. Journal entries are shaped like the real ones: `MarketBuy` writes
the commodity as a lowercase symbol in `Type`, `ColonisationContribution` writes
it as `$Symbol_name;` in `Contributions[].Name`.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "EDHaulAssist"))

import edha_commodities                 # noqa: E402
import edha_overlay                     # noqa: E402
import edha_plan                        # noqa: E402
import edha_session as session_mod      # noqa: E402
import edha_store                       # noqa: E402

START = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
SITE_MARKET = 4385925635        # a real MarketID: note it does not fit in 32 bits


def at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def stamp(minutes: float) -> str:
    return at(minutes).strftime("%Y-%m-%dT%H:%M:%SZ")


def stop(system, station, **commodities):
    return {"system": system, "station": station, "commodities": [
        {"name": name.replace("_", " "), "quantity": quantity}
        for name, quantity in commodities.items()]}


def document(*runs, name="Test haul"):
    return {"schema": 1, "name": name, "runs": [{"stops": list(stops)} for stops in runs]}


TWO_RUNS = document(
    [stop("Alpha", "Able Port", Steel=400, Titanium=320),
     stop("Beta", "Baker Hub", Aluminium=200)],
    [stop("Alpha", "Able Port", Steel=100)],
)


def kinds(effects):
    return [effect.kind for effect in effects]


def copied(effects):
    return [effect.payload for effect in effects if effect.kind == session_mod.CLIPBOARD]


class Haul:
    """A session plus the handful of journal events a haul is made of."""

    def __init__(self, doc=TWO_RUNS, sell=("Home", "", None), start=True, cargo=None):
        plan, self.warnings = edha_plan.parse(doc)
        self.session = session_mod.Session()
        self.session.load_plan(plan, self.warnings)
        self.session.set_sell(*sell)
        self.clock = 0.0
        self.start_effects = self.session.start(now=at(0), cargo=cargo)[1] if start else []

    def tick(self, minutes=1.0):
        self.clock += minutes
        return self.clock

    def dock(self, system, station, market_id=None):
        self.tick()
        return self.session.on_journal(
            {"event": "Docked", "timestamp": stamp(self.clock), "StarSystem": system,
             "StationName": station, "MarketID": market_id},
            system=system, station=station, now=at(self.clock))

    def undock(self):
        self.tick()
        return self.session.on_journal({"event": "Undocked", "timestamp": stamp(self.clock)},
                                       system=self.session.loc_system, now=at(self.clock))

    def buy(self, symbol, count, localised=None, when=None):
        self.tick()
        entry = {"event": "MarketBuy", "timestamp": stamp(self.clock if when is None else when),
                 "Type": symbol, "Count": count}
        if localised:
            entry["Type_Localised"] = localised
        return self.session.on_journal(entry, system=self.session.loc_system,
                                       station=self.session.loc_station or None,
                                       now=at(self.clock))

    def sell(self, symbol, count):
        self.tick()
        return self.session.on_journal(
            {"event": "MarketSell", "timestamp": stamp(self.clock), "Type": symbol,
             "Count": count},
            system=self.session.loc_system, station=self.session.loc_station or None,
            now=at(self.clock))

    def contribute(self, market_id=None, **amounts):
        self.tick()
        entry = {"event": "ColonisationContribution", "timestamp": stamp(self.clock),
                 "Contributions": [{"Name": "${}_name;".format(name), "Amount": amount}
                                   for name, amount in amounts.items()]}
        if market_id is not None:
            entry["MarketID"] = market_id
        return self.session.on_journal(entry, system=self.session.loc_system,
                                       station=self.session.loc_station or None,
                                       now=at(self.clock))

    def lines(self):
        return self.session.display_lines(now=at(self.clock))

    def run_one(self):
        """Fly the first run of TWO_RUNS to completion."""
        self.dock("Alpha", "Able Port")
        self.buy("steel", 400)
        self.buy("titanium", 320)
        self.dock("Beta", "Baker Hub")
        self.buy("aluminium", 200)
        self.dock("Home", "Depot")
        return self.contribute(Steel=400, Titanium=320, Aluminium=200)


class SelfTests(unittest.TestCase):
    """Each module carries its own self-test; run them here too so one command
    covers everything."""

    def test_module_self_tests(self):
        for module in (edha_commodities, edha_overlay, edha_plan, edha_store, session_mod):
            with self.subTest(module=module.__name__):
                module._self_test()


class FrozenRuntimeImports(unittest.TestCase):
    """EDMC ships a FROZEN Python. Its library.zip carries only the stdlib
    modules EDMC itself uses - `difflib`, for one, is absent - and a plugin that
    imports a missing module fails to load outright, with the error visible only
    in EDMC's debug log. "Standard library only" is necessary but NOT sufficient.

    This walks every import in the plugin and checks it against what the frozen
    runtime really provides. Adding an import that is not on the list is a
    deliberate decision that has to be verified against a real EDMC install.
    """

    #: Verified present in EDMC 6.x's library.zip, or a C builtin compiled into
    #: python313.dll (math, time, itertools and friends are not .pyc entries).
    STDLIB_IN_EDMC = {
        "__future__", "dataclasses", "datetime", "json", "logging", "os", "re",
        "tempfile", "tkinter", "typing",
    }
    #: Provided by EDMC at runtime, or by the user's overlay plugin.
    EDMC_PROVIDED = {"config", "myNotebook", "theme", "monitor", "edmcoverlay"}

    def plugin_sources(self):
        return sorted((ROOT / "EDHaulAssist").glob("*.py"))

    def imported_modules(self, path):
        import ast
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
        return found

    def test_every_import_exists_in_the_frozen_runtime(self):
        allowed = self.STDLIB_IN_EDMC | self.EDMC_PROVIDED
        for path in self.plugin_sources():
            for module in sorted(self.imported_modules(path)):
                if module.startswith("edha_"):
                    continue                      # our own, shipped alongside
                with self.subTest(file=path.name, module=module):
                    self.assertIn(module, allowed,
                                  "{} imports {!r}, which is not known to exist in "
                                  "EDMC's frozen Python. Verify against a real "
                                  "install before allowing it.".format(path.name, module))

    def test_against_a_real_edmc_install_when_one_is_present(self):
        """Opportunistic: if EDMC is installed here, check the pure-Python
        imports against its actual library.zip rather than against the list."""
        import zipfile
        candidates = [Path(r"C:/Program Files (x86)/EDMarketConnector/library.zip"),
                      Path(r"C:/Program Files/EDMarketConnector/library.zip")]
        library = next((p for p in candidates if p.exists()), None)
        if library is None:
            self.skipTest("no local EDMC install to check against")

        with zipfile.ZipFile(library) as archive:
            names = set(archive.namelist())

        def present(module):
            return ("{}.pyc".format(module) in names
                    or "{}/__init__.pyc".format(module) in names)

        for path in self.plugin_sources():
            for module in sorted(self.imported_modules(path)):
                if module.startswith("edha_") or module in self.EDMC_PROVIDED:
                    continue
                with self.subTest(file=path.name, module=module):
                    self.assertTrue(present(module),
                                    "{} imports {!r}, absent from {}".format(
                                        path.name, module, library))


class PlanParsing(unittest.TestCase):
    def test_the_shipped_example_loads_clean(self):
        plan, warnings = edha_plan.load_file(str(ROOT / "examples" / "sample_plan.json"))
        self.assertEqual(warnings, [])
        self.assertEqual(len(plan.runs), 3)
        self.assertEqual(plan.tonnes, 824 + 474 + 698 + 400 + 200 + 60 + 540)

    def test_display_names_resolve_to_journal_symbols(self):
        # The whole reason the table is bundled: these two do not share a word.
        plan, warnings = edha_plan.parse(document(
            [stop("A", "B", Land_Enrichment_Systems=5, CMM_Composite=7)]))
        self.assertEqual(warnings, [])
        self.assertEqual([item.key for item in plan.runs[0].stops[0].items],
                         ["terrainenrichmentsystems", "cmmcomposite"])

    def test_an_unknown_commodity_warns_but_still_loads(self):
        plan, warnings = edha_plan.parse(document([stop("A", "B", Titanum=5)]))
        self.assertEqual(len(warnings), 1)
        self.assertIn("Run 1 stop 1", warnings[0])
        self.assertIn("Titanium", warnings[0])
        self.assertEqual(plan.runs[0].tonnes, 5)

    def test_what_is_rejected_says_where(self):
        cases = {
            "schema": dict(document([stop("A", "B", Steel=1)]), schema=2),
            "no runs": {"schema": 1, "runs": []},
            "Run 2: no stops": document([stop("A", "B", Steel=1)], []),
            "Run 1 stop 1: the station": document([stop("A", " ", Steel=1)]),
            "Run 1 stop 2: no commodities": document([stop("A", "B", Steel=1),
                                                      stop("A", "C")]),
            "quantity must be a whole number": document([stop("A", "B", Steel=1.5)]),
            "quantity must be a number": document([stop("A", "B", Steel="lots")]),
        }
        cases["quantity must be a number "] = document([stop("A", "B", Steel=True)])
        for expected, doc in cases.items():
            with self.subTest(expected=expected):
                with self.assertRaises(edha_plan.PlanError) as caught:
                    edha_plan.parse(doc)
                self.assertIn(expected.strip(), str(caught.exception))

    def test_a_file_that_is_not_json_is_a_plan_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text("{ nope", encoding="utf-8")
            with self.assertRaises(edha_plan.PlanError):
                edha_plan.load_file(str(path))
            with self.assertRaises(edha_plan.PlanError):
                edha_plan.load_file(str(Path(directory) / "absent.json"))

    def test_a_bom_does_not_matter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(TWO_RUNS), encoding="utf-8-sig")
            plan, _ = edha_plan.load_file(str(path))
            self.assertEqual(len(plan.runs), 2)


class StartAndStop(unittest.TestCase):
    def test_start_needs_a_plan_and_a_sell_system(self):
        session = session_mod.Session()
        ok, _ = session.start(now=at(0))
        self.assertFalse(ok)
        self.assertIn("plan", session.message)

        haul = Haul(sell=("", "", None), start=False)
        ok, effects = haul.session.start(now=at(0))
        self.assertFalse(ok)
        self.assertFalse(haul.session.running)
        self.assertIn("sell system", haul.session.message)
        self.assertEqual(copied(effects), [])

    def test_start_copies_the_first_stop(self):
        haul = Haul()
        self.assertEqual(copied(haul.start_effects), ["Alpha"])
        self.assertEqual(haul.lines()[1], session_mod.BUY)

    def test_nothing_is_tracked_or_copied_while_stopped(self):
        haul = Haul(start=False)
        haul.dock("Alpha", "Able Port")
        self.assertEqual(haul.buy("steel", 400), [])
        self.assertEqual(haul.session.bought, [{}, {}])
        lines, role = haul.lines()
        self.assertEqual(role, session_mod.OFF)      # the overlay stays blank
        self.assertTrue(lines)                       # but the panel still shows the run

    def test_location_is_tracked_while_stopped(self):
        # "Here" has to work before Start has ever been pressed.
        haul = Haul(start=False)
        haul.dock("Home", "Depot", SITE_MARKET)
        self.assertEqual((haul.session.loc_system, haul.session.loc_station,
                          haul.session.loc_market_id), ("Home", "Depot", SITE_MARKET))
        haul.undock()
        self.assertEqual(haul.session.loc_station, "")

    def test_stopping_freezes_the_clock(self):
        haul = Haul()
        haul.session.stop(now=at(10))
        self.assertEqual(haul.session.stats(now=at(500)).active_seconds, 600)
        haul.session.start(now=at(500))
        self.assertEqual(haul.session.stats(now=at(505)).active_seconds, 900)

    def test_a_replayed_purchase_is_ignored(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        self.assertEqual(haul.buy("steel", 400, when=-40), [])
        self.assertEqual(haul.session.bought[0], {})
        self.assertTrue(haul.buy("steel", 400))


class StopTracking(unittest.TestCase):
    def test_a_stop_completes_only_when_every_commodity_is_bought(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        effects = haul.buy("steel", 400)
        self.assertEqual(copied(effects), [])               # still titanium to buy here
        self.assertEqual(kinds(effects), [session_mod.SAVE, session_mod.REDRAW])
        self.assertEqual(haul.session.target(), (session_mod.STOP_KIND, 0))
        effects = haul.buy("titanium", 320)
        self.assertEqual(copied(effects), ["Beta"])
        self.assertEqual(haul.session.target(), (session_mod.STOP_KIND, 1))

    def test_purchases_accumulate_across_several_buys(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 150)
        self.assertFalse(haul.session.stop_done(0))
        self.assertIn("Steel 150/400", "\n".join(haul.lines()[0]))
        haul.buy("steel", 250)
        haul.buy("titanium", 320)
        self.assertTrue(haul.session.stop_done(0))

    def test_stops_can_be_done_out_of_order(self):
        haul = Haul()
        haul.dock("Beta", "Baker Hub")
        effects = haul.buy("aluminium", 200)
        self.assertTrue(haul.session.stop_done(1))
        self.assertEqual(copied(effects), [])               # the target is still stop 1
        self.assertEqual(haul.session.target(), (session_mod.STOP_KIND, 0))

    def test_a_commodity_the_run_does_not_want_is_ignored(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        self.assertEqual(haul.buy("gold", 50), [])

    def test_a_purchase_at_an_unplanned_station_still_counts(self):
        haul = Haul()
        haul.dock("Gamma", "Somewhere Else")
        haul.buy("aluminium", 200)
        self.assertTrue(haul.session.stop_done(1))

    def test_the_docked_station_is_credited_before_plan_order(self):
        doc = document([stop("Alpha", "Able Port", Steel=400),
                        stop("Beta", "Baker Hub", Steel=300)])
        haul = Haul(doc)
        haul.dock("Beta", "Baker Hub")
        haul.buy("steel", 300)
        self.assertEqual(haul.session.bought, [{}, {"steel": 300}])

    def test_overbuying_spills_to_another_stop_then_is_dropped(self):
        doc = document([stop("Alpha", "Able Port", Steel=400),
                        stop("Beta", "Baker Hub", Steel=300)])
        haul = Haul(doc)
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 1000)
        self.assertEqual(haul.session.bought, [{"steel": 400}, {"steel": 300}])
        self.assertEqual(haul.session.target(), (session_mod.SELL_KIND, -1))

    def test_selling_back_undoes_the_purchase(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.buy("titanium", 320)
        effects = haul.sell("titanium", 20)
        self.assertEqual(haul.session.bought[0]["titanium"], 300)
        self.assertEqual(copied(effects), ["Alpha"])        # the stop reopened
        self.assertEqual(haul.session.delivered, {})

    def test_a_commodity_missing_from_the_table_tracks_by_its_localised_name(self):
        haul = Haul(document([stop("A", "B", Quantum_Widgets=10)]))
        self.assertEqual(len(haul.warnings), 1)
        haul.dock("A", "B")
        haul.buy("qwidget", 10, localised="Quantum Widgets")
        self.assertTrue(haul.session.stop_done(0))

    def test_skip_moves_guidance_on_and_keeps_what_was_bought(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 312)
        effects = haul.session.skip_stop(now=at(5))
        self.assertEqual(copied(effects), ["Beta"])
        self.assertEqual(haul.session.bought[0], {"steel": 312})
        self.assertTrue(haul.lines()[0][1].startswith("[-]"))
        # A later purchase can still finish a skipped stop.
        haul.buy("steel", 88)
        haul.buy("titanium", 320)
        self.assertTrue(haul.lines()[0][1].startswith("[x]"))


class RunTracking(unittest.TestCase):
    def test_a_colony_contribution_completes_the_run(self):
        haul = Haul()
        effects = haul.run_one()
        self.assertEqual(haul.session.run_index, 1)
        self.assertEqual(copied(effects), ["Alpha"])        # run 2's first stop
        self.assertEqual(haul.session.history[0]["delivered"], 920)
        self.assertEqual(haul.session.history[0]["shortfall"], {})
        self.assertEqual(haul.session.bought, [{}])

    def test_market_sales_complete_the_run_when_the_last_lot_goes(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.buy("titanium", 320)
        haul.dock("Beta", "Baker Hub")
        effects = haul.buy("aluminium", 200)
        self.assertEqual(copied(effects), ["Home"])
        self.assertEqual(haul.lines()[1], session_mod.SELL)
        haul.dock("Home", "Depot")
        haul.sell("steel", 400)
        haul.sell("titanium", 320)
        self.assertEqual(haul.session.run_index, 0)
        self.assertIn("200 t still aboard", "\n".join(haul.lines()[0]))
        haul.sell("aluminium", 200)
        self.assertEqual(haul.session.run_index, 1)

    def test_a_sale_elsewhere_is_not_a_delivery(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.dock("Nowhere", "Wrong Place")
        haul.sell("steel", 400)
        self.assertEqual(haul.session.delivered, {})
        self.assertEqual(haul.session.run_index, 0)

    def test_a_contribution_elsewhere_is_not_a_delivery(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.dock("Nowhere", "Another Site")
        self.assertEqual(haul.contribute(Steel=400), [])

    def test_with_a_station_set_only_that_station_counts(self):
        haul = Haul(sell=("Home", "Depot", SITE_MARKET))
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.dock("Home", "Other Outpost", 777)
        self.assertEqual(haul.contribute(market_id=777, Steel=400), [])
        haul.dock("Home", "Depot", SITE_MARKET)
        self.assertTrue(haul.contribute(market_id=SITE_MARKET, Steel=400))
        self.assertEqual(haul.session.history[0]["delivered"], 400)
        self.assertIn("SELL -> Home / Depot", "\n".join(haul.lines()[0]))

    def test_the_station_matches_by_name_when_no_market_id_was_captured(self):
        haul = Haul(sell=("Home", "Depot", None))
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.dock("Home", "depot")
        self.assertTrue(haul.contribute(Steel=400))

    def test_delivering_early_ends_the_run_and_records_the_shortfall(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.buy("titanium", 300)              # the market ran dry 20 t short
        haul.dock("Home", "Depot")
        haul.contribute(Steel=400, Titanium=300)
        self.assertEqual(haul.session.run_index, 1)
        done = haul.session.history[0]
        self.assertEqual(done["delivered"], 700)
        self.assertEqual(done["shortfall"], {"Titanium": 20, "Aluminium": 200})
        self.assertIn("Shortfall: Aluminium 200, Titanium 20",
                      haul.session.stats_lines(now=at(haul.clock)))

    def test_the_tail_of_one_unloading_does_not_start_the_next_run(self):
        # Run 1 completes on the first sale because that is all the ledger knew
        # was aboard; a second sale of a commodity run 2 also wants must not be
        # booked against run 2, let alone complete it.
        doc = document([stop("Alpha", "Able Port", Steel=400)],
                       [stop("Alpha", "Able Port", Steel=100, Titanium=50)])
        haul = Haul(doc)
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.dock("Home", "Depot")
        haul.sell("steel", 400)
        self.assertEqual(haul.session.run_index, 1)
        self.assertEqual(haul.sell("steel", 30), [])
        self.assertEqual(haul.sell("titanium", 50), [])
        self.assertEqual(haul.session.run_index, 1)
        self.assertEqual(haul.session.delivered, {})

    def test_cargo_bought_before_start_delivers_once_the_stops_are_skipped(self):
        haul = Haul(document([stop("Alpha", "Able Port", Steel=400)],
                             [stop("Alpha", "Able Port", Steel=100)]))
        haul.session.skip_stop(now=at(1))
        haul.dock("Home", "Depot")
        haul.contribute(Steel=400)
        self.assertEqual(haul.session.run_index, 1)
        self.assertEqual(haul.session.history[0]["shortfall"], {})

    def test_next_run_closes_a_run_the_site_would_not_fully_take(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.buy("titanium", 320)
        haul.dock("Beta", "Baker Hub")
        haul.buy("aluminium", 200)
        haul.dock("Home", "Depot")
        haul.contribute(Steel=400, Titanium=320, Aluminium=150)
        self.assertEqual(haul.session.run_index, 0)         # 50 t still aboard
        effects = haul.session.next_run(now=at(haul.tick()))
        self.assertEqual(haul.session.run_index, 1)
        self.assertEqual(copied(effects), ["Alpha"])
        self.assertTrue(haul.session.history[0]["manual"])
        self.assertEqual(haul.session.history[0]["shortfall"], {"Aluminium": 50})

    def test_next_run_copies_even_when_the_next_target_looks_the_same(self):
        # Run 1 stop 0 and run 2 stop 0 are the same (kind, index) pair.
        haul = Haul()
        effects = haul.session.next_run(now=at(1))
        self.assertEqual(copied(effects), ["Alpha"])

    def test_the_last_run_completes_the_plan_and_stops_the_session(self):
        haul = Haul()
        haul.run_one()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 100)
        haul.dock("Home", "Depot")
        effects = haul.contribute(Steel=100)
        self.assertTrue(haul.session.complete)
        self.assertFalse(haul.session.running)
        self.assertEqual(kinds(effects),
                         [session_mod.SAVE, session_mod.REDRAW, session_mod.FLASH])
        lines, role = haul.lines()
        self.assertEqual(role, session_mod.OFF)
        self.assertIn("Plan complete", lines[0])
        self.assertIn("1,020 of 1,020 t", lines[1])
        self.assertEqual(haul.session.status_line(), "Complete")
        ok, _ = haul.session.start(now=at(99))
        self.assertFalse(ok)


THREE_RUNS = document(
    [stop("Alpha", "Able Port", Steel=400)],
    [stop("Alpha", "Able Port", Steel=300)],
    [stop("Beta", "Baker Hub", Aluminium=200)],
)


class JoiningPartWay(unittest.TestCase):
    """Picking a plan up after some of it has already been flown."""

    def test_runs_jumped_past_count_as_delivered_not_as_shortfall(self):
        haul = Haul(THREE_RUNS, start=False)
        effects = haul.session.goto_run(3, now=at(0))
        self.assertEqual(kinds(effects), [session_mod.SAVE, session_mod.REDRAW])
        self.assertEqual(haul.session.run_index, 2)
        self.assertEqual([done["run"] for done in haul.session.history], [1, 2])
        self.assertTrue(all(done["external"] for done in haul.session.history))
        self.assertIn("Runs 1-2", haul.session.message)
        stats = haul.session.stats(now=at(0))
        self.assertEqual((stats.runs_done, stats.delivered, stats.shortfall), (2, 700, {}))
        self.assertIn("Run 3/3", haul.lines()[0][0])

    def test_they_feed_neither_the_average_nor_the_rate(self):
        haul = Haul(THREE_RUNS)
        haul.session.goto_run(2, now=at(0))
        stats = haul.session.stats(now=at(30))
        self.assertIsNone(stats.avg_run_seconds)
        self.assertIsNone(stats.eta_seconds)
        self.assertIsNone(stats.tonnes_per_hour)       # 400 t "delivered", none of it timed

        haul.clock = 30
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 300)
        haul.dock("Home", "Depot")
        haul.contribute(Steel=300)                     # run 2 done at 34 minutes
        stats = haul.session.stats(now=at(haul.clock))
        self.assertEqual(stats.delivered, 700)
        self.assertEqual(stats.avg_run_seconds, haul.clock * 60)
        self.assertAlmostEqual(stats.tonnes_per_hour, 300 / (haul.clock / 60.0))

    def test_jumping_while_running_copies_the_new_target(self):
        haul = Haul(THREE_RUNS)
        self.assertEqual(copied(haul.session.goto_run(3, now=at(1))), ["Beta"])
        self.assertEqual(haul.session.stats(now=at(1)).active_seconds, 60)

    def test_going_back_discards_what_was_recorded_from_there_on(self):
        haul = Haul(THREE_RUNS)
        haul.session.goto_run(3, now=at(1))
        haul.dock("Beta", "Baker Hub")
        haul.buy("aluminium", 50)
        haul.session.goto_run(2, now=at(5))
        self.assertEqual(haul.session.run_index, 1)
        self.assertEqual([done["run"] for done in haul.session.history], [1])
        self.assertEqual(haul.session.bought, [{}])
        self.assertEqual(haul.session.message, "Back at run 2.")

    def test_a_finished_plan_can_be_reopened(self):
        haul = Haul(THREE_RUNS)
        haul.session.goto_run(3, now=at(1))
        haul.session.next_run(now=at(2))
        self.assertTrue(haul.session.complete)
        haul.session.goto_run(3, now=at(3))
        self.assertFalse(haul.session.complete)
        self.assertEqual(len(haul.session.history), 2)

    def test_the_current_run_and_nonsense_are_no_ops(self):
        haul = Haul(THREE_RUNS)
        for number in (1, 0, 4, -2):
            with self.subTest(number=number):
                self.assertEqual(haul.session.goto_run(number, now=at(1)), [])
        self.assertEqual(haul.session.history, [])
        self.assertEqual(session_mod.Session().goto_run(1), [])

    def test_a_jump_survives_a_restart(self):
        haul = Haul(THREE_RUNS)
        haul.session.goto_run(3, now=at(1))
        resumed = session_mod.Session()
        self.assertTrue(resumed.restore(haul.session.to_dict(now=at(2))))
        self.assertEqual(resumed.run_index, 2)
        self.assertIsNone(resumed.stats(now=at(2)).tonnes_per_hour)


class CargoAlreadyAboard(unittest.TestCase):
    """Start reconciles the ledger with the hold."""

    def test_stops_already_bought_are_ticked_off_at_start(self):
        # EDMC reports the hold keyed by lowercase journal symbol.
        haul = Haul(cargo={"steel": 400, "titanium": 320, "drones": 8})
        self.assertEqual(haul.session.bought, [{"steel": 400, "titanium": 320}, {}])
        self.assertEqual(copied(haul.start_effects), ["Beta"])       # not Alpha
        self.assertEqual(kinds(haul.start_effects)[0], session_mod.SAVE)
        self.assertEqual(haul.session.message, "Already aboard: Steel 400, Titanium 320.")
        self.assertTrue(haul.lines()[0][1].startswith("[x]"))

    def test_a_full_hold_goes_straight_to_the_sell_location(self):
        haul = Haul(cargo={"steel": 400, "titanium": 320, "aluminium": 200})
        self.assertEqual(copied(haul.start_effects), ["Home"])
        self.assertEqual(haul.lines()[1], session_mod.SELL)

    def test_market_sales_then_complete_the_run_without_false_shortfall(self):
        # The case Skip stop gets wrong: with nothing in the ledger the first
        # MarketSell would close the run and the rest would read as shortfall.
        haul = Haul(cargo={"steel": 400, "titanium": 320, "aluminium": 200})
        haul.dock("Home", "Depot")
        haul.sell("steel", 400)
        haul.sell("titanium", 320)
        self.assertEqual(haul.session.run_index, 0)
        haul.sell("aluminium", 200)
        self.assertEqual(haul.session.run_index, 1)
        self.assertEqual(haul.session.history[0]["shortfall"], {})
        self.assertEqual(haul.session.history[0]["delivered"], 920)

    def test_a_part_bought_stop_shows_what_is_left_to_buy(self):
        haul = Haul(cargo={"steel": 400, "titanium": 100})
        self.assertEqual(copied(haul.start_effects), ["Alpha"])
        self.assertIn("      Steel 400   Titanium 100/320", haul.lines()[0])

    def test_one_commodity_at_two_stops_fills_in_plan_order_and_is_capped(self):
        doc = document([stop("Alpha", "Able Port", Steel=400),
                        stop("Beta", "Baker Hub", Steel=300)])
        haul = Haul(doc, cargo={"steel": 450})
        self.assertEqual(haul.session.bought, [{"steel": 400}, {"steel": 50}])
        over = Haul(doc, cargo={"steel": 5000})
        self.assertEqual(over.session.bought, [{"steel": 400}, {"steel": 300}])
        self.assertEqual(over.session.message, "Already aboard: Steel 700.")

    def test_any_spelling_of_the_commodity_is_recognised(self):
        doc = document([stop("A", "B", Land_Enrichment_Systems=60, Liquid_oxygen=10)])
        haul = Haul(doc, cargo={"terrainenrichmentsystems": 60, "$LiquidOxygen_name;": 10})
        self.assertEqual(haul.session.target(), (session_mod.SELL_KIND, -1))

    def test_cargo_already_counted_is_not_counted_twice(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.session.stop(now=at(5))
        _, effects = haul.session.start(now=at(60), cargo={"steel": 400})
        self.assertEqual(haul.session.bought[0], {"steel": 400})
        self.assertEqual(haul.session.message, "")
        self.assertNotIn(session_mod.SAVE, kinds(effects))

    def test_a_purchase_made_while_stopped_is_picked_up_on_the_next_start(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.session.stop(now=at(5))
        haul.buy("titanium", 320)                       # ignored: the session is stopped
        self.assertEqual(haul.session.bought[0], {"steel": 400})
        _, effects = haul.session.start(now=at(60), cargo={"steel": 400, "titanium": 320})
        self.assertEqual(haul.session.bought[0], {"steel": 400, "titanium": 320})
        self.assertEqual(copied(effects), ["Beta"])
        self.assertEqual(haul.session.message, "Already aboard: Titanium 320.")

    def test_a_part_delivered_run_is_measured_against_what_is_still_aboard(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.dock("Home", "Depot")
        haul.sell("steel", 300)                         # 100 t of it still aboard
        self.assertEqual(haul.session.run_index, 0)
        haul.session.stop(now=at(5))
        haul.session.start(now=at(60), cargo={"steel": 100})
        self.assertEqual(haul.session.bought[0], {"steel": 400})

    def test_a_hold_with_less_than_the_ledger_changes_nothing(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.session.stop(now=at(5))
        haul.session.start(now=at(60), cargo={})
        self.assertEqual(haul.session.bought[0], {"steel": 400})
        haul.session.stop(now=at(61))
        haul.session.start(now=at(62), cargo=None)      # hold not known at all
        self.assertEqual(haul.session.bought[0], {"steel": 400})

    def test_a_refused_start_does_not_touch_the_ledger(self):
        haul = Haul(sell=("", "", None), start=False)
        ok, _ = haul.session.start(now=at(0), cargo={"steel": 400})
        self.assertFalse(ok)
        self.assertEqual(haul.session.bought, [{}, {}])


class Display(unittest.TestCase):
    def test_the_block_matches_the_agreed_layout(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.buy("titanium", 320)
        lines, role = haul.lines()
        self.assertEqual(lines, [
            "HAUL  Run 1/2   ETA --",
            "[x] Alpha / Able Port",
            "      Steel 400   Titanium 320",
            ">>  Beta / Baker Hub",
            "      Aluminium 0/200",
            "then SELL -> Home",
        ])
        self.assertEqual(role, session_mod.BUY)

    def test_a_long_stop_wraps_its_commodities(self):
        haul = Haul(document([stop("A", "B", Steel=1, Titanium=2, Aluminium=3, Copper=4,
                                   Ceramic_Composites=5, Insulating_Membrane=6)]))
        item_lines = [line for line in haul.lines()[0] if line.startswith("      ")]
        self.assertGreater(len(item_lines), 1)
        for line in item_lines:
            self.assertLessEqual(len(line), 6 + session_mod.ITEM_LINE_WIDTH)

    def test_the_overview_lists_every_run_and_stop(self):
        haul = Haul(THREE_RUNS)
        haul.session.goto_run(2, now=at(0))            # run 1 flown before joining
        haul.clock = 1
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 120)
        rows = haul.session.overview(now=at(haul.clock))
        self.assertEqual(rows[:3], [
            ("Test haul", session_mod.NOTE),
            ("Sell at Home", session_mod.NOTE),
            ("Runs 1/3   400 / 900 t   Active 3m", session_mod.NOTE),
        ])
        body = rows[rows.index(("", session_mod.NOTE)):]
        self.assertEqual(body, [
            ("", session_mod.NOTE),
            ("Run 1/3   delivered before joining", session_mod.DONE),
            ("    Alpha / Able Port", session_mod.DONE),
            ("      Steel 400", session_mod.DONE),
            ("", session_mod.NOTE),
            ("Run 2/3   in progress", session_mod.CURRENT),
            (">>  Alpha / Able Port", session_mod.CURRENT),
            ("      Steel 120/300", session_mod.CURRENT),
            ("then SELL -> Home", session_mod.CURRENT),
            ("", session_mod.NOTE),
            ("Run 3/3   200 t", session_mod.TODO),
            ("    Beta / Baker Hub", session_mod.TODO),
            ("      Aluminium 200", session_mod.TODO),
        ])

    def test_the_overview_says_how_each_finished_run_ended(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.buy("titanium", 300)
        haul.dock("Home", "Depot")
        haul.contribute(Steel=400, Titanium=300)       # run 1: delivered short
        haul.session.next_run(now=at(haul.tick()))     # run 2: closed by hand
        heads = [line for line, role in haul.session.overview(now=at(haul.clock))
                 if line.startswith("Run ")]
        self.assertEqual(heads, [
            "Run 1/2   done in 5m   short: Aluminium 200, Titanium 20",
            "Run 2/2   closed by hand   short: Steel 100",
        ])
        self.assertNotIn(session_mod.CURRENT,
                         [role for _, role in haul.session.overview(now=at(haul.clock))])

    def test_the_overview_of_a_stopped_session_calls_the_run_next(self):
        haul = Haul(start=False)
        self.assertIn(("Run 1/2   next", session_mod.CURRENT), haul.session.overview(now=at(0)))
        self.assertEqual(session_mod.Session().overview(), [])

    def test_no_plan_shows_nothing(self):
        session = session_mod.Session()
        self.assertEqual(session.display_lines(now=at(0)), ([], session_mod.OFF))
        self.assertEqual(session.stats_lines(now=at(0)), [])
        self.assertEqual(session.clipboard_text(), "")

    def test_durations(self):
        fmt = session_mod.format_duration
        self.assertEqual([fmt(None), fmt(20), fmt(60), fmt(3599), fmt(3600), fmt(6000)],
                         ["--", "<1m", "1m", "59m", "1h 00m", "1h 40m"])


class Statistics(unittest.TestCase):
    def test_eta_comes_from_completed_runs(self):
        haul = Haul()
        self.assertIsNone(haul.session.stats(now=at(3)).eta_seconds)
        haul.run_one()                                    # six one-minute ticks
        done_at = haul.clock
        stats = haul.session.stats(now=at(done_at))
        self.assertEqual(stats.avg_run_seconds, done_at * 60)
        self.assertEqual(stats.eta_seconds, done_at * 60)         # one run left, just begun
        self.assertEqual((stats.runs_done, stats.runs_total), (1, 2))
        self.assertEqual((stats.delivered, stats.planned), (920, 1020))
        self.assertAlmostEqual(stats.tonnes_per_hour, 920 / (done_at / 60.0))

        # Two minutes into run 2 the ETA has come down by two minutes...
        self.assertEqual(haul.session.stats(now=at(done_at + 2)).eta_seconds,
                         (done_at - 2) * 60)
        # ...and a run that overruns the average bottoms out at zero, not below.
        self.assertEqual(haul.session.stats(now=at(done_at + 500)).eta_seconds, 0)

    def test_a_break_does_not_count_towards_the_eta(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.buy("titanium", 320)                          # three minutes in
        haul.session.stop(now=at(3))
        haul.session.start(now=at(603))                    # a ten-hour break
        haul.clock = 603
        haul.dock("Beta", "Baker Hub")
        haul.buy("aluminium", 200)
        haul.dock("Home", "Depot")
        haul.contribute(Steel=400, Titanium=320, Aluminium=200)   # four more minutes
        self.assertEqual(haul.session.history[0]["seconds"], 7 * 60)
        self.assertEqual(haul.session.stats(now=at(haul.clock)).active_seconds, 7 * 60)

    def test_a_run_closed_by_hand_with_nothing_delivered_is_not_averaged(self):
        haul = Haul()
        haul.session.next_run(now=at(1))
        stats = haul.session.stats(now=at(1))
        self.assertIsNone(stats.avg_run_seconds)
        self.assertIsNone(stats.eta_seconds)
        self.assertEqual(stats.shortfall, {"Steel": 400, "Titanium": 320, "Aluminium": 200})

    def test_over_delivery_does_not_hide_a_shortfall(self):
        haul = Haul(document([stop("Alpha", "Able Port", Steel=400, Titanium=320)],
                             [stop("Alpha", "Able Port", Steel=1)]))
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.dock("Home", "Depot")
        haul.contribute(Steel=900)            # more aboard than the plan knew about
        self.assertEqual(haul.session.history[0]["delivered"], 400)
        self.assertEqual(haul.session.history[0]["shortfall"], {"Titanium": 320})


class Persistence(unittest.TestCase):
    def round_trip(self, session, now):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / edha_store.FILENAME)
            self.assertTrue(edha_store.save(path, session.to_dict(now=now)))
            resumed = session_mod.Session()
            self.assertTrue(resumed.restore(edha_store.load(path)))
        return resumed

    def test_a_haul_resumes_at_the_same_stop(self):
        haul = Haul()
        haul.run_one()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 60)
        resumed = self.round_trip(haul.session, at(haul.clock + 5))

        self.assertFalse(resumed.running)                  # always comes back stopped
        self.assertEqual(resumed.run_index, 1)
        self.assertEqual(resumed.bought, [{"steel": 60}])
        self.assertEqual(resumed.history, haul.session.history)
        self.assertEqual(resumed.plan.name, "Test haul")
        self.assertEqual(resumed.display_lines(now=at(0))[0][1:3],
                         [">>  Alpha / Able Port", "      Steel 60/100"])
        # The live segment was banked into the save.
        self.assertEqual(resumed.active_seconds,
                         haul.session.stats(now=at(haul.clock + 5)).active_seconds)
        self.assertEqual(resumed.run_seconds, (2 + 5) * 60)

    def test_skips_and_deliveries_survive(self):
        haul = Haul()
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 400)
        haul.buy("titanium", 320)
        haul.session.skip_stop(now=at(3))
        haul.dock("Home", "Depot")
        haul.sell("steel", 400)
        resumed = self.round_trip(haul.session, at(haul.clock))
        self.assertEqual(resumed.skipped, {1})
        self.assertEqual(resumed.delivered, {"steel": 400})
        self.assertTrue(resumed.delivered_any)
        self.assertEqual(resumed.target(), (session_mod.SELL_KIND, -1))

    def test_a_completed_plan_stays_completed(self):
        haul = Haul(document([stop("Alpha", "Able Port", Steel=10)]))
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 10)
        haul.dock("Home", "Depot")
        haul.contribute(Steel=10)
        resumed = self.round_trip(haul.session, at(haul.clock))
        self.assertTrue(resumed.complete)
        self.assertIsNone(resumed.target())

    def test_state_that_does_not_fit_the_plan_is_dropped_not_trusted(self):
        haul = Haul()
        state = haul.session.to_dict(now=at(0))
        state.update(run_index=99, bought=[{"steel": 5}], skipped=[7, "x"],
                     delivered={"gold": 3}, active_seconds="soon")
        resumed = session_mod.Session()
        self.assertTrue(resumed.restore(state))
        self.assertTrue(resumed.complete)                  # clamped to the end

        state.update(run_index=0)
        self.assertTrue(resumed.restore(state))
        self.assertEqual(resumed.bought, [{}, {}])         # wrong length for this run
        self.assertEqual(resumed.skipped, set())
        self.assertEqual(resumed.delivered, {})
        self.assertEqual(resumed.active_seconds, 0.0)

    def test_nothing_saved_or_a_foreign_file_restores_nothing(self):
        session = session_mod.Session()
        for junk in (None, {}, {"version": 99, "plan": TWO_RUNS},
                     {"version": 1, "plan": None}, {"version": 1, "plan": {"schema": 7}}):
            with self.subTest(junk=junk):
                self.assertFalse(session.restore(junk))
                self.assertIsNone(session.plan)

    def test_loading_a_new_plan_discards_progress(self):
        haul = Haul()
        haul.run_one()
        plan, _ = edha_plan.parse(document([stop("X", "Y", Gold=1)], name="Next"))
        effects = haul.session.load_plan(plan)
        self.assertEqual(kinds(effects), [session_mod.SAVE, session_mod.REDRAW])
        self.assertFalse(haul.session.running)
        self.assertFalse(haul.session.has_progress)
        self.assertEqual(haul.session.stats(now=at(0)).active_seconds, 0)

    def test_reset_goes_back_to_run_one(self):
        haul = Haul()
        haul.run_one()
        haul.session.reset()
        self.assertEqual(haul.session.run_index, 0)
        self.assertEqual(haul.session.history, [])
        self.assertFalse(haul.session.running)


class SellLocation(unittest.TestCase):
    def test_changing_it_mid_delivery_recopies(self):
        haul = Haul(document([stop("Alpha", "Able Port", Steel=10)]))
        haul.dock("Alpha", "Able Port")
        haul.buy("steel", 10)
        effects = haul.session.set_sell("Elsewhere")
        self.assertEqual(copied(effects), ["Elsewhere"])
        # While still buying, the sell system is not what is on the clipboard.
        other = Haul()
        self.assertEqual(copied(other.session.set_sell("Elsewhere")), [])

    def test_a_market_id_without_a_station_is_meaningless(self):
        session = session_mod.Session()
        session.set_sell("Home", "", SITE_MARKET)
        self.assertIsNone(session.sell.market_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
