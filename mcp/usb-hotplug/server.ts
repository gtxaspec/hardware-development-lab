#!/usr/bin/env bun
/**
 * USB hotplug channel for Claude Code.
 *
 * Monitors udevadm on the bench controller (USB_HOTPLUG_HOST) via SSH and pushes channel
 * notifications on device add/remove. Also exposes tools for on-demand queries.
 *
 * Timestamps are this system's local wall-clock time, captured when the udev
 * event's header line arrives (not when its block finishes parsing), and are
 * embedded in BOTH the tool output and the channel notification so a delayed
 * notification can't read as "just now". remove events (which carry no device
 * descriptor) are back-filled from the matching add by sysfs path.
 *
 * The process exits when Claude Code closes stdin and kills its ssh child, so
 * sessions don't leak orphaned bun + ssh monitors.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { spawn, spawnSync, type ChildProcess } from "child_process";

const REMOTE_HOST = process.env.USB_HOTPLUG_HOST || "root@localhost";
const SSH_OPTS = ["-o", "ServerAliveInterval=30", "-o", "ConnectTimeout=5"];
const MAX_EVENTS = 1000; // cap the in-memory buffer so a long-lived server can't grow unbounded

interface UsbEvent {
  action: string;
  timestamp: number; // epoch ms, captured when the event's header line arrived
  vendor_id: string;
  product_id: string;
  manufacturer: string;
  product: string;
  busnum: string;
  devnum: string;
  devpath: string;
}

// Local-time zone abbreviation (e.g. "PDT"), resolved once from this system.
const ZONE =
  new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
    .formatToParts(new Date())
    .find((p) => p.type === "timeZoneName")?.value || "";

// This system's local time WITH a date, so stale events are unmistakable.
function fmtTime(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  const time = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  return ZONE ? `${date} ${time} ${ZONE}` : `${date} ${time}`;
}

// Strip mojibake / non-printable bytes from udev strings (e.g. the leading
// non-ASCII byte in the Ingenic bootrom's product string showed up as "Ã").
function clean(s: string): string {
  return (s || "")
    .replace(/_/g, " ")
    .replace(/[^\x20-\x7E]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function deviceName(ev: UsbEvent): string {
  return ev.product || ev.manufacturer || "unknown";
}

function vidPid(ev: UsbEvent): string {
  return ev.vendor_id ? `${ev.vendor_id}:${ev.product_id}` : "????:????";
}

function formatEvent(ev: UsbEvent): string {
  return `[${fmtTime(ev.timestamp)}] ${ev.action.toUpperCase().padEnd(6)} ${vidPid(ev)} ${deviceName(ev)} (bus ${ev.busnum} dev ${ev.devnum})`;
}

// Event buffer
const events: UsbEvent[] = [];
let cursor = 0;
let connected = false;

// Identity of the last-seen add per sysfs path, so a remove (which has no
// descriptor) can be back-filled with what was actually unplugged.
const lastSeen = new Map<
  string,
  { vendor_id: string; product_id: string; manufacturer: string; product: string }
>();

// Waiters: resolved when a matching event arrives
type Waiter = {
  action: string;
  vid_pid: string;
  resolve: (ev: UsbEvent) => void;
};
const waiters: Waiter[] = [];

// Create MCP server with channel capability
const mcp = new Server(
  { name: "usb-hotplug", version: "1.1.0" },
  {
    capabilities: {
      experimental: { "claude/channel": {} },
      tools: {},
    },
    instructions:
      `USB hotplug events from ${REMOTE_HOST} arrive as <channel source="usb-hotplug"> ` +
      "notifications with a timestamp, action, vid_pid, and device name. These indicate a physical USB " +
      "device was plugged in or removed from the lab bench. The timestamp is this system's local time " +
      "of the physical event; compare it to the current time to tell whether a notification is recent or stale.",
  }
);

// Tools
mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "usb_events",
      description:
        `Get USB hotplug events since last call. Returns only NEW add/remove events on ${REMOTE_HOST} since you last checked.`,
      inputSchema: { type: "object" as const, properties: {} },
    },
    {
      name: "usb_devices",
      description: `List all currently connected USB devices on ${REMOTE_HOST}.`,
      inputSchema: { type: "object" as const, properties: {} },
    },
    {
      name: "usb_wait",
      description:
        `Block until a USB add or remove event occurs on ${REMOTE_HOST}, then return it. ` +
        "Use this instead of sleep loops when waiting for a device to appear or disappear. " +
        "Waits for a LIVE detection: it returns immediately only if a matching event happened in the last ~2 seconds " +
        "(the plug-then-call race); it never replays older buffered/log events. " +
        "Example: usb_wait(action='add', timeout_s=30) to wait for any device plug-in. " +
        "Add vid_pid='a108:c309' to wait specifically for Ingenic bootrom.",
      inputSchema: {
        type: "object" as const,
        properties: {
          action: {
            type: "string",
            enum: ["add", "remove"],
            description: "Wait for 'add' (plug-in) or 'remove' (unplug). Required.",
          },
          vid_pid: {
            type: "string",
            description: "Optional VID:PID filter (e.g. 'a108:c309' for bootrom). If omitted, any device matches.",
          },
          timeout_s: {
            type: "number",
            description: "Timeout in seconds (default 30, max 120). Returns 'timeout' if no event arrives.",
          },
        },
        required: ["action"],
      },
    },
    {
      name: "usb_history",
      description: "Show ALL hotplug events since server started.",
      inputSchema: { type: "object" as const, properties: {} },
    },
  ],
}));

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name } = req.params;

  if (name === "usb_events") {
    const newEvents = events.slice(cursor);
    cursor = events.length;
    const status = connected ? "connected" : "reconnecting";
    if (newEvents.length === 0) {
      return { content: [{ type: "text", text: `no new usb events (monitor: ${status}, now: ${fmtTime(Date.now())})` }] };
    }
    const lines = newEvents.map(formatEvent);
    lines.push(`(monitor: ${status}, now: ${fmtTime(Date.now())})`);
    return { content: [{ type: "text", text: lines.join("\n") }] };
  }

  if (name === "usb_devices") {
    const result = spawnSync("ssh", [...SSH_OPTS, REMOTE_HOST, "lsusb"], {
      timeout: 10000,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const text = result.stdout?.toString().trim() || "error running lsusb";
    return { content: [{ type: "text", text }] };
  }

  if (name === "usb_wait") {
    const action = (req.params.arguments as any)?.action as string;
    const vid_pid = ((req.params.arguments as any)?.vid_pid as string) || "";
    const timeout_s = Math.min(Math.max(((req.params.arguments as any)?.timeout_s as number) || 30, 1), 120);

    // Only honor a buffered event if it JUST happened (handles the race where a
    // device appears microseconds before this call). Anything older is stale and
    // must NOT be returned: usb_wait waits for a LIVE detection, it does not
    // replay old log entries. Consume the buffer either way so stale events from
    // a long-lived / boot-looping device can never resurface here.
    const FRESH_MS = 2000;
    const now = Date.now();
    const match = events.slice(cursor).find((ev) => {
      if (ev.action !== action) return false;
      if (vid_pid && vidPid(ev) !== vid_pid) return false;
      return now - ev.timestamp <= FRESH_MS;
    });
    cursor = events.length;
    if (match) {
      return { content: [{ type: "text", text: formatEvent(match) }] };
    }

    // Wait for a future event
    const result = await Promise.race([
      new Promise<UsbEvent>((resolve) => {
        waiters.push({ action, vid_pid, resolve });
      }),
      new Promise<null>((resolve) => setTimeout(() => resolve(null), timeout_s * 1000)),
    ]);

    if (result === null) {
      // Clean up our waiter
      const idx = waiters.findIndex((w) => w.action === action && w.vid_pid === vid_pid);
      if (idx !== -1) waiters.splice(idx, 1);
      return { content: [{ type: "text", text: `timeout: no USB ${action} event after ${timeout_s}s` }] };
    }

    return { content: [{ type: "text", text: formatEvent(result) }] };
  }

  if (name === "usb_history") {
    if (events.length === 0) {
      const status = connected ? "connected" : "reconnecting";
      return { content: [{ type: "text", text: `no usb events recorded since server start (monitor: ${status}, now: ${fmtTime(Date.now())})` }] };
    }
    return { content: [{ type: "text", text: events.map(formatEvent).join("\n") }] };
  }

  throw new Error(`unknown tool: ${name}`);
});

// ---- lifecycle: exit (and kill the ssh child) when Claude Code closes stdin ----
let monitorProc: ChildProcess | null = null;
let shuttingDown = false;
function shutdown(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  try {
    monitorProc?.kill("SIGTERM");
  } catch {}
  process.exit(code);
}
process.stdin.on("end", () => shutdown(0));
process.stdin.on("close", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));
process.on("SIGINT", () => shutdown(0));
mcp.onclose = () => shutdown(0);

// Connect to Claude Code over stdio
await mcp.connect(new StdioServerTransport());

// Start SSH udevadm monitor
function startMonitor() {
  const proc = spawn(
    "ssh",
    [...SSH_OPTS, REMOTE_HOST, "udevadm", "monitor", "--property", "--udev", "--subsystem-match=usb"],
    { stdio: ["ignore", "pipe", "ignore"] }
  );
  monitorProc = proc;

  connected = true;
  let props: Record<string, string> = {};
  let action = "";
  let pendingTs = 0; // arrival time of the current block's header line
  let buf = "";

  proc.stdout!.on("data", (chunk: Buffer) => {
    buf += chunk.toString();
    const lines = buf.split("\n");
    buf = lines.pop()!;

    for (const raw of lines) {
      const line = raw.trimEnd();

      if (line.startsWith("UDEV")) {
        if (action && props["DEVTYPE"] === "usb_device") {
          emitEvent(action, props, pendingTs);
        }
        props = {};
        action = "";
        pendingTs = Date.now(); // stamp on arrival, so a deferred emit keeps the real time
        for (const p of line.split(" ")) {
          if (["add", "remove", "bind", "unbind", "change"].includes(p)) {
            action = p;
            break;
          }
        }
      } else if (line.includes("=")) {
        const idx = line.indexOf("=");
        props[line.slice(0, idx)] = line.slice(idx + 1);
      } else if (line === "" && action && props["DEVTYPE"] === "usb_device") {
        emitEvent(action, props, pendingTs);
        props = {};
        action = "";
      }
    }
  });

  proc.on("close", () => {
    connected = false;
    if (!shuttingDown) setTimeout(startMonitor, 5000);
  });
}

function emitEvent(action: string, props: Record<string, string>, ts: number) {
  if (action !== "add" && action !== "remove") return;

  const devpath = props["DEVPATH"] || "";
  let vendor_id = props["ID_VENDOR_ID"] || "";
  let product_id = props["ID_MODEL_ID"] || "";
  let manufacturer = clean(props["ID_VENDOR_FROM_DATABASE"] || props["ID_VENDOR"] || "");
  let product = clean(props["ID_MODEL_FROM_DATABASE"] || props["ID_MODEL"] || "");

  if (action === "add") {
    if (devpath) lastSeen.set(devpath, { vendor_id, product_id, manufacturer, product });
  } else {
    // remove events carry no descriptor -> back-fill from the matching add
    const prev = devpath ? lastSeen.get(devpath) : undefined;
    if (prev) {
      vendor_id = vendor_id || prev.vendor_id;
      product_id = product_id || prev.product_id;
      manufacturer = manufacturer || prev.manufacturer;
      product = product || prev.product;
      lastSeen.delete(devpath);
    }
  }

  const ev: UsbEvent = {
    action,
    timestamp: ts || Date.now(),
    vendor_id,
    product_id,
    manufacturer,
    product,
    busnum: props["BUSNUM"] || "",
    devnum: props["DEVNUM"] || "",
    devpath,
  };
  events.push(ev);
  if (events.length > MAX_EVENTS) {
    const drop = events.length - MAX_EVENTS;
    events.splice(0, drop);
    cursor = Math.max(0, cursor - drop);
  }

  // Resolve any matching waiters
  const ev_vid_pid = vidPid(ev);
  for (let i = waiters.length - 1; i >= 0; i--) {
    const w = waiters[i];
    if (w.action !== ev.action) continue;
    if (w.vid_pid && w.vid_pid !== ev_vid_pid) continue;
    waiters.splice(i, 1);
    w.resolve(ev);
  }

  const name = deviceName(ev);
  void mcp.notification({
    method: "notifications/claude/channel",
    params: {
      content: `[${fmtTime(ev.timestamp)}] USB ${ev.action}: ${ev_vid_pid} ${name} (bus ${ev.busnum} dev ${ev.devnum})`,
      meta: {
        action: ev.action,
        vid_pid: ev_vid_pid,
        device: name,
        bus: ev.busnum,
        devnum: ev.devnum,
        timestamp: ev.timestamp,
        time: fmtTime(ev.timestamp),
      },
    },
  });
}

startMonitor();
