/*
 * OverlayBridge for Vencord / Vesktop
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

import { IpcMainInvokeEvent } from "electron";
import { connect, Socket } from "net";

let socket: Socket | null = null;
let connected = false;
let connecting = false;
let lastAttempt = 0;
let port = 6889;

// Frames are dropped while the overlay is slow, so a busy socket never piles up memory.
const MAX_BUFFERED = 4 * 1024 * 1024;

function ensureConnected() {
    if (connected || connecting) return;
    const now = Date.now();
    if (now - lastAttempt < 2000) return;
    lastAttempt = now;
    connecting = true;

    const s = connect({ host: "127.0.0.1", port });
    s.setNoDelay(true);
    s.on("connect", () => {
        connecting = false;
        connected = true;
        socket = s;
    });
    s.on("error", () => { /* overlay not running yet */ });
    s.on("close", () => {
        connecting = false;
        connected = false;
        if (socket === s) socket = null;
    });
    s.on("data", () => { /* overlay never sends anything back */ });
}

function write(line: string) {
    ensureConnected();
    if (!connected || !socket) return false;
    socket.write(line + "\n");
    return true;
}

export function setPort(_: IpcMainInvokeEvent, newPort: number) {
    if (newPort === port) return;
    port = newPort;
    socket?.destroy();
    socket = null;
    connected = false;
    lastAttempt = 0;
}

export function send(_: IpcMainInvokeEvent, message: unknown) {
    return write(JSON.stringify(message));
}

export function sendFrame(_: IpcMainInvokeEvent, id: string, jpeg: Uint8Array, w: number, h: number) {
    ensureConnected();
    if (!connected || !socket || socket.writableLength > MAX_BUFFERED) return false;
    const data = Buffer.from(jpeg).toString("base64");
    return write(JSON.stringify({ t: "frame", id, w, h, data }));
}

export function isConnected() {
    ensureConnected();
    return connected;
}

export function disconnect() {
    socket?.destroy();
    socket = null;
    connected = false;
}
