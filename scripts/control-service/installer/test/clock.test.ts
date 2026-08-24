import { describe, it } from "vitest";
import { SystemInstallerClock } from "../clock.js";

async function bounded<T>(promise: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(
          () => reject(new Error("clock operation did not settle")),
          250,
        );
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

describe("installer system clock", () => {
  it("returns immediately for an already-aborted sleep", async () => {
    const controller = new AbortController();
    controller.abort();
    await bounded(new SystemInstallerClock().sleep(60_000, controller.signal));
  });

  it("cancels an active sleep without leaving the caller blocked", async () => {
    const controller = new AbortController();
    const sleeping = new SystemInstallerClock().sleep(60_000, controller.signal);
    controller.abort();
    await bounded(sleeping);
  });

  it("settles natural and repeated cancelled sleeps", async () => {
    const clock = new SystemInstallerClock();
    await bounded(clock.sleep(1));
    for (let index = 0; index < 10; index += 1) {
      const controller = new AbortController();
      const sleeping = clock.sleep(60_000, controller.signal);
      controller.abort();
      await bounded(sleeping);
    }
  });
});
