# ed-haul-assist

Source repo for **EDHaulAssist**, an [EDMC](https://github.com/EDCD/EDMarketConnector) plugin that
guides you through an imported hauling plan: the current run on the in-game overlay, the next system
on the clipboard, and Stops and Runs ticked off from the journal as you buy and deliver.

The shippable plugin is [`EDHaulAssist/`](EDHaulAssist/) — see
[its README](EDHaulAssist/README.md) for installation, use and the plan file format.

## Layout

```
EDHaulAssist/             the plugin (this is what gets zipped)
  load.py                 EDMC callbacks, the panel, clipboard, heartbeat
  edha_session.py         pure: state machine, journal handling, statistics, display
  edha_plan.py            pure: the plan schema - parse and validate
  edha_store.py           pure: state.json, written atomically
  edha_commodities.py     name resolution (+ generated edha_commodities.json)
  edha_overlay.py         EDMCOverlay / EDMCModernOverlay output
  edha_settings.py        preferences tab
examples/sample_plan.json a plan file to try it with
examples/haul_plan.schema.json  JSON Schema for the plan file
package.py                builds dist/EDHaulAssist-v<version>.zip
smoke_test.py             stubs EDMC and drives a whole haul
tests/test_core.py        pure-logic tests
tools/build_commodities.py  regenerates the commodity table from EDCD/FDevIDs
```

Every helper is `edha_`-prefixed and imported top-level. EDMC puts every installed plugin's folder
on one shared `sys.path`, so a generic `overlay.py` or `utils.py` would silently lose to whichever
plugin imported first. Relative imports do not work either — EDMC imports `load.py` directly, not as
a package.

`edha_session.py`, `edha_plan.py` and `edha_store.py` import nothing from EDMC or tkinter, which is
what lets the test suite fly a whole plan in-process.

## Developing

```bash
python -m compileall -q .        # CI cannot import load.py; this catches syntax errors
python tests/test_core.py        # pure logic, no EDMC
python smoke_test.py             # stubs EDMC, needs a display
python package.py                # dist/EDHaulAssist-v<version>.zip
```

See [CLAUDE.md](CLAUDE.md) for the constraints that are easy to trip over.
