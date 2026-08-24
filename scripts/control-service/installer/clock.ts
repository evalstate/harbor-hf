import { performance } from "node:perf_hooks";

export interface InstallerClock {
  monotonicMilliseconds(): number;
  sleep(milliseconds: number, signal?: AbortSignal): Promise<void>;
}

export class SystemInstallerClock implements InstallerClock {
  monotonicMilliseconds(): number {
    return performance.now();
  }

  async sleep(milliseconds: number, signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) return;
    await new Promise<void>((resolve) => {
      const finish = () => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", finish);
        resolve();
      };
      const timer = setTimeout(finish, milliseconds);
      signal?.addEventListener("abort", finish, { once: true });
    });
  }
}
