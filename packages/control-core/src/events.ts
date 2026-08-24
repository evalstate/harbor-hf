export interface ControlEvent {
  id: string;
  type: string;
  occurred_at: string;
  data: Record<string, unknown>;
}

export type EventListener = (event: ControlEvent) => void;

export function eventCursor(epoch: string, order: number): string {
  if (!epoch || epoch.includes("\u0000") || !Number.isSafeInteger(order) || order < 1)
    throw new Error("invalid event cursor fields");
  return Buffer.from(`v2\u0000${epoch}\u0000${order}`, "utf8").toString("base64url");
}

export function decodeEventCursor(cursor: string): {
  epoch: string;
  order: number;
} {
  const decoded = Buffer.from(cursor, "base64url").toString("utf8");
  const fields = decoded.split("\u0000");
  const order = Number(fields[2]);
  if (
    fields.length !== 3 ||
    fields[0] !== "v2" ||
    !fields[1] ||
    !/^\d+$/.test(fields[2] ?? "") ||
    !Number.isSafeInteger(order) ||
    order < 1
  )
    throw new Error("invalid event cursor");
  return { epoch: fields[1], order };
}

export class EventBus {
  private readonly listeners = new Set<EventListener>();

  publish(event: ControlEvent): void {
    for (const listener of this.listeners) listener(event);
  }

  subscribe(listener: EventListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  listenerCount(): number {
    return this.listeners.size;
  }
}
