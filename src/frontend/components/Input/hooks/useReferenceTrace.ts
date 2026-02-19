import { useCallback, useRef } from 'react';

interface TraceSample {
  lapPct: number;
  throttle: number;
  brake: number;
}

interface CompletedLap {
  samples: TraceSample[];
  startedAt: number;
  endedAt: number;
}

interface LiveInputSample {
  lapPct?: number;
  sessionTime?: number;
  throttle?: number;
  brake?: number;
}

const MIN_LAP_TIME_S = 20;
const MAX_LAP_TIME_S = 300;
const MIN_SAMPLES = 100;
const WRAP_THRESHOLD = 0.95;

export const useReferenceTrace = () => {
  const currentLap = useRef<TraceSample[]>([]);
  const currentLapStartTime = useRef<number | null>(null);
  const lastLapPct = useRef<number | null>(null);
  const bestLap = useRef<CompletedLap | null>(null);

  const updateReferenceTrace = useCallback((sample: LiveInputSample) => {
    const lapPct = sample.lapPct;
    const sessionTime = sample.sessionTime;

    if (lapPct === undefined || sessionTime === undefined) {
      return;
    }

    if (currentLapStartTime.current === null) {
      currentLapStartTime.current = sessionTime;
    }

    const normalizedLapPct = lapPct < 0 ? 0 : lapPct > 1 ? 1 : lapPct;

    if (
      lastLapPct.current !== null &&
      lastLapPct.current > WRAP_THRESHOLD &&
      normalizedLapPct < 0.05
    ) {
      const lapStart = currentLapStartTime.current ?? sessionTime;
      const lapTime = sessionTime - lapStart;
      const lapSamples = currentLap.current;

      if (
        lapSamples.length >= MIN_SAMPLES &&
        lapTime >= MIN_LAP_TIME_S &&
        lapTime <= MAX_LAP_TIME_S
      ) {
        if (
          !bestLap.current ||
          lapTime < bestLap.current.endedAt - bestLap.current.startedAt
        ) {
          bestLap.current = {
            samples: [...lapSamples],
            startedAt: lapStart,
            endedAt: sessionTime,
          };
        }
      }

      currentLap.current = [];
      currentLapStartTime.current = sessionTime;
    }

    currentLap.current.push({
      lapPct: normalizedLapPct,
      throttle: sample.throttle ?? 0,
      brake: sample.brake ?? 0,
    });

    if (currentLap.current.length > 8000) {
      currentLap.current.shift();
    }

    lastLapPct.current = normalizedLapPct;
  }, []);

  const getReferenceSample = useCallback((lapPct?: number) => {
    if (
      lapPct === undefined ||
      !bestLap.current ||
      bestLap.current.samples.length === 0
    ) {
      return undefined;
    }

    const samples = bestLap.current.samples;
    const normalized = lapPct < 0 ? 0 : lapPct > 1 ? 1 : lapPct;

    let nearest = samples[0];
    let nearestDistance = Math.abs(samples[0].lapPct - normalized);

    for (let i = 1; i < samples.length; i += 1) {
      const dist = Math.abs(samples[i].lapPct - normalized);
      if (dist < nearestDistance) {
        nearest = samples[i];
        nearestDistance = dist;
      }
    }

    return nearest;
  }, []);

  const hasReferenceLap = useCallback(() => bestLap.current !== null, []);

  return {
    updateReferenceTrace,
    getReferenceSample,
    hasReferenceLap,
  };
};
