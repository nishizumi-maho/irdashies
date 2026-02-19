import * as d3 from 'd3';
import { useEffect, useRef } from 'react';
import { getColor } from '@irdashies/utils/colors';
import { useReferenceTrace } from '../hooks/useReferenceTrace';

const BRAKE_COLOR = getColor('red');
const BRAKE_ABS_COLOR = getColor('yellow', 500);
const THROTTLE_COLOR = getColor('green');
const CLUTCH_COLOR = getColor('blue');
const STEER_COLOR = getColor('slate', 300);
const REFERENCE_BRAKE_COLOR = '#fca5a5';
const REFERENCE_THROTTLE_COLOR = '#86efac';

export interface InputTraceProps {
  input: {
    clutch?: number;
    brake?: number;
    brakeAbsActive?: boolean;
    throttle?: number;
    steer?: number;
    lapDistPct?: number;
    sessionTime?: number;
  };
  settings?: {
    includeClutch?: boolean;
    includeThrottle?: boolean;
    includeBrake?: boolean;
    includeAbs?: boolean;
    includeSteer?: boolean;
    strokeWidth?: number;
    maxSamples?: number;
    referenceOverlay?: {
      enabled?: boolean;
    };
  };
}

export const InputTrace = ({ input, settings }: InputTraceProps) => {
  const {
    includeClutch = true,
    includeThrottle = true,
    includeBrake = true,
    includeAbs = true,
    includeSteer = true,
    strokeWidth = 3,
    maxSamples = 400,
    referenceOverlay,
  } = settings ?? {};
  const svgRef = useRef<SVGSVGElement>(null);
  const rafRef = useRef<number | null>(null);
  const { width, height } = { width: 400, height: 100 };
  const { updateReferenceTrace, getReferenceSample, hasReferenceLap } =
    useReferenceTrace();

  const bufferSize = maxSamples;

  const brakeArray = useRef<number[]>(
    Array.from({ length: bufferSize }, () => 0)
  );
  const brakeABSArray = useRef<boolean[]>(
    Array.from({ length: bufferSize }, () => false)
  );
  const throttleArray = useRef<number[]>(
    Array.from({ length: bufferSize }, () => 0)
  );
  const steerArray = useRef<number[]>(
    Array.from({ length: bufferSize }, () => 0.5)
  );
  const clutchArray = useRef<number[]>(
    Array.from({ length: bufferSize }, () => 0)
  );

  const writeIndex = useRef<number>(0);

  useEffect(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);

    rafRef.current = requestAnimationFrame(() => {
      const idx = writeIndex.current;

      updateReferenceTrace({
        lapPct: input.lapDistPct,
        sessionTime: input.sessionTime,
        throttle: input.throttle,
        brake: input.brake,
      });

      if (includeThrottle) throttleArray.current[idx] = input.throttle ?? 0;
      if (includeBrake) {
        brakeArray.current[idx] = input.brake ?? 0;
        if (includeAbs)
          brakeABSArray.current[idx] = input.brakeAbsActive ?? false;
      }
      if (includeClutch) clutchArray.current[idx] = input.clutch ?? 0;
      if (includeSteer) {
        const angleRad = input.steer ?? 0;
        steerArray.current[idx] = Math.max(
          0,
          Math.min(1, angleRad / (2 * Math.PI) + 0.5)
        );
      }

      writeIndex.current = (idx + 1) % bufferSize;

      const valueArrayWithColors = [];
      if (includeSteer)
        valueArrayWithColors.push({
          values: steerArray.current,
          color: STEER_COLOR,
          isCentered: true,
        });
      if (includeClutch)
        valueArrayWithColors.push({
          values: clutchArray.current,
          color: CLUTCH_COLOR,
        });
      if (includeThrottle)
        valueArrayWithColors.push({
          values: throttleArray.current,
          color: THROTTLE_COLOR,
        });
      if (includeBrake) {
        valueArrayWithColors.push({
          values: brakeArray.current,
          color: BRAKE_COLOR,
          absStates: includeAbs ? brakeABSArray.current : undefined,
          absColor: includeAbs ? BRAKE_ABS_COLOR : undefined,
        });
      }

      const currentReference = getReferenceSample(input.lapDistPct);

      drawGraph(
        svgRef.current,
        valueArrayWithColors,
        width,
        height,
        strokeWidth,
        bufferSize,
        writeIndex.current,
        referenceOverlay?.enabled ?? true,
        hasReferenceLap(),
        currentReference
      );
    });

    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [
    input,
    includeThrottle,
    includeBrake,
    includeAbs,
    includeSteer,
    includeClutch,
    bufferSize,
    width,
    height,
    strokeWidth,
    updateReferenceTrace,
    getReferenceSample,
    hasReferenceLap,
    referenceOverlay?.enabled,
  ]);

  return (
    <svg
      ref={svgRef}
      width={'100%'}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
    ></svg>
  );
};

