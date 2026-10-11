# Changelog

All notable changes to EDHaulAssist. Versions follow [SemVer](https://semver.org/);
`__version__` in `EDHaulAssist/load.py` is authoritative and the git tag must match it.

## [0.1.0] - unreleased

### Added
- Import a haul plan (schema 1): Runs, each a list of Stops, each a station with commodities and
  quantities to buy.
- Guidance for the current Run on the in-game overlay and in the EDMC window, with the next system
  copied to the clipboard whenever the target changes.
- Stops tick off from `MarketBuy`; Runs tick off from `MarketSell` or `ColonisationContribution` at
  the configured Sell Location (a system, optionally narrowed to one station).
- Start/Stop: nothing is drawn, copied, tracked or timed while stopped.
- Progress survives restarting EDMC.
- Statistics: runs and tonnes done, active time, average run time, tonnes per hour, shortfall, ETA.
- Skip stop, Next run and Reset for when a haul does not go to plan.
- **Show plan**: a pop-out EDMC window listing every Run and Stop with its status.
- Joining a plan part-way: **Go to run...** marks the runs jumped past as already delivered, and
  **Start** credits cargo already in the hold to the current run's Stops.
