import fs from "node:fs/promises";
import process from "node:process";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

const [, , browser, url, outputPath] = process.argv;

if (!browser || !url || !outputPath) {
  process.exit(2);
}

const DEBUG_PORT = 9222;
const WAIT_MS = 8000;
const DEADLINE_MS = 25000;

const browserProcess = spawn(
  browser,
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-software-rasterizer",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
    "--hide-scrollbars",
    "--mute-audio",
    "--no-first-run",
    "--window-size=1600,2000",
    `--remote-debugging-port=${DEBUG_PORT}`,
    url,
  ],
  {
    stdio: ["ignore", "ignore", "ignore"],
    detached: true,
  },
);

let cleaned = false;

function cleanup(code) {
  if (cleaned) return;
  cleaned = true;
  try {
    process.kill(-browserProcess.pid, "SIGKILL");
  } catch {}
  process.exit(code);
}

async function fetchJson(fetchUrl) {
  const response = await fetch(fetchUrl);
  if (!response.ok) {
    throw new Error(String(response.status));
  }
  return response.json();
}

async function waitForTarget() {
  const started = Date.now();
  while (Date.now() - started < DEADLINE_MS) {
    try {
      const targets = await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
      const pageTarget = targets.find((target) => target.type === "page" && target.url && target.url !== "about:blank");
      if (pageTarget?.webSocketDebuggerUrl) {
        return pageTarget.webSocketDebuggerUrl;
      }
    } catch {}
    await delay(250);
  }
  throw new Error("target-timeout");
}

function sendCommand(socket, id, method, params = {}) {
  socket.send(JSON.stringify({ id, method, params }));
}

async function captureDom(wsUrl) {
  const socket = new WebSocket(wsUrl);
  let commandId = 0;

  return await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      socket.close();
      reject(new Error("capture-timeout"));
    }, DEADLINE_MS);

    socket.addEventListener("open", async () => {
      await delay(WAIT_MS);
      commandId += 1;
      sendCommand(socket, commandId, "Runtime.evaluate", {
        expression: "document.documentElement.outerHTML",
        returnByValue: true,
      });
    });

    socket.addEventListener("message", (event) => {
      try {
        const message = JSON.parse(event.data.toString());
        if (message.id !== commandId) {
          return;
        }
        clearTimeout(timeout);
        socket.close();
        const html = message.result?.result?.value;
        if (typeof html === "string" && html.trim()) {
          resolve(html);
        } else {
          reject(new Error("empty-dom"));
        }
      } catch (error) {
        clearTimeout(timeout);
        socket.close();
        reject(error);
      }
    });

    socket.addEventListener("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
  });
}

try {
  const wsUrl = await waitForTarget();
  const html = await captureDom(wsUrl);
  await fs.writeFile(outputPath, html, "utf8");
  cleanup(0);
} catch {
  cleanup(1);
}