function getCircularValue<T>(
  array: T[],
  logicalIndex: number,
  writeIndex: number,
  bufferSize: number
): T {
  const physicalIndex = (writeIndex + logicalIndex) % bufferSize;
  return array[physicalIndex];
}

let cachedIndices: number[] = [];
function getIndices(bufferSize: number): number[] {
  if (cachedIndices.length !== bufferSize)
    cachedIndices = Array.from({ length: bufferSize }, (_, i) => i);
  return cachedIndices;
}

function drawGraph(
  svgElement: SVGSVGElement | null,
  valueArrayWithColors: {
    values: number[] | boolean[];
    color: string;
    absStates?: boolean[];
    absColor?: string;
    isCentered?: boolean;
  }[],
  width: number,
  height: number,
  strokeWidth: number,
  bufferSize: number,
  writeIndex: number,
  showReferenceOverlay: boolean,
  hasReferenceLap: boolean,
  currentReference?: { throttle: number; brake: number }
) {
  if (!svgElement || valueArrayWithColors.length === 0) return;

  const svg = d3.select(svgElement);
  svg.selectAll('*').remove();

  const scaleMargin = 0.05;
  const xScale = d3
    .scaleLinear()
    .domain([0, bufferSize - 1])
    .range([0, width]);
  const yScale = d3
    .scaleLinear()
    .domain([0 - scaleMargin, 1 + scaleMargin])
    .range([height, 0]);

  drawYAxis(svg, yScale, width);

  valueArrayWithColors.forEach(
    ({ values, color, absStates, absColor, isCentered }) => {
      if (absStates && absColor) {
        drawABSAwareLine(
          svg,
          values as number[],
          absStates,
          xScale,
          yScale,
          color,
          absColor,
          strokeWidth,
          writeIndex,
          bufferSize
        );
      } else if (isCentered) {
        drawCenteredLine(
          svg,
          values as number[],
          xScale,
          yScale,
          color,
          height,
          writeIndex,
          bufferSize
        );
      } else {
        drawLine(
          svg,
          values as number[],
          xScale,
          yScale,
          color,
          strokeWidth,
          writeIndex,
          bufferSize
        );
      }
    }
  );

  if (showReferenceOverlay && hasReferenceLap && currentReference) {
    drawReferenceOverlay(svg, yScale, width, currentReference);
  }
}

function drawReferenceOverlay(
  svg: d3.Selection<SVGSVGElement, unknown, null, undefined>,
  yScale: d3.ScaleLinear<number, number>,
  width: number,
  reference: { throttle: number; brake: number }
) {
  const overlays = [
    {
      value: reference.throttle,
      color: REFERENCE_THROTTLE_COLOR,
      label: 'Ref T',
    },
    { value: reference.brake, color: REFERENCE_BRAKE_COLOR, label: 'Ref B' },
  ];

  overlays.forEach(({ value, color, label }) => {
    const y = yScale(Math.max(0, Math.min(1, value)));
    svg
      .append('line')
      .attr('x1', width * 0.82)
      .attr('x2', width)
      .attr('y1', y)
      .attr('y2', y)
      .attr('stroke', color)
      .attr('stroke-width', 1.5)
      .attr('stroke-dasharray', '3,2')
      .attr('opacity', 0.9);

    svg
      .append('text')
      .attr('x', width * 0.81)
      .attr('y', y - 2)
      .attr('fill', color)
      .attr('font-size', 8)
      .attr('text-anchor', 'end')
      .text(label);
  });
}

