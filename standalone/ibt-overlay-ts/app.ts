import * as path from 'node:path';
import { parseIbtFile, type TelemetryFrame } from './ibtParser';

interface LapData {
  lapPct: number[];
  lapDist: number[];
  throttle: number[];
  brake: number[];
  speed: number[];
  steering: number[];
  gear: number[];
  trackLengthM: number;
}

interface RefEvent {
  kind: 'brake' | 'lift' | 'power';
  lapPct: number;
}

const BRAKE_THRESHOLD = 0.1;
const LIFT_THRESHOLD = 0.2;
const POWER_THRESHOLD = 0.7;

function normalizePct(pct: number): number {
  if (pct > 1.5) return pct / 100;
  return pct;
}

function frameValue(frame: TelemetryFrame, key: keyof TelemetryFrame): number {
  return typeof frame[key] === 'number' ? (frame[key] as number) : 0;
}

function extractBestLap(frames: TelemetryFrame[]): LapData {
  const pctAll = frames.map((f) => normalizePct(frameValue(f, 'LapDistPct')));

  let start = 0;
  const segments: { start: number; end: number }[] = [];
  for (let i = 1; i < pctAll.length; i += 1) {
    if (pctAll[i] < pctAll[i - 1] - 0.5) {
      segments.push({ start, end: i });
      start = i;
    }
  }
  segments.push({ start, end: pctAll.length });

  const best = segments.reduce((a, b) =>
    b.end - b.start > a.end - a.start ? b : a
  );

  const slice = frames.slice(best.start, best.end);
  const lapPct = pctAll.slice(best.start, best.end);
  const lapDist = slice.map((f) => frameValue(f, 'LapDist'));
  const throttle = slice.map((f) => frameValue(f, 'Throttle'));
  const brake = slice.map((f) => frameValue(f, 'Brake'));
  const speed = slice.map((f) => frameValue(f, 'Speed'));
  const steering = slice.map((f) => frameValue(f, 'SteeringWheelAngle'));
  const gear = slice.map((f) => Math.round(frameValue(f, 'Gear')));

  const trackLengthM = lapDist.length > 0 ? Math.max(...lapDist) : 5000;

  return {
    lapPct,
    lapDist,
    throttle,
    brake,
    speed,
    steering,
    gear,
    trackLengthM,
  };
}

function interpolate(seriesX: number[], seriesY: number[], x: number): number {
  if (seriesX.length === 0) return 0;
  if (x <= seriesX[0]) return seriesY[0];
  if (x >= seriesX[seriesX.length - 1]) return seriesY[seriesY.length - 1];

  let lo = 0;
  let hi = seriesX.length - 1;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (seriesX[mid] < x) lo = mid + 1;
    else hi = mid - 1;
  }

  const i = Math.max(1, lo);
  const x0 = seriesX[i - 1];
  const x1 = seriesX[i];
  const y0 = seriesY[i - 1];
  const y1 = seriesY[i];
  if (x1 === x0) return y0;
  const t = (x - x0) / (x1 - x0);
  return y0 + (y1 - y0) * t;
}

function buildEvents(lap: LapData): RefEvent[] {
  const out: RefEvent[] = [];
  let inBrake = false;
  let inLift = false;

  for (let i = 1; i < lap.lapPct.length; i += 1) {
    const brake = lap.brake[i];
    const throttle = lap.throttle[i];
    const prevBrake = lap.brake[i - 1];
    const prevThrottle = lap.throttle[i - 1];

    if (!inBrake && prevBrake < BRAKE_THRESHOLD && brake >= BRAKE_THRESHOLD) {
      out.push({ kind: 'brake', lapPct: lap.lapPct[i] });
      inBrake = true;
      inLift = false;
    }

    if (inBrake && brake < BRAKE_THRESHOLD * 0.5) {
      inBrake = false;
    }

    if (
      !inBrake &&
      !inLift &&
      prevThrottle >= LIFT_THRESHOLD &&
      throttle < LIFT_THRESHOLD &&
      brake < BRAKE_THRESHOLD
    ) {
      out.push({ kind: 'lift', lapPct: lap.lapPct[i] });
      inLift = true;
    }

    if (inLift && throttle > LIFT_THRESHOLD * 1.2) {
      inLift = false;
    }

    if (
      (inBrake || inLift) &&
      prevThrottle < POWER_THRESHOLD &&
      throttle >= POWER_THRESHOLD
    ) {
      out.push({ kind: 'power', lapPct: lap.lapPct[i] });
    }
  }

  return out;
}

