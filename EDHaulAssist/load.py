"""EDHaulAssist - follow an imported haul plan, one run at a time.

Something else works out what to buy where and exports it as JSON (see
edha_plan.py for the format). This plugin walks you through it: the current run
on the in-game overlay, the next system on the clipboard, Stops ticked off as the
journal reports the purchases and Runs ticked off as it reports the deliveries.
Dock, buy, paste, fly, repeat - then paste, fly, deliver.

The state machine is in edha_session.py and knows nothing about EDMC or tkinter;
this file owns every Tk and EDMC interaction and simply carries out the Effects
the session hands back.
"""

from __future__ import annotations

import logging
import os
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from typing import Dict, List, Optional

from config import appname, config
from theme import theme

import edha_overlay
import edha_plan
import edha_session as session_mod
import edha_settings
import edha_store

__version__ = "0.1.0"
plugin_version = __version__

PLUGIN_NAME = "EDHaulAssist"
DISPLAY_NAME = "Haul Assist"

logger = logging.getLogger("{}.{}".format(appname, os.path.basename(os.path.dirname(__file__))))

#: The journal goes quiet while docked, so the overlay (whose messages expire on
#: a TTL) and the elapsed/ETA figures are refreshed on a timer of our own.
HEARTBEAT_MS = 3000

# The Sell Location belongs to the haul rather than to the plan file, and it
# usually outlives one plan - the next export is for the same building site.
# Prefixed because the config store is shared with every plugin.
CFG_SELL_SYSTEM = "edha_sell_system"
CFG_SELL_STATION = "edha_sell_station"
#: Stored as text: a MarketID does not fit the 32-bit integer EDMC's config holds.
CFG_SELL_MARKET = "edha_sell_market_id"
CFG_PLAN_DIR = "edha_plan_dir"

ANY_STATION = "any station in the system"

_TK_COLORS = {session_mod.BUY: "#e08000", session_mod.SELL: "#207a20", session_mod.OFF: None}
_OVERLAY_COLORS = {session_mod.BUY: edha_overlay.COLOR_BUY, session_mod.SELL: edha_overlay.COLOR_SELL}
#: The pop-out plan window: runs behind you in green, the one you are on in
#: orange. Runs still to come keep the theme's own text colour.
_PLAN_COLORS = {session_mod.DONE: "#207a20", session_mod.CURRENT: "#e08000"}


def _state_path(plugin_dir: str) -> str:
    """Progress is kept in EDMC's data folder, not the plugin's own, so that
    replacing the plugin folder to upgrade it does not lose a haul in progress."""
    base = getattr(config, "app_dir_path", None)
    if base:
        directory = os.path.join(str(base), PLUGIN_NAME)
        try:
            os.makedirs(directory, exist_ok=True)
            return os.path.join(directory, edha_store.FILENAME)
        except OSError as exc:
            logger.warning("cannot use %s (%s); keeping state in the plugin folder", directory, exc)
    return os.path.join(plugin_dir, edha_store.FILENAME)


def _confirm(title: str, question: str) -> bool:
    return bool(messagebox.askyesno(title, question))


def _ask_run(parent, total: int, current: int) -> Optional[int]:
    return simpledialog.askinteger(
        DISPLAY_NAME,
        "Go to which run (1-{})?\n\n"
        "Runs you jump past are marked as already delivered.\n"
        "Going back discards what was recorded from that run on.".format(total),
        parent=parent, minvalue=1, maxvalue=total, initialvalue=current)


