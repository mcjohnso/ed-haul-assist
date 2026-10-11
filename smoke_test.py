"""Stub-EDMC smoke test for EDHaulAssist.

EDMC's runtime modules (config, myNotebook, theme, edmcoverlay) do not exist
outside EDMC, so load.py cannot normally be imported. This fakes them, then drives
the plugin through a haul the way EDMC would - catching import errors, Tk layout
mistakes and broken wiring without launching the game.

Progress is written to a temporary directory, never to your real EDMC data.

Needs a display for tkinter, so run it locally:

    python smoke_test.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_DIR = ROOT / "EDHaulAssist"
SAMPLE = ROOT / "examples" / "sample_plan.json"
sys.path.insert(0, str(PLUGIN_DIR))

SCRATCH = tempfile.TemporaryDirectory()

# --- fake `config` -----------------------------------------------------------

config_mod = types.ModuleType("config")
config_mod.appname = "EDMarketConnector"
config_mod.appversion = "6.1.2"


class _Config:
    def __init__(self):
        self.store = {}
        self.app_dir_path = Path(SCRATCH.name)

    def get_str(self, k, *, default=""):
        return self.store.get(k, default)

    def get_int(self, k, *, default=0):
        return int(self.store.get(k, default))

    def get_bool(self, k, *, default=False):
        return bool(self.store.get(k, default))

    def set(self, k, v):
        # The real store is the Windows registry: an int has to fit in 32 bits.
        if isinstance(v, int) and not isinstance(v, bool):
            assert -2 ** 31 <= v < 2 ** 31, "{}={} does not fit EDMC's config".format(k, v)
        self.store[k] = v

    def delete(self, k, *, suppress=False):
        self.store.pop(k, None)

    shutting_down = False


config_mod.config = _Config()
sys.modules["config"] = config_mod

# --- fake `myNotebook` and `theme` -------------------------------------------

nb = types.ModuleType("myNotebook")
nb.Frame = tk.Frame
nb.Label = tk.Label
nb.Entry = tk.Entry
nb.Checkbutton = tk.Checkbutton
nb.Button = tk.Button
nb.Notebook = tk.Frame
sys.modules["myNotebook"] = nb

theme_mod = types.ModuleType("theme")
theme_mod.theme = types.SimpleNamespace(update=lambda w: None)
sys.modules["theme"] = theme_mod

# --- fake `edmcoverlay` (capture what the plugin draws) ----------------------

overlay_mod = types.ModuleType("edmcoverlay")
SENT = []


class Overlay:
    def __init__(self, *a, **k):
        pass

    def connect(self):
        pass

    def send_message(self, msgid, text, color, x, y, ttl=4, size="normal"):
        SENT.append({"id": msgid, "text": text, "color": color, "x": x, "y": y, "ttl": ttl})


overlay_mod.Overlay = Overlay
sys.modules["edmcoverlay"] = overlay_mod

# --- import and drive the plugin ---------------------------------------------

root = tk.Tk()
root.withdraw()

import load                       # noqa: E402
import edha_overlay               # noqa: E402
import edha_session               # noqa: E402

SITE = ("Eagle Sector YK-O b6-6", "Planetary Construction Site: Huang Base", 4385925635)
MARKET = ("Eagle Sector KC-V c2-2", "Anna Khammatova", 4368328707)


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def journal(event, where=None, **fields):
    system, station, _ = where or (load.this.session.loc_system,
                                   load.this.session.loc_station or None, None)
    entry = dict(event=event, timestamp=stamp(), **fields)
    assert load.journal_entry("CMDR Test", False, system, station, entry, {}) is None


def dock(where):
    journal("Docked", where, StarSystem=where[0], StationName=where[1], MarketID=where[2])


def overlay_text() -> str:
    return SENT[-1]["text"] if SENT else ""


def clipboard() -> str:
    try:
        return root.clipboard_get()
    except tk.TclError:
        return ""


def detail() -> str:
    return load.this.vars["detail"].get()


assert load.plugin_start3(str(PLUGIN_DIR)) == "Haul Assist"
load.this.confirm = lambda title, question: True
frame = load.plugin_app(root)
assert "Load a haul plan" in detail(), detail()
print("plugin_start3 / plugin_app OK")

load.plugin_prefs(root, "CMDR Test", False)
load.prefs_changed("CMDR Test", False)
assert load.this.notice == "", load.this.notice
print("plugin_prefs / prefs_changed OK")

# A file that cannot be followed is refused and says why.
broken = Path(SCRATCH.name) / "broken.json"
broken.write_text(json.dumps({"schema": 1, "runs": [{"stops": []}]}), encoding="utf-8")
assert load.this.load_plan_path(str(broken)) is False
assert "Run 1: no stops" in detail(), detail()

assert load.this.load_plan_path(str(SAMPLE)) is True
assert load.this.session.plan.name == "Huang Base - sample"
assert "Run 1/3" in detail() and "Stopped" in load.this.vars["status"].get()
print("plan loaded OK")

# Start is refused until there is somewhere to sell.
load._toggle_run()
assert not load.this.session.running and "sell system" in detail(), detail()

# Docked at the building site: "Here" captures system, station and market.
dock(SITE)
load._fill_here()
assert load.this.vars["sell_system"].get() == SITE[0]
assert load.this.vars["station"].get() == SITE[1]
assert config_mod.config.store["edha_sell_market_id"] == str(SITE[2])
assert SENT == [], "the overlay drew while stopped: {}".format(SENT)
print("sell location captured OK")

load._toggle_run()
assert load.this.session.running
assert load.this.buttons["run"].cget("text") == "Stop"
assert clipboard() == MARKET[0], clipboard()
assert SENT[-1]["id"] == edha_overlay.BLOCK_ID and SENT[-1]["color"] == edha_overlay.COLOR_BUY
assert overlay_text().startswith("HAUL  Run 1/3"), overlay_text()
assert ">>  Eagle Sector KC-V c2-2 / Anna Khammatova" in overlay_text()
print("start OK; overlay:\n" + overlay_text())

dock(MARKET)
journal("MarketBuy", MarketID=MARKET[2], Type="liquidoxygen", Type_Localised="Liquid oxygen",
        Count=824)
assert "Liquid oxygen 824   Titanium 0/474" in overlay_text(), overlay_text()
assert clipboard() == MARKET[0]
journal("MarketBuy", MarketID=MARKET[2], Type="titanium", Count=474)
assert clipboard() == SITE[0], clipboard()
assert SENT[-1]["color"] == edha_overlay.COLOR_SELL
assert ">>  SELL -> {} / {}".format(SITE[0], SITE[1]) in overlay_text(), overlay_text()
print("stop completed OK")

# A contribution at some other site in the same system is not ours.
dock((SITE[0], "Orbital Construction Site: Elsewhere", 4200000001))
journal("ColonisationContribution", MarketID=4200000001, Contributions=[
    {"Name": "$Titanium_name;", "Name_Localised": "Titanium", "Amount": 474}])
assert load.this.session.run_index == 0

dock(SITE)
journal("ColonisationContribution", MarketID=SITE[2], Contributions=[
    {"Name": "$LiquidOxygen_name;", "Name_Localised": "Liquid oxygen", "Amount": 824},
    {"Name": "$Titanium_name;", "Name_Localised": "Titanium", "Amount": 474}])
assert load.this.session.run_index == 1
assert clipboard() == MARKET[0], clipboard()
assert overlay_text().startswith("HAUL  Run 2/3"), overlay_text()
assert "Runs 1/3   1,298 / 3,196 t" in load.this.vars["stats"].get(), load.this.vars["stats"].get()
print("run completed OK; stats:\n" + load.this.vars["stats"].get())

# The heartbeat's redraw, by hand: it must re-send the block, not blank it.
before = len(SENT)
load.this.redraw()
assert len(SENT) == before + 1 and overlay_text().startswith("HAUL  Run 2/3")

# Skip a stop, then stop the session: nothing may draw from here on.
dock(MARKET)
journal("MarketBuy", MarketID=MARKET[2], Type="titanium", Count=500)
load._skip_stop()
assert clipboard() == "Sol", clipboard()
assert "[-] Eagle Sector KC-V c2-2 / Anna Khammatova" in overlay_text(), overlay_text()

load._toggle_run()
assert not load.this.session.running and overlay_text() == "", SENT[-1]
quiet = len(SENT)
root.clipboard_clear()
root.clipboard_append("untouched")
journal("MarketBuy", MarketID=MARKET[2], Type="titanium", Count=198)
journal("Undocked")
load.this.redraw()
assert len(SENT) == quiet, "the overlay drew while stopped: {}".format(SENT[quiet:])
assert clipboard() == "untouched"
assert "Titanium 500/698" in detail(), detail()      # nothing was credited either
print("stop OK; overlay stays blank")

# Restarting EDMC: the haul comes back at the same place, stopped.
load.plugin_stop()
state_file = Path(SCRATCH.name) / "EDHaulAssist" / "state.json"
assert state_file.is_file(), "no state written to {}".format(state_file)
assert not (PLUGIN_DIR / "state.json").exists(), "state leaked into the plugin folder"

assert load.plugin_start3(str(PLUGIN_DIR)) == "Haul Assist"
load.this.confirm = lambda title, question: True
frame = load.plugin_app(root)
resumed = load.this.session
assert not resumed.running and resumed.run_index == 1
assert resumed.bought[0] == {"titanium": 500} and resumed.skipped == {0}
assert (resumed.sell.system, resumed.sell.station, resumed.sell.market_id) == SITE
assert load.this.vars["sell_system"].get() == SITE[0]
assert "Run 2/3" in detail() and len(SENT) == quiet
print("resume after restart OK")

# Joining part-way: jump to run 3 with some of its cargo already in the hold.
# EDMC hands the hold to plugins in `state`, keyed by lowercase symbol.
assert load.this.buttons["goto"].cget("state") == tk.NORMAL
load.this.ask_run = lambda parent, total, current: 3
load._goto_run()
assert resumed.run_index == 2 and "Run 3/3" in detail(), detail()
assert "Run 2 marked as already delivered." in detail(), detail()
load.journal_entry("CMDR Test", False, MARKET[0], None,
                   {"event": "Cargo", "timestamp": stamp(), "Vessel": "Ship"},
                   {"Cargo": {"steel": 540, "drones": 4}})
load._toggle_run()
assert "Already aboard: Steel 540." in detail(), detail()
assert "Land Enrichment Systems 0/60   Steel 540" in overlay_text(), overlay_text()
assert "Already aboard" not in overlay_text()
assert clipboard() == "Sol", clipboard()
load._toggle_run()
assert not resumed.running and overlay_text() == ""
print("go to run / cargo already aboard OK")


# The pop-out window lists every run, follows changes, and survives being closed.
def plan_text() -> str:
    return load.this.plan_text.get("1.0", tk.END)


assert load.this.buttons["plan"].cget("state") == tk.NORMAL
load.this.buttons["plan"].invoke()
root.update()
shown = plan_text()
assert load.this.plan_window.title() == "Haul Assist - Huang Base - sample"
for expected in ("Run 1/3   done in", "Run 2/3   delivered before joining",
                 "Run 3/3   next", "      Land Enrichment Systems 0/60   Steel 540",
                 "Sell at {} / {}".format(SITE[0], SITE[1])):
    assert expected in shown, "{!r} missing from:\n{}".format(expected, shown)
assert load.this.plan_text.tag_ranges(edha_session.DONE)
assert load.this.plan_text.tag_ranges(edha_session.CURRENT)
assert overlay_text() == "", "the plan window drew on the overlay"

only_window = load.this.plan_window
load.this.open_plan_window()                      # a second click reuses the window
assert load.this.plan_window is only_window

load.this.ask_run = lambda parent, total, current: 2
load._goto_run()
root.update()
assert "Run 2/3   next" in plan_text() and "Run 3/3   600 t" in plan_text(), plan_text()

only_window.destroy()                             # closed some way we did not see
load.this.redraw()
assert load.this.plan_window is None
load.this.open_plan_window()
root.update()
assert "Run 2/3   next" in plan_text()
load.this.close_plan_window()
assert load.this.plan_window is None
load.this.redraw()
print("plan window OK:\n" + shown)

# The last delivery of a plan: the session stops itself and says so once.
tiny = Path(SCRATCH.name) / "tiny.json"
tiny.write_text(json.dumps({"schema": 1, "name": "Tiny", "runs": [{"stops": [
    {"system": MARKET[0], "station": MARKET[1],
     "commodities": [{"name": "Steel", "quantity": 10}]}]}]}), encoding="utf-8")
assert load.this.load_plan_path(str(tiny)) is True
load._toggle_run()
dock(MARKET)
journal("MarketBuy", MarketID=MARKET[2], Type="steel", Count=10)
dock(SITE)
journal("ColonisationContribution", MarketID=SITE[2], Contributions=[
    {"Name": "$Steel_name;", "Name_Localised": "Steel", "Amount": 10}])
assert load.this.session.complete and not load.this.session.running
assert "Plan complete" in overlay_text() and SENT[-1]["ttl"] == 20, SENT[-1]
flashed = len(SENT)
journal("Undocked")
load.this.redraw()
assert len(SENT) == flashed, "the completion notice was wiped early"
assert load.this.buttons["skip"].cget("state") == tk.DISABLED
assert "Complete" in load.this.vars["status"].get()
print("plan complete OK; overlay:\n" + overlay_text())

load._reset()
assert load.this.session.run_index == 0 and "Run 1/1" in detail()

load.plugin_stop()
root.destroy()
SCRATCH.cleanup()
print("SMOKE TEST: OK")