function drawYAxis(
  svg: d3.Selection<SVGSVGElement, unknown, null, undefined>,
  yScale: d3.ScaleLinear<number, number>,
  width: number
) {
  const yAxis = d3
    .axisLeft(yScale)
    .tickValues(d3.range(0, 1.25, 0.25))
    .tickFormat(() => '');

  svg
    .append('g')
    .call(yAxis)
    .selectAll('line')
    .attr('x2', width)
    .attr('stroke', '#666')
    .attr('stroke-dasharray', '2,2');
  svg.select('.domain').remove();
}

function drawLine(
  svg: d3.Selection<SVGSVGElement, unknown, null, undefined>,
  valueArray: number[],
  xScale: d3.ScaleLinear<number, number>,
  yScale: d3.ScaleLinear<number, number>,
  color: string,
  strokeWidth: number,
  writeIndex: number,
  bufferSize: number
) {
  const line = d3
    .line<number>()
    .x((i) => xScale(i))
    .y((i) =>
      yScale(
        Math.max(
          0,
          Math.min(1, getCircularValue(valueArray, i, writeIndex, bufferSize))
        )
      )
    )
    .curve(d3.curveBasis);

  svg
    .append('g')
    .append('path')
    .datum(getIndices(bufferSize))
    .attr('fill', 'none')
    .attr('stroke', color)
    .attr('stroke-width', strokeWidth)
    .attr('d', line);
}

function drawCenteredLine(
  svg: d3.Selection<SVGSVGElement, unknown, null, undefined>,
  valueArray: number[],
  xScale: d3.ScaleLinear<number, number>,
  _yScale: d3.ScaleLinear<number, number>,
  color: string,
  height: number,
  writeIndex: number,
  bufferSize: number
) {
  const line = d3
    .line<number>()
    .x((i) => xScale(i))
    .y((i) => {
      const d = getCircularValue(valueArray, i, writeIndex, bufferSize);
      return height / 2 - (d - 0.5) * height;
    })
    .curve(d3.curveBasis);

  svg
    .append('g')
    .append('path')
    .datum(getIndices(bufferSize))
    .attr('fill', 'none')
    .attr('stroke', color)
    .attr('stroke-width', 1)
    .attr('d', line);
}

function drawABSAwareLine(
  svg: d3.Selection<SVGSVGElement, unknown, null, undefined>,
  valueArray: number[],
  absStates: boolean[],
  xScale: d3.ScaleLinear<number, number>,
  yScale: d3.ScaleLinear<number, number>,
  normalColor: string,
  absColor: string,
  strokeWidth: number,
  writeIndex: number,
  bufferSize: number
) {
  const segments: {
    values: { value: number; index: number }[];
    isABS: boolean;
  }[] = [];
  let currentSegment: { value: number; index: number }[] = [];
  let currentIsABS = getCircularValue(absStates, 0, writeIndex, bufferSize);

  for (let i = 0; i < bufferSize; i++) {
    const isABS = getCircularValue(absStates, i, writeIndex, bufferSize);
    const value = getCircularValue(valueArray, i, writeIndex, bufferSize);

    if (i === 0 || isABS === currentIsABS) {
      currentSegment.push({ value, index: i });
    } else {
      if (currentSegment.length > 0)
        segments.push({ values: [...currentSegment], isABS: currentIsABS });
      currentSegment = [
        currentSegment[currentSegment.length - 1],
        { value, index: i },
      ];
      currentIsABS = isABS;
    }
  }

  if (currentSegment.length > 0)
    segments.push({ values: currentSegment, isABS: currentIsABS });

  segments.forEach((segment) => {
    if (segment.values.length > 1) {
      const line = d3
        .line<{ value: number; index: number }>()
        .x((d) => xScale(d.index))
        .y((d) => yScale(Math.max(0, Math.min(1, d.value))))
        .curve(d3.curveBasis);

      svg
        .append('g')
        .append('path')
        .datum(segment.values)
        .attr('fill', 'none')
        .attr('stroke', segment.isABS ? absColor : normalColor)
        .attr(
          'stroke-width',
          segment.isABS ? Math.round(strokeWidth * 1.67) : strokeWidth
        )
        .attr('d', line);
    }
  });
}
