import * as fs from 'node:fs';

const IRSDK_MAX_STRING = 32;
const VAR_HEADER_SIZE = 144;
const HEADER_SIZE = 112;
const DISK_SUB_HEADER_SIZE = 32;

interface ParsedVarHeader {
  type: number;
  offset: number;
  count: number;
  name: string;
}

export interface TelemetryFrame {
  LapDistPct?: number;
  LapDist?: number;
  Throttle?: number;
  Brake?: number;
  Speed?: number;
  SteeringWheelAngle?: number;
  Gear?: number;
  SessionTime?: number;
}

export interface ParsedIbt {
  filePath: string;
  frameCount: number;
  tickRate: number;
  frames: TelemetryFrame[];
}

function readCString(buffer: Buffer, offset: number, maxLen: number): string {
  const end = offset + maxLen;
  let firstNull = offset;
  while (firstNull < end && buffer[firstNull] !== 0) firstNull += 1;
  return buffer.toString('utf8', offset, firstNull);
}

function readVarHeaders(
  buffer: Buffer,
  varHeaderOffset: number,
  numVars: number
): ParsedVarHeader[] {
  const vars: ParsedVarHeader[] = [];
  for (let i = 0; i < numVars; i += 1) {
    const base = varHeaderOffset + i * VAR_HEADER_SIZE;
    vars.push({
      type: buffer.readInt32LE(base),
      offset: buffer.readInt32LE(base + 4),
      count: buffer.readInt32LE(base + 8),
      name: readCString(buffer, base + 16, IRSDK_MAX_STRING),
    });
  }
  return vars;
}

function typeBytes(type: number): number {
  switch (type) {
    case 0:
    case 1:
      return 1;
    case 2:
    case 3:
    case 4:
      return 4;
    case 5:
      return 8;
    default:
      throw new Error(`Unsupported var type ${type}`);
  }
}

function readScalar(buffer: Buffer, offset: number, type: number): number {
  switch (type) {
    case 0:
      return buffer.readInt8(offset);
    case 1:
      return buffer.readUInt8(offset);
    case 2:
    case 3:
      return buffer.readInt32LE(offset);
    case 4:
      return buffer.readFloatLE(offset);
    case 5:
      return buffer.readDoubleLE(offset);
    default:
      throw new Error(`Unsupported var type ${type}`);
  }
}

export function parseIbtFile(filePath: string): ParsedIbt {
  const raw = fs.readFileSync(filePath);

  const tickRate = raw.readInt32LE(8);
  const numVars = raw.readInt32LE(24);
  const varHeaderOffset = raw.readInt32LE(28);
  const bufLen = raw.readInt32LE(36);

  const sessionRecordCount = raw.readInt32LE(
    HEADER_SIZE + DISK_SUB_HEADER_SIZE - 4
  );
  const dataStartOffset = HEADER_SIZE + DISK_SUB_HEADER_SIZE;

  const headers = readVarHeaders(raw, varHeaderOffset, numVars);
  const wanted = new Set([
    'LapDistPct',
    'LapDist',
    'Throttle',
    'Brake',
    'Speed',
    'SteeringWheelAngle',
    'Gear',
    'SessionTime',
  ]);

  const selectedHeaders = headers.filter((h) => wanted.has(h.name));
  const frames: TelemetryFrame[] = [];

  for (let row = 0; row < sessionRecordCount; row += 1) {
    const frame: TelemetryFrame = {};
    const rowBase = dataStartOffset + row * bufLen;

    for (const header of selectedHeaders) {
      if (header.count < 1) continue;
      const absoluteOffset = rowBase + header.offset;
      const value = readScalar(raw, absoluteOffset, header.type);
      (frame as Record<string, number>)[header.name] = value;
      typeBytes(header.type);
    }

    frames.push(frame);
  }

  return {
    filePath,
    frameCount: sessionRecordCount,
    tickRate,
    frames,
  };
}