class Plugin:
    def __init__(self, plugin_dir: str) -> None:
        self.session = session_mod.Session()
        self.overlay = edha_overlay.OverlayManager()
        self.state_path = _state_path(plugin_dir)
        #: Swapped out by smoke_test.py, which has nobody to click "Yes".
        self.confirm = _confirm
        self.ask_run = _ask_run
        #: The hold as EDMC last reported it, for when `monitor` cannot be asked.
        self.cargo: Optional[Dict[str, int]] = None
        #: A one-off message from the plugin itself (a rejected file, a bad
        #: preference) rather than from the session's own state.
        self.notice = ""

        # Widgets, wired up in plugin_app().
        self.frame: Optional[tk.Frame] = None
        self.vars: Dict[str, tk.StringVar] = {}
        self.detail_label: Optional[tk.Label] = None
        self.buttons: Dict[str, tk.Button] = {}
        self.default_fg: Optional[str] = None

        # The pop-out view of the whole plan, while it is open.
        self.plan_window: Optional[tk.Toplevel] = None
        self.plan_text: Optional[tk.Text] = None
        #: What the window last drew, and for which run, so that a redraw with
        #: nothing new leaves the text - and where you had scrolled to - alone.
        self.plan_rendered: Optional[list] = None
        self.plan_run_index = -1

    # --- persistence --------------------------------------------------------

    def restore(self) -> None:
        market = config.get_str(CFG_SELL_MARKET, default="")
        self.session.set_sell(config.get_str(CFG_SELL_SYSTEM, default=""),
                              config.get_str(CFG_SELL_STATION, default=""),
                              int(market) if market.isdigit() else None)
        saved = edha_store.load(self.state_path)
        if saved is not None and self.session.restore(saved):
            logger.info("resumed %r at run %d", self.session.plan.name,
                        self.session.run_index + 1)

    def save(self) -> None:
        edha_store.save(self.state_path, self.session.to_dict())

    def save_sell(self) -> None:
        sell = self.session.sell
        config.set(CFG_SELL_SYSTEM, sell.system)
        config.set(CFG_SELL_STATION, sell.station)
        config.set(CFG_SELL_MARKET, "" if sell.market_id is None else str(sell.market_id))

    def current_cargo(self) -> Optional[Dict[str, int]]:
        """What is in the hold, or None if EDMC has not said. Asked only at
        Start, when the ship is at rest and EDMC's figure has settled."""
        try:
            from monitor import monitor        # EDMC's own; absent under test
            cargo = monitor.state.get("Cargo")
            if cargo is not None:
                return dict(cargo)
        except Exception:
            pass
        return self.cargo

    # --- effects ------------------------------------------------------------

    def perform(self, effects: List[session_mod.Effect]) -> None:
        for effect in effects or ():
            if effect.kind == session_mod.CLIPBOARD:
                self.copy_to_clipboard(effect.payload)
            elif effect.kind == session_mod.SAVE:
                self.save()
            elif effect.kind == session_mod.REDRAW:
                self.redraw()
            elif effect.kind == session_mod.FLASH:
                if edha_settings.overlay_enabled():
                    self.overlay.flash(effect.payload)

    def copy_to_clipboard(self, text: str) -> bool:
        """Tk owns the clipboard. The trailing update() is required on Windows or
        the contents vanish as soon as EDMC loses focus - which is immediately,
        since the next thing you do is alt-tab into the game."""
        widget = self.frame
        if widget is None or not text:
            return False
        try:
            widget.clipboard_clear()
            widget.clipboard_append(text)
            widget.update()
        except tk.TclError:
            logger.debug("clipboard unavailable")
            return False
        return True

    # --- rendering ----------------------------------------------------------

    def redraw(self) -> None:
        if self.frame is None:
            return
        state = self.session
        name = state.plan.name if state.plan is not None else ""
        self.vars["status"].set(
            "{}  -  {}".format(state.status_line(), name) if name else state.status_line())
        self.vars["station"].set(state.sell.station or ANY_STATION)

        lines, role = state.display_lines()
        shown = list(lines) if lines else ["Load a haul plan (JSON) to begin."]
        for extra in (state.message, self.notice):
            if extra:
                shown.append(extra)
        self.vars["detail"].set("\n".join(shown))
        self.vars["stats"].set("\n".join(state.stats_lines()))
        if self.detail_label is not None:
            try:
                self.detail_label.configure(foreground=_TK_COLORS.get(role) or self.default_fg)
            except tk.TclError:
                pass

        target = state.target()
        enabled = {
            "run": True,
            "copy": bool(state.clipboard_text()),
            "skip": target is not None and target[0] == session_mod.STOP_KIND,
            "next": target is not None,
            "goto": state.plan is not None and len(state.plan.runs) > 1,
            "plan": state.plan is not None,
            "reset": state.has_progress,
        }
        self.buttons["run"].configure(text="Stop" if state.running else "Start")
        for key, on in enabled.items():
            self.buttons[key].configure(state=tk.NORMAL if on else tk.DISABLED)

        if edha_settings.overlay_enabled():
            self.overlay.set_position(*edha_settings.overlay_position())
            self.overlay.set_size(edha_settings.overlay_size())
            self.overlay.set_enabled(True)
            if role == session_mod.OFF:
                # Nothing draws in the game unless a session is started. Journal
                # events and the panel's own buttons redraw while stopped too, so
                # this has to be decided on every redraw, not once at Stop.
                self.overlay.clear()
            else:
                self.overlay.show(lines, _OVERLAY_COLORS[role])
        else:
            self.overlay.set_enabled(False)

        self.refresh_plan_window()

    # --- the whole plan, in a window of its own -----------------------------

    def open_plan_window(self) -> None:
        if self.plan_window is not None:
            try:
                self.plan_window.deiconify()
                self.plan_window.lift()
                self.refresh_plan_window(find_current=True)
                return
            except tk.TclError:
                self.plan_window = None        # closed behind our back
        if self.frame is None or self.session.plan is None:
            return

        window = tk.Toplevel(self.frame)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        # A Text is not something EDMC's theme reaches, so it borrows the
        # colours the theme has already given the panel.
        background = self.detail_label.cget("background")
        text = tk.Text(window, width=72, height=34, wrap=tk.NONE, font=("TkFixedFont", 9),
                       background=background, foreground=self.default_fg, borderwidth=0,
                       highlightthickness=0, padx=8, pady=6, cursor="arrow",
                       state=tk.DISABLED)
        scrollbar = tk.Scrollbar(window, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar.grid(row=0, column=1, sticky=tk.NS)
        window.configure(background=background)
        for role, colour in _PLAN_COLORS.items():
            text.tag_configure(role, foreground=colour)
        window.protocol("WM_DELETE_WINDOW", self.close_plan_window)

        self.plan_window, self.plan_text, self.plan_rendered = window, text, None
        self.refresh_plan_window(find_current=True)

    def close_plan_window(self) -> None:
        window, self.plan_window, self.plan_text = self.plan_window, None, None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def refresh_plan_window(self, find_current: bool = False) -> None:
        """Redraw the pop-out if it is open and something has changed. It keeps
        your scroll position, except that it follows you to a new run."""
        if self.plan_window is None or self.plan_text is None:
            return
        try:
            alive = bool(self.plan_window.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive:                          # closed behind our back
            self.plan_window = self.plan_text = None
            return
        state = self.session
        rows = state.overview()
        moved = state.run_index != self.plan_run_index
        if rows == self.plan_rendered and not find_current:
            return
        text = self.plan_text
        try:
            self.plan_window.title("{} - {}".format(
                DISPLAY_NAME, (state.plan.name if state.plan is not None else "") or "plan"))
            top = text.yview()[0]
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            current = None
            for number, (line, role) in enumerate(rows, start=1):
                if current is None and role == session_mod.CURRENT:
                    current = number
                text.insert(tk.END, line + "\n", role)
            text.configure(state=tk.DISABLED)
            if (find_current or moved) and current is not None:
                text.yview("{}.0".format(max(1, current - 1)))
            else:
                text.yview_moveto(top)
        except tk.TclError:
            self.plan_window = self.plan_text = None
            return
        self.plan_rendered = rows
        self.plan_run_index = state.run_index

    # --- panel actions ------------------------------------------------------

    def apply_sell_entry(self) -> None:
        """Take the typed sell system. A different system cannot still be the
        captured station's, so changing it drops the station."""
        typed = self.vars["sell_system"].get().strip()
        sell = self.session.sell
        if typed.casefold() == sell.system.casefold():
            return
        self.perform(self.session.set_sell(typed))
        self.save_sell()

    def load_plan_path(self, path: str) -> bool:
        try:
            plan, warnings = edha_plan.load_file(path)
        except edha_plan.PlanError as exc:
            self.notice = str(exc)
            self.redraw()
            return False
        if self.session.has_progress and not self.confirm(
                DISPLAY_NAME, "Loading this plan discards your progress on the current one. "
                              "Continue?"):
            return False
        self.notice = " ".join(warnings[:3])
        if len(warnings) > 3:
            self.notice += " (+{} more)".format(len(warnings) - 3)
        for warning in warnings:
            logger.warning("plan: %s", warning)
        config.set(CFG_PLAN_DIR, os.path.dirname(os.path.abspath(path)))
        self.perform(self.session.load_plan(plan, warnings))
        return True


this: Optional[Plugin] = None


# --- EDMC callbacks ----------------------------------------------------------

def plugin_start3(plugin_dir: str) -> str:
    global this
    this = Plugin(plugin_dir)
    this.restore()
    logger.info("%s %s started", PLUGIN_NAME, __version__)
    return DISPLAY_NAME


def plugin_stop() -> None:
    if this is None:
        return
    # Stopping banks the time since Start; the save is what keeps it.
    this.session.stop()
    this.save()
    this.overlay.stop()
    this.close_plan_window()


def plugin_app(parent):
    frame = tk.Frame(parent)
    frame.columnconfigure(1, weight=1)
    this.frame = frame

    def var(name: str, value: str = "") -> tk.StringVar:
        this.vars[name] = tk.StringVar(master=frame, value=value)
        return this.vars[name]

    def button(key: str, holder, text: str, command, column: int, **grid) -> tk.Button:
        this.buttons[key] = tk.Button(holder, text=text, command=command)
        this.buttons[key].grid(row=grid.pop("row", 0), column=column, **grid)
        return this.buttons[key]

    tk.Label(frame, text=DISPLAY_NAME).grid(row=0, column=0, sticky=tk.W)
    tk.Label(frame, textvariable=var("status"), anchor=tk.W).grid(
        row=0, column=1, sticky=tk.EW, padx=(6, 0))
    button("load", frame, "Load plan...", _load_plan, 2, sticky=tk.E)
    button("run", frame, "Start", _toggle_run, 3, sticky=tk.E, padx=(4, 0))

    tk.Label(frame, text="Sell system").grid(row=1, column=0, sticky=tk.W)
    system_row = tk.Frame(frame)
    system_row.grid(row=1, column=1, columnspan=3, sticky=tk.EW)
    system_row.columnconfigure(0, weight=1)
    entry = tk.Entry(system_row, textvariable=var("sell_system", this.session.sell.system))
    entry.grid(row=0, column=0, sticky=tk.EW)
    entry.bind("<Return>", lambda _event: this.apply_sell_entry())
    entry.bind("<FocusOut>", lambda _event: this.apply_sell_entry())
    button("here", system_row, "Here", _fill_here, 1, padx=(4, 0))

    tk.Label(frame, text="Sell station").grid(row=2, column=0, sticky=tk.W)
    station_row = tk.Frame(frame)
    station_row.grid(row=2, column=1, columnspan=3, sticky=tk.EW)
    station_row.columnconfigure(0, weight=1)
    tk.Label(station_row, textvariable=var("station", ANY_STATION), anchor=tk.W).grid(
        row=0, column=0, sticky=tk.EW)
    button("clear", station_row, "Clear", _clear_station, 1, padx=(4, 0))

    this.detail_label = tk.Label(frame, textvariable=var("detail"), anchor=tk.W,
                                 justify=tk.LEFT, font=("TkFixedFont", 8))
    this.detail_label.grid(row=3, column=0, columnspan=4, sticky=tk.EW, pady=(4, 0))
    tk.Label(frame, textvariable=var("stats"), anchor=tk.W, justify=tk.LEFT).grid(
        row=4, column=0, columnspan=4, sticky=tk.EW)

    actions = tk.Frame(frame)
    actions.grid(row=5, column=0, columnspan=4, sticky=tk.W)
    button("copy", actions, "Copy system", _copy_again, 0)
    button("skip", actions, "Skip stop", _skip_stop, 1, padx=(4, 0))
    button("next", actions, "Next run", _next_run, 2, padx=(4, 0))
    button("goto", actions, "Go to run...", _goto_run, 0, row=1, pady=(2, 0), sticky=tk.W)
    button("plan", actions, "Show plan", this.open_plan_window, 1, row=1, padx=(4, 0),
           pady=(2, 0), sticky=tk.W)
    button("reset", actions, "Reset", _reset, 2, row=1, padx=(4, 0), pady=(2, 0), sticky=tk.W)

    theme.update(frame)
    this.default_fg = this.detail_label.cget("foreground")
    this.redraw()
    _schedule_heartbeat(frame)
    return frame


def plugin_prefs(parent, cmdr, is_beta):
    return edha_settings.build(parent, cmdr, is_beta)


def prefs_changed(cmdr, is_beta) -> None:
    if this is None:
        return
    this.notice = " ".join(edha_settings.save())
    this.redraw()


def journal_entry(cmdr, is_beta, system, station, entry, state):
    if this is None:
        return None
    try:
        if isinstance(state, dict) and state.get("Cargo") is not None:
            this.cargo = dict(state["Cargo"])
        this.perform(this.session.on_journal(entry, system=system, station=station))
    except Exception:
        logger.exception("journal_entry failed for %s", (entry or {}).get("event"))
    return None


# --- panel actions -----------------------------------------------------------

def _toggle_run() -> None:
    this.notice = ""
    if this.session.running:
        this.perform(this.session.stop())
        return
    this.apply_sell_entry()
    _, effects = this.session.start(cargo=this.current_cargo())
    # A refused start leaves the session stopped and showing why, so the button
    # still reads "Start" and the next click retries.
    this.perform(effects)


def _load_plan() -> None:
    path = filedialog.askopenfilename(
        parent=this.frame, title="Load haul plan",
        initialdir=config.get_str(CFG_PLAN_DIR, default="") or None,
        filetypes=(("Haul plan", "*.json"), ("All files", "*.*")))
    if path:
        this.load_plan_path(path)


def _fill_here() -> None:
    state = this.session
    if not state.loc_system:
        this.notice = "Current system not known yet - jump or dock first."
        this.redraw()
        return
    this.notice = ""
    this.vars["sell_system"].set(state.loc_system)
    this.perform(state.set_sell(state.loc_system, state.loc_station, state.loc_market_id))
    this.save_sell()


def _clear_station() -> None:
    this.perform(this.session.set_sell(this.session.sell.system))
    this.save_sell()


def _copy_again() -> None:
    this.copy_to_clipboard(this.session.clipboard_text())


def _skip_stop() -> None:
    this.perform(this.session.skip_stop())


def _next_run() -> None:
    if this.confirm(DISPLAY_NAME, "Close run {} as it stands? Anything not delivered is "
                                  "recorded as shortfall.".format(this.session.run_index + 1)):
        this.perform(this.session.next_run())


def _goto_run() -> None:
    state = this.session
    if state.plan is None:
        return
    total = len(state.plan.runs)
    number = this.ask_run(this.frame, total, min(state.run_index + 1, total))
    if number:
        this.perform(state.goto_run(number))


def _reset() -> None:
    if this.confirm(DISPLAY_NAME, "Reset all progress on this plan back to run 1?"):
        this.perform(this.session.reset())


def _schedule_heartbeat(widget) -> None:
    def beat():
        try:
            if config.shutting_down:
                return
        except Exception:
            pass
        try:
            if this is not None and this.session.running:
                this.redraw()
            widget.after(HEARTBEAT_MS, beat)
        except tk.TclError:
            pass

    widget.after(HEARTBEAT_MS, beat)