function pctToMeters(pct: number, trackLengthM: number): number {
  return pct * trackLengthM;
}

function rms(values: number[]): number {
  if (values.length === 0) return 0;
  const s = values.reduce((acc, v) => acc + v * v, 0);
  return Math.sqrt(s / values.length);
}

function main() {
  const [, , referencePathRaw, candidatePathRaw] = process.argv;
  if (!referencePathRaw || !candidatePathRaw) {
    console.error(
      'Usage: npx tsx standalone/ibt-overlay-ts/app.ts <reference.ibt> <candidate.ibt>'
    );
    process.exit(1);
  }

  const referencePath = path.resolve(referencePathRaw);
  const candidatePath = path.resolve(candidatePathRaw);

  const referenceIbt = parseIbtFile(referencePath);
  const candidateIbt = parseIbtFile(candidatePath);

  const refLap = extractBestLap(referenceIbt.frames);
  const candLap = extractBestLap(candidateIbt.frames);

  const n = Math.min(refLap.lapPct.length, 1500);
  const throttleErrors: number[] = [];
  const brakeErrors: number[] = [];
  const speedErrors: number[] = [];
  const steerErrors: number[] = [];
  const gearErrors: number[] = [];

  for (let i = 0; i < n; i += 1) {
    const pct = refLap.lapPct[Math.floor((i / n) * (refLap.lapPct.length - 1))];

    throttleErrors.push(
      interpolate(candLap.lapPct, candLap.throttle, pct) -
        interpolate(refLap.lapPct, refLap.throttle, pct)
    );
    brakeErrors.push(
      interpolate(candLap.lapPct, candLap.brake, pct) -
        interpolate(refLap.lapPct, refLap.brake, pct)
    );
    speedErrors.push(
      interpolate(candLap.lapPct, candLap.speed, pct) -
        interpolate(refLap.lapPct, refLap.speed, pct)
    );
    steerErrors.push(
      interpolate(candLap.lapPct, candLap.steering, pct) -
        interpolate(refLap.lapPct, refLap.steering, pct)
    );
    gearErrors.push(
      interpolate(candLap.lapPct, candLap.gear, pct) -
        interpolate(refLap.lapPct, refLap.gear, pct)
    );
  }

  const refEvents = buildEvents(refLap).filter((e) => e.kind === 'brake');
  const eventReport = refEvents.slice(0, 20).map((event, index) => {
    const refM = pctToMeters(event.lapPct, refLap.trackLengthM);
    const candidateBrake = buildEvents(candLap)
      .filter((e) => e.kind === 'brake')
      .reduce((closest, next) => {
        const closestDelta = Math.abs(closest.lapPct - event.lapPct);
        const nextDelta = Math.abs(next.lapPct - event.lapPct);
        return nextDelta < closestDelta ? next : closest;
      });

    const candidateM = pctToMeters(candidateBrake.lapPct, candLap.trackLengthM);
    const deltaM = candidateM - refM;
    return `${index + 1}. brake @ ${refM.toFixed(1)}m | candidate ${candidateM.toFixed(1)}m | Δ ${deltaM >= 0 ? '+' : ''}${deltaM.toFixed(1)}m`;
  });

  console.log('=== Nishizumi IBT Overlay (TypeScript Standalone) ===');
  console.log(`Reference: ${referencePath}`);
  console.log(`Candidate: ${candidatePath}`);
  console.log(
    `Reference frames: ${referenceIbt.frameCount} @ ${referenceIbt.tickRate}Hz`
  );
  console.log(
    `Candidate frames: ${candidateIbt.frameCount} @ ${candidateIbt.tickRate}Hz`
  );
  console.log('');
  console.log('Overall trace deltas (candidate vs reference):');
  console.log(`- Throttle RMSE: ${rms(throttleErrors).toFixed(4)}`);
  console.log(`- Brake RMSE: ${rms(brakeErrors).toFixed(4)}`);
  console.log(`- Speed RMSE: ${rms(speedErrors).toFixed(3)} m/s`);
  console.log(`- Steering RMSE: ${rms(steerErrors).toFixed(3)} rad`);
  console.log(`- Gear RMSE: ${rms(gearErrors).toFixed(3)} gears`);
  console.log('');
  console.log('Brake-point comparison (first 20):');
  if (eventReport.length === 0) {
    console.log('- No brake events detected.');
  } else {
    for (const line of eventReport) console.log(`- ${line}`);
  }
}

main();
