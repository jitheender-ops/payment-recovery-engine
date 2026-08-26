export const IST_OFFSET_MINUTES = 330;

export function istWall(instant: Date): Date {
  return new Date(instant.getTime() + IST_OFFSET_MINUTES * 60_000);
}

export function fromIstWall(wall: Date): Date {
  return new Date(wall.getTime() - IST_OFFSET_MINUTES * 60_000);
}

export function istHour(instant: Date): number {
  return istWall(instant).getUTCHours();
}

export function istDayOfWeek(instant: Date): number {
  return (istWall(instant).getUTCDay() + 6) % 7;
}

export interface BlackoutSettings {
  retryBlackoutStartHour: number;
  retryBlackoutEndHour: number;
}

export function isInBlackout(hour: number, settings: BlackoutSettings): boolean {
  const { retryBlackoutStartHour: start, retryBlackoutEndHour: end } = settings;
  if (start > end) return hour >= start || hour < end;
  return start <= hour && hour < end;
}

export function clampRetryAtOutOfBlackout(
  retryAt: Date,
  settings: BlackoutSettings,
): Date {
  const local = istWall(retryAt);
  if (!isInBlackout(local.getUTCHours(), settings)) {
    return retryAt;
  }

  const end = settings.retryBlackoutEndHour;
  let wake = Date.UTC(
    local.getUTCFullYear(),
    local.getUTCMonth(),
    local.getUTCDate(),
    end % 24,
    5,
    0,
    0,
  );
  if (wake <= local.getTime()) {
    wake += 86_400_000;
  }

  return fromIstWall(new Date(wake));
}
