# IBT Overlay Standalone (TypeScript)

This folder contains a standalone TypeScript app that ports the core idea of `nishizumi_ibt_overlay.py` into the repository's primary language (TypeScript).

## What it does

- Loads a **reference** `.ibt` file and a **candidate** `.ibt` file.
- Extracts the longest lap from each file (same strategy as the Python script).
- Detects brake/lift/power events from throttle + brake traces.
- Compares candidate input telemetry against reference telemetry and prints:
  - RMSE for throttle, brake, speed, steering, and gear.
  - Brake-point distance deltas in meters.

## Why this helps

This avoids Python tracer issues and keeps the telemetry workflow in TypeScript, matching the rest of this repository.

## Usage

From repository root:

```bash
npx tsx standalone/ibt-overlay-ts/app.ts <reference.ibt> <candidate.ibt>
```

Example:

```bash
npx tsx standalone/ibt-overlay-ts/app.ts "C:/laps/ref.ibt" "C:/laps/new.ibt"
```

## Notes

- This is intentionally standalone and isolated in a new folder.
- It currently focuses on `.ibt` to `.ibt` lap comparison (reference vs candidate).
- If you want, this can be extended into a visual desktop overlay window next (Electron window + existing input trace components).
