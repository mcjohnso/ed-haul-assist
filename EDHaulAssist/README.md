# EDHaulAssist

An EDMC plugin that walks you through a hauling plan — for instance, the commodities a colony
facility needs — one run at a time. It does **no planning of its own**: another tool decides what to
buy where and exports it as JSON, and this plugin guides you through it.

For each run it shows the Stops on the in-game overlay, puts the next system on the clipboard, ticks
Stops off as you buy, and ticks the Run off as you deliver. The loop is
**paste → fly → buy → paste → fly → deliver**, with no window switching.

## Install

1. Download the release zip and extract it into EDMC's plugins folder, so you end up with
   `plugins/EDHaulAssist/load.py`:
   - Windows: `%LOCALAPPDATA%\EDMarketConnector\plugins`
   - macOS: `~/Library/Application Support/EDMarketConnector/plugins`
   - Linux: `~/.local/share/EDMarketConnector/plugins`
2. Restart EDMC.

The in-game overlay is optional and comes from a separate plugin —
[EDMCOverlay](https://github.com/inorton/EDMCOverlay) or
[EDMCModernOverlay](https://github.com/SweetJonnySauce/EDMCModernOverlay). Without one, everything
still shows in the EDMC window.

## Using it

1. **Load plan...** and pick the JSON file your planning tool exported.
2. Set the **Sell system** — type it, or dock at the delivery point and press **Here**, which also
   captures the station. With a station captured, only deliveries at that station count; press
   **Clear** to accept any station in the system.
3. Press **Start**.

From then on:

- The system of the next Stop is on the clipboard. Paste it into the galaxy map.
- Buying a Stop's commodities in the planned quantity ticks the Stop off and copies the next one.
- When every Stop is done, the **Sell system** is copied instead.
- Delivering the cargo completes the Run and copies the first Stop of the next.

```
HAUL  Run 3/12   ETA 1h 40m
[x] Sol / Daedalus
      Steel 400   Titanium 320
>>  Alpha Centauri / Hutton Orbital
      Aluminium 0/200
[ ] Barnard's Star / Miller Depot
      Copper 0/150
then SELL -> HIP 1234
```

`[x]` done, `>>` where to go now, `[ ]` still to do, `[-]` skipped. The block is orange while you
are buying and green once you are heading for the Sell Location.

**Stop** pauses everything: no overlay, nothing copied, nothing tracked, and the clock stops, so a
break does not distort the ETA. Progress is saved as you go and survives restarting EDMC — which
always comes back stopped, so press **Start** to pick up where you left off.

### Seeing the whole plan

**Show plan** opens a separate window listing every Run and Stop: Runs behind you in green with how
each ended (`done in 31m`, `short: Titanium 20`, `closed by hand`, `delivered before joining`), the
Run you are on in orange exactly as the overlay shows it, and the Runs still to come as planned. It
updates as you go and follows you to each new Run. It is an EDMC window only — nothing extra is drawn
in the game.

### What counts

- **A purchase** is a `MarketBuy` of a commodity the current Run wants. It is credited to the Stop
  you are docked at; if you bought it somewhere the plan did not mention, it is credited to the first
  Stop still wanting that commodity. Anything beyond what the Run asks for is ignored.
- **Selling something back** away from the Sell Location undoes that much of the purchase.
- **A delivery** is a `MarketSell`, or a `ColonisationContribution` to a construction site, at the
  Sell Location. Transferring cargo to a fleet carrier's hold is not counted.
- **A Run is complete** when you have delivered at least once and nothing the plugin saw you buy is
  still aboard.

### Starting part-way through a plan

- **Some runs are already flown.** Press **Go to run...** and enter the run you are on. The runs you
  jump past are recorded as delivered in full — not as shortfall — and take no part in the average
  run time or tonnes per hour.
- **Cargo is already in the hold.** Just press **Start**. Anything aboard that the current Run wants
  and the plugin has not already counted is credited, filling Stops in plan order, so guidance begins
  at the first Stop you still need. The EDMC window says what it found
  (`Already aboard: Steel 400, Titanium 320.`). The same happens on every Start, so something bought
  during a break is picked up too.

Crediting from the hold counts any matching cargo, including leftovers from an earlier Run, and it
only ever adds: a hold with *less* than expected changes nothing.

### When things do not go to plan

| Button | Use it when |
|---|---|
| **Skip stop** | A market has run dry. Guidance moves on; what you did buy there still counts. |
| **Next run** | The Run will never complete by itself — the site took less than you carried, or you delivered while stopped. |
| **Go to run...** | You are joining part-way (above), or you pressed **Next run** by mistake: going back discards what was recorded from that run on. |
| **Copy system** | You have overwritten the clipboard and want the current target back. |
| **Reset** | Start the plan again from Run 1. |

If you deliver without finishing every Stop, the Run ends there and whatever was planned but not
delivered is recorded as **shortfall**. The plugin does not re-plan it; the figure is there so you
know what to ask your planning tool for next.

### Statistics

Below the run: runs done, tonnes delivered against the plan, active time, average time per run,
tonnes per hour, and an **ETA** — average run time × runs remaining, less the time already spent on
the current one. There is no ETA until one Run has completed, because there is nothing to base it on.

## The plan file

```json
{
  "schema": 1,
  "name": "Orbital outpost - HIP 1234",
  "runs": [
    {
      "stops": [
        {
          "system": "Sol",
          "station": "Daedalus",
          "commodities": [
            {"name": "Steel", "quantity": 400},
            {"name": "Titanium", "quantity": 320}
          ]
        }
      ]
    }
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `schema` | yes | Must be `1`. |
| `name` | no | Shown in the EDMC window. |
| `runs` | yes | At least one. Each is one round trip, flown in the order listed. |
| `runs[].stops` | yes | At least one. Guidance follows the order listed; you may visit them in any order. |
| `stops[].system` | yes | Exactly as the galaxy map spells it — this is what gets pasted. |
| `stops[].station` | yes | The station name as the game shows it. |
| `stops[].commodities` | yes | At least one `{name, quantity}`. |
| `commodities[].name` | yes | The in-game name (`Land Enrichment Systems`) or the journal symbol (`terrainenrichmentsystems`); case and spacing do not matter. |
| `commodities[].quantity` | yes | Whole tonnes, above zero. |

The Sell Location is deliberately **not** in the file; it is set in the plugin. A file that cannot be
followed is refused with a message saying which run and stop is at fault. An unrecognised commodity
name is only a warning — it may simply be newer than the plugin's table.

The repository has a complete example in `examples/sample_plan.json` and a JSON Schema for the
format in `examples/haul_plan.schema.json`.

## Settings

*File → Settings → Haul Assist*: turn the overlay off, move it (top-left by default), or change its
text size.

## Licence

MIT. See `LICENSE`.
