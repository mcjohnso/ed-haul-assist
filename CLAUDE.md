# Working on ed-haul-assist

An EDMC plugin. Read `EDHaulAssist/README.md` for what it does; this file is the things that will
bite you. Much of the plumbing came from the sibling project `../ed-trade-assist`, and its
`CLAUDE.md` explains several of these constraints at greater length.

## Constraints

- **"Standard library only" is necessary but NOT sufficient.** EDMC ships a *frozen* Python, and its
  `library.zip` contains only the stdlib modules EDMC itself uses (`difflib` and `unittest` are
  absent, for instance). A missing import makes the plugin fail to load with nothing visible in the
  UI — the error appears only in `%LOCALAPPDATA%\EDMarketConnector\logs\EDMarketConnector-debug.log`.
  `FrozenRuntimeImports` in `tests/test_core.py` enforces an allow-list and, when EDMC is installed
  locally, checks against the real `library.zip`. Adding an import means verifying it there first.
- **Do not rename the `edha_`-prefixed helper modules**, and import them top-level
  (`import edha_session`). EDMC adds every plugin's folder to one shared `sys.path`; a generic name
  collides silently, and package-relative imports fail because `load.py` is not imported as a package.
- **The overlay message id is `haulasst_block`, deliberately NOT `edha_`.** EDMCModernOverlay applies
  a position-normalising transform to ids matching a registered plugin group; an unregistered prefix
  keeps the x/y we send honoured.
- **The overlay is one newline-joined message, not a message per line.** The overlay server lays out
  lines with its own font metrics, so hand-computed y positions drift under display scaling. This is
  why Stops are marked with `[x]` / `>>` / `[ ]` text rather than per-line colours.
- **Never store a MarketID with `config.set(key, int)`.** EDMC's config is the Windows registry and
  MarketIDs do not fit in 32 bits. `CFG_SELL_MARKET` is stored as text; `smoke_test.py`'s fake config
  asserts on this.
- **Never create a folder named `config/`** inside the plugin — it shadows EDMC's own module.
- `__version__` in `load.py` is the single source of truth. The git tag must match it.
- All EDMC callbacks run on the main Tk thread. There is no worker thread here; keep it that way
  unless something genuinely needs the network.

## Architecture

`edha_session.py`, `edha_plan.py` and `edha_store.py` import nothing from EDMC or tkinter. `load.py`
performs the `Effect`s the session hands back (`CLIPBOARD`, `REDRAW`, `SAVE`, `FLASH`) and owns
every Tk and EDMC interaction. Every time-dependent session method takes `now=None`; `load.py`
passes nothing and tests pass everything.

The plan document is persisted **as imported** (`Plan.raw`) inside `state.json`, and re-parsed by
`edha_plan.parse` on restore — one reader for both paths. `state.json` lives under
`config.app_dir_path / "EDHaulAssist"`, outside the plugin folder, so upgrading the plugin does not
lose a haul. The Sell Location is not in the plan or the state file: it is EDMC config, because it
usually outlives a single plan.

The **Show plan** window is a read-only `tk.Text` fed by `Session.overview()`, which returns
`(line, role)` pairs and is tested without Tk. `redraw()` refreshes it, and it rewrites the text only
when the rows differ from what it last drew — the heartbeat redraws every three seconds, and
rewriting each time would throw away the scroll position. EDMC's theme does not reach a `Text`, so
it borrows the panel label's colours.

## Rules that look arbitrary and are not

- **Nothing draws on the overlay unless a session is running.** `display_lines()` returns role `OFF`
  when stopped and `redraw()` clears on `OFF` every time, because journal events and panel buttons
  redraw while stopped too. The one exception is `FLASH` ("Plan complete"), sent via
  `OverlayManager.flash()`, which leaves `_showing` false precisely so that the next `clear()` does
  not wipe it.
- **Two clocks, never subtracted from each other.** Journal timestamps are only compared against
  `started_at` to reject replayed events (`is_replay`, `REPLAY_SLACK`). Elapsed time is wall clock,
  because the journal falls silent while docked.
- **`_position()` rather than `target()` decides whether to re-copy.** The first Stop of one run and
  the first Stop of the next are the same `(kind, index)` pair.
- **A delivery is ignored until the run has cargo** (something credited, or every Stop closed). A
  run completes on the first sale that empties what the ledger knew about; the *next* `MarketSell`
  of that same unloading would otherwise be booked against the following run and could complete it.
  `RunTracking.test_the_tail_of_one_unloading_does_not_start_the_next_run` pins this.
- **Run completion is a ledger (`bought − delivered`), not EDMC's `state["Cargo"]`.** EDMC's monitor
  does not handle `ColonisationContribution` at all; its cargo only catches up when the game writes
  a `Cargo` event a second or two later, so the hold is stale at exactly the moment a delivery
  arrives.
- **The hold IS read at Start, and only then** (`Session._reconcile`). At rest it has settled, and
  it is the only way to see cargo bought before the plugin was in use or while stopped. It credits
  upward only: a hold with less than the ledger expects could be a delivery made while stopped or
  cargo sold elsewhere, and the hold cannot say which. `load.py` asks `monitor.state` first and
  falls back to the `state` last passed to `journal_entry`.
- **Runs jumped past with Go to run are `external`.** They count as delivered in full so they are
  not shortfall, but carry `seconds: 0` and are subtracted from the tonnes-per-hour figure, because
  none of that tonnage was moved on this clock.
- **Shortfall is measured against deliveries, capped per commodity.** Over-delivering Steel does not
  make up for missing Titanium.
- **An unknown commodity warns; it does not reject the plan.** The journal carries
  `Type_Localised` / `Name_Localised`, and `Session._key` falls back to it, so a commodity newer
  than the bundled table still tracks.

## Journal shapes (checked against real journals, 2026-10-11)

```
MarketBuy                 Type "liquidoxygen", Type_Localised "Liquid oxygen" (omitted when it
                          equals Type), Count, MarketID
MarketSell                Type, Count, MarketID
ColonisationContribution  MarketID, Contributions [{Name "$LiquidOxygen_name;",
                          Name_Localised, Amount}]
Docked                    StarSystem, StationName, MarketID
                          (a site is "Planetary Construction Site: <name>")
```

`ColonisationContribution` carries no system or station — only the `MarketID` and wherever we were
last docked.

## Checks

```bash
python -m compileall -q .        # CI cannot import load.py
python tests/test_core.py        # no EDMC, no display
python smoke_test.py             # stubs EDMC, drives a whole haul (needs a display; uses the clipboard)
python package.py                # dist/EDHaulAssist-v<version>.zip
```

Regenerate the commodity table when Frontier adds commodities:
`python tools/build_commodities.py`.

## Releasing

A `v*` tag builds the zip and publishes a GitHub Release (`.github/workflows/release.yml`). Bump
`__version__`, add a `CHANGELOG.md` section, commit, then tag. The tag must match `__version__`.
