"""Preferences tab for EDHaulAssist.

The things that change with every haul - the plan and the Sell Location - live in
the main EDMC panel. This tab holds only what you set once: whether the in-game
overlay is used, and where it sits.

Layout is GRID ONLY - EDMC's nb.Frame already manages its children with grid, and
mixing pack() anywhere inside raises TclError.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

import myNotebook as nb
from config import config

import edha_overlay

logger = logging.getLogger(__name__)

# EDMC's themed entry is `EntryMenu` in current versions and `Entry` in older ones.
_Entry = getattr(nb, "Entry", None) or getattr(nb, "EntryMenu", None) or tk.Entry

# --- config keys (the store is shared across every installed plugin) ---------

CFG_OVERLAY = "edha_overlay_enabled"
CFG_OVERLAY_X = "edha_overlay_x"
CFG_OVERLAY_Y = "edha_overlay_y"
CFG_OVERLAY_SIZE = "edha_overlay_size"

_vars: Dict[str, Any] = {}


def overlay_enabled() -> bool:
    return config.get_bool(CFG_OVERLAY, default=True)


def overlay_position() -> Tuple[int, int]:
    return (config.get_int(CFG_OVERLAY_X, default=0) or edha_overlay.DEFAULT_X,
            config.get_int(CFG_OVERLAY_Y, default=0) or edha_overlay.DEFAULT_Y)


def overlay_size() -> str:
    size = config.get_str(CFG_OVERLAY_SIZE, default="")
    return size if size in edha_overlay.SIZES else edha_overlay.DEFAULT_SIZE


# --- the tab -----------------------------------------------------------------

def build(parent, cmdr: Optional[str], is_beta: bool):
    frame = nb.Frame(parent)
    frame.columnconfigure(1, weight=1)
    row = 0

    nb.Label(frame, text="Overlay").grid(row=row, column=0, padx=8, pady=(8, 2), sticky=tk.W)
    row += 1

    overlay_var = tk.BooleanVar(master=frame, value=overlay_enabled())
    _vars[CFG_OVERLAY] = overlay_var
    nb.Checkbutton(frame, text="Show the current run on the in-game overlay",
                   variable=overlay_var).grid(row=row, column=0, columnspan=2,
                                              padx=8, pady=2, sticky=tk.W)
    row += 1

    pos_x, pos_y = overlay_position()
    nb.Label(frame, text="Overlay position (x, y)").grid(row=row, column=0, padx=8, pady=2,
                                                         sticky=tk.W)
    holder = nb.Frame(frame)
    holder.grid(row=row, column=1, padx=8, pady=2, sticky=tk.W)
    x_var = tk.StringVar(master=frame, value=str(pos_x))
    y_var = tk.StringVar(master=frame, value=str(pos_y))
    _vars[CFG_OVERLAY_X] = x_var
    _vars[CFG_OVERLAY_Y] = y_var
    _Entry(holder, textvariable=x_var, width=5).grid(row=0, column=0)
    _Entry(holder, textvariable=y_var, width=5).grid(row=0, column=1, padx=(4, 0))
    row += 1

    nb.Label(frame, text="Overlay text size").grid(row=row, column=0, padx=8, pady=2,
                                                   sticky=tk.W)
    size_var = tk.StringVar(master=frame, value=overlay_size())
    _vars[CFG_OVERLAY_SIZE] = size_var
    ttk.Combobox(frame, textvariable=size_var, values=list(edha_overlay.SIZES),
                 state="readonly", width=8).grid(row=row, column=1, padx=8, pady=2,
                                                 sticky=tk.W)
    row += 1

    nb.Label(
        frame,
        text=("The overlay sits in the top-left corner by default and only draws while "
              "a session is started. It needs EDMCOverlay or EDMCModernOverlay installed "
              "separately; without one, everything still shows in the EDMC window."),
        wraplength=380, justify=tk.LEFT,
    ).grid(row=row, column=0, columnspan=2, padx=8, pady=(2, 8), sticky=tk.W)

    return frame


def save() -> List[str]:
    """Persist the tab. Returns messages about anything that was rejected."""
    problems: List[str] = []

    for key, label in ((CFG_OVERLAY_X, "Overlay x"), (CFG_OVERLAY_Y, "Overlay y")):
        var = _vars.get(key)
        try:
            raw = str(var.get()).strip() if var is not None else ""
        except tk.TclError:
            raw = ""
        if not raw:
            continue
        try:
            value = int(float(raw))
        except ValueError:
            problems.append("{} must be a number; kept the previous value.".format(label))
            continue
        if not 0 <= value <= 4000:
            problems.append("{} must be between 0 and 4000.".format(label))
            continue
        config.set(key, value)

    size_var = _vars.get(CFG_OVERLAY_SIZE)
    if size_var is not None:
        try:
            size = str(size_var.get()).strip()
        except tk.TclError:
            size = ""
        if size in edha_overlay.SIZES:
            config.set(CFG_OVERLAY_SIZE, size)

    overlay_var = _vars.get(CFG_OVERLAY)
    if overlay_var is not None:
        try:
            config.set(CFG_OVERLAY, bool(overlay_var.get()))
        except tk.TclError:
            pass

    return problems
