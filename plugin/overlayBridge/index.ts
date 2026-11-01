/*
 * OverlayBridge for Vencord / Vesktop
 * Sends your voice call (members, speaking, mute/deafen, webcams) to the
 * Vesktop Overlay app running on this computer.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

import { definePluginSettings } from "@api/Settings";
import { Logger } from "@utils/Logger";
import definePlugin, { makeRange, OptionType, PluginNative, ReporterTestable } from "@utils/types";
import { ChannelStore, GuildMemberStore, GuildStore, SelectedChannelStore, UserStore, VoiceStateStore } from "@webpack/common";

const Native = VencordNative.pluginHelpers.OverlayBridge as PluginNative<typeof import("./native")>;
const logger = new Logger("OverlayBridge");

const settings = definePluginSettings({
    style: {
        type: OptionType.SELECT,
        description: "Overlay look",
        options: [
            { label: "Discord (like the Windows overlay)", value: "discord", default: true },
            { label: "Mac-style cards", value: "mac" },
        ],
        onChange: () => pushState(),
    },
    showNames: {
        type: OptionType.SELECT,
        description: "Show names",
        options: [
            { label: "Always", value: "always", default: true },
            { label: "Only while speaking", value: "speaking" },
            { label: "Never", value: "never" },
        ],
        onChange: () => pushState(),
    },
    avatarSize: {
        type: OptionType.SELECT,
        description: "Avatar size",
        options: [
            { label: "Large", value: "large", default: true },
            { label: "Small", value: "small" },
        ],
        onChange: () => pushState(),
    },
    position: {
        type: OptionType.SELECT,
        description: "Where the overlay sits on screen",
        options: [
            { label: "Top left", value: "top-left", default: true },
            { label: "Top right", value: "top-right" },
            { label: "Bottom left", value: "bottom-left" },
            { label: "Bottom right", value: "bottom-right" },
            { label: "Left middle", value: "middle-left" },
            { label: "Right middle", value: "middle-right" },
        ],
        onChange: () => pushState(),
    },
    scale: {
        type: OptionType.SLIDER,
        description: "Overlay size",
        default: 1,
        markers: makeRange(0.6, 1.8, 0.2),
        onChange: () => pushState(),
    },
    opacity: {
        type: OptionType.SLIDER,
        description: "Card opacity",
        default: 0.85,
        markers: makeRange(0.3, 1, 0.1),
        onChange: () => pushState(),
    },
    accentColor: {
        type: OptionType.STRING,
        description: "Speaking glow color (hex)",
        default: "#23a55a",
        onChange: () => pushState(),
    },
    showOnlySpeaking: {
        type: OptionType.BOOLEAN,
        description: "Only show people while they talk (plus anyone with a webcam on)",
        default: false,
        onChange: () => pushState(),
    },
    showSelf: {
        type: OptionType.BOOLEAN,
        description: "Show yourself in the list",
        default: true,
        onChange: () => pushState(),
    },
    showWebcams: {
        type: OptionType.BOOLEAN,
        description: "Show webcams (needs the call view open somewhere in Vesktop)",
        default: true,
        onChange: () => pushState(),
    },
    showOwnWebcam: {
        type: OptionType.BOOLEAN,
        description: "Show your own webcam preview",
        default: true,
        onChange: () => pushState(),
    },
    webcamFps: {
        type: OptionType.SLIDER,
        description: "Webcam frames per second (lower = less CPU)",
        default: 15,
        markers: [5, 10, 15, 20, 24, 30],
        stickToMarkers: true,
        onChange: () => restartFrameLoop(),
    },
    webcamWidth: {
        type: OptionType.SLIDER,
        description: "Webcam tile width in pixels",
        default: 280,
        markers: [160, 200, 240, 280, 320, 400, 480],
        stickToMarkers: true,
        onChange: () => pushState(),
    },
    toasts: {
        type: OptionType.BOOLEAN,
        description: "Show a small popup when someone joins or leaves",
        default: true,
    },
    port: {
        type: OptionType.NUMBER,
        description: "Port of the overlay app (change only if you changed it there too)",
        default: 6889,
        onChange: v => Native.setPort(Number(v) || 6889),
    },
});

// Speaking is tracked here because the event is the only reliable live source.
const speaking = new Set<string>();
let lastMembers = new Set<string>();
let lastChannelId: string | null = null;
let pushTimer: ReturnType<typeof setTimeout> | null = null;
let heartbeat: ReturnType<typeof setInterval> | null = null;
let frameLoop: ReturnType<typeof setInterval> | null = null;
let canvas: OffscreenCanvas | null = null;
let ctx: OffscreenCanvasRenderingContext2D | null = null;
const busy = new Set<string>();

function displayName(userId: string, guildId: string | null | undefined) {
    const user = UserStore.getUser(userId) as any;
    if (!user) return "Unknown";
    return (guildId && GuildMemberStore.getNick(guildId, userId)) || user.globalName || user.username;
}

function avatarUrl(userId: string, guildId: string | null | undefined) {
    const user = UserStore.getUser(userId);
    try {
        return user?.getAvatarURL(guildId ?? undefined, 128, false, "png") ?? null;
    } catch {
        return null;
    }
}

function config() {
    const s = settings.store;
    return {
        position: s.position,
        scale: s.scale,
        opacity: s.opacity,
        accent: s.accentColor,
        style: s.style,
        showNames: s.showNames,
        avatarSize: s.avatarSize,
        onlySpeaking: s.showOnlySpeaking,
        showWebcams: s.showWebcams,
        webcamWidth: s.webcamWidth,
    };
}

function buildState() {
    const channelId = SelectedChannelStore.getVoiceChannelId();
    if (!channelId) return { t: "state", channel: null, users: [], config: config() };

    const channel = ChannelStore.getChannel(channelId);
    const guildId = channel?.guild_id ?? null;
    const guild = guildId ? GuildStore.getGuild(guildId) : null;
    const me = UserStore.getCurrentUser()?.id;
    const states = VoiceStateStore.getVoiceStatesForChannel(channelId) as Record<string, any>;

    const users = Object.values(states)
        .filter(vs => settings.store.showSelf || vs.userId !== me)
        .map(vs => ({
            id: vs.userId,
            name: displayName(vs.userId, guildId),
            avatar: avatarUrl(vs.userId, guildId),
            me: vs.userId === me,
            speaking: speaking.has(vs.userId),
            mute: !!(vs.mute || vs.selfMute || vs.suppress),
            deaf: !!(vs.deaf || vs.selfDeaf),
            serverMuted: !!(vs.mute || vs.deaf),
            video: !!vs.selfVideo && settings.store.showWebcams
                && (vs.userId !== me || settings.store.showOwnWebcam),
            live: !!vs.selfStream,
        }))
        .sort((a, b) => Number(b.me) - Number(a.me) || a.name.localeCompare(b.name));

    const name = channel?.name || (channel?.isDM?.() ? displayName(channel.recipients?.[0], null) : "Call");
    return {
        t: "state",
        channel: { id: channelId, name, guild: guild?.name ?? null, icon: (guild as any)?.getIconURL?.(64, false) ?? null },
        users,
        config: config(),
    };
}

function pushState() {
    if (pushTimer) return;
    pushTimer = setTimeout(() => {
        pushTimer = null;
        try {
            Native.send(buildState());
        } catch (e) {
            logger.error("Failed to send state", e);
        }
    }, 30);
}

function checkMembership() {
    const channelId = SelectedChannelStore.getVoiceChannelId() ?? null;
    const now = new Set(channelId ? Object.keys(VoiceStateStore.getVoiceStatesForChannel(channelId)) : []);
    const me = UserStore.getCurrentUser()?.id;

    if (settings.store.toasts && channelId && channelId === lastChannelId) {
        const guildId = ChannelStore.getChannel(channelId)?.guild_id;
        for (const id of now) if (!lastMembers.has(id) && id !== me)
            Native.send({ t: "toast", kind: "join", text: `${displayName(id, guildId)} joined`, avatar: avatarUrl(id, guildId) });
        for (const id of lastMembers) if (!now.has(id) && id !== me)
            Native.send({ t: "toast", kind: "leave", text: `${displayName(id, guildId)} left`, avatar: avatarUrl(id, guildId) });
    }

    if (channelId !== lastChannelId) speaking.clear();
    lastMembers = now;
    lastChannelId = channelId;
}

// Webcams: Discord only decodes video it is showing, so we read the <video>
// elements of the call view and find which user each one belongs to.
function fiberOf(el: Element): any {
    const key = Object.keys(el).find(k => k.startsWith("__reactFiber$"));
    return key ? (el as any)[key] : null;
}

function userIdForVideo(video: HTMLVideoElement, candidates: Set<string>): string | null {
    // Discord marks every call tile with the participant id. Screen shares use a
    // "type:guild:channel:user" stream key, so only plain ids are cameras.
    const tile = video.closest("[data-selenium-video-tile]");
    const tileId = tile?.getAttribute("data-selenium-video-tile");
    if (tileId) {
        if (candidates.has(tileId)) return tileId;
        if (tileId.includes(":")) return null;
    }

    let fiber = fiberOf(video);
    for (let depth = 0; fiber && depth < 80; depth++, fiber = fiber.return) {
        const p = fiber.memoizedProps;
        if (!p || typeof p !== "object") continue;
        const participant = p.participant;
        const ids = [p.participantUserId, p.userId, p.user?.id, participant?.user?.id, participant?.userId, participant?.id];
        for (const id of ids) {
            if (typeof id === "string" && candidates.has(id)) return id;
        }
    }
    return null;
}

function videoUsers(channelId: string) {
    const me = UserStore.getCurrentUser()?.id;
    const states = VoiceStateStore.getVoiceStatesForChannel(channelId) as Record<string, any>;
    const ids = new Set<string>();
    for (const vs of Object.values(states)) {
        if (!vs.selfVideo) continue;
        if (vs.userId === me && !settings.store.showOwnWebcam) continue;
        ids.add(vs.userId);
    }
    return ids;
}

function playingVideos() {
    return [...document.querySelectorAll("video")].filter(v => v.videoWidth > 0 && v.readyState >= 2);
}

function findVideos(): Map<string, HTMLVideoElement> {
    const result = new Map<string, HTMLVideoElement>();
    const channelId = SelectedChannelStore.getVoiceChannelId();
    if (!channelId || !settings.store.showWebcams) return result;

    const withVideo = videoUsers(channelId);
    if (!withVideo.size) return result;

    // The same camera can be shown twice (tile + popout); group by stream.
    const unmapped = new Map<unknown, HTMLVideoElement>();
    for (const video of playingVideos()) {
        const id = userIdForVideo(video, withVideo);
        if (!id) {
            const key = video.srcObject ?? video.currentSrc ?? video;
            const prev = unmapped.get(key);
            if (!prev || video.videoWidth > prev.videoWidth) unmapped.set(key, video);
            continue;
        }
        const prev = result.get(id);
        if (!prev || video.videoWidth > prev.videoWidth) result.set(id, video);
    }

    // One camera left over can only belong to the one person still missing.
    const missing = [...withVideo].filter(id => !result.has(id));
    if (missing.length === 1 && unmapped.size === 1) result.set(missing[0], [...unmapped.values()][0]);
    return result;
}

// Run OverlayBridgeDebug() in Vesktop's DevTools console to see what the camera matching finds.
function debugVideos() {
    const channelId = SelectedChannelStore.getVoiceChannelId();
    const candidates = channelId ? videoUsers(channelId) : new Set<string>();
    const videos = [...document.querySelectorAll("video")].map(v => ({
        size: `${v.videoWidth}x${v.videoHeight}`,
        readyState: v.readyState,
        hasStream: !!v.srcObject,
        tile: v.closest("[data-selenium-video-tile]")?.getAttribute("data-selenium-video-tile") ?? null,
        matched: userIdForVideo(v, candidates),
    }));
    const found = [...findVideos().keys()];
    const report = { channelId, candidates: [...candidates], videos, found };
    console.log("[OverlayBridge]", report);
    return report;
}

async function grabFrame(id: string, video: HTMLVideoElement) {
    if (busy.has(id)) return;
    busy.add(id);
    try {
        const targetW = Math.min(settings.store.webcamWidth * 2, video.videoWidth);
        const targetH = Math.round(targetW * video.videoHeight / video.videoWidth);
        canvas ??= new OffscreenCanvas(targetW, targetH);
        if (canvas.width !== targetW || canvas.height !== targetH) {
            canvas.width = targetW;
            canvas.height = targetH;
            ctx = null;
        }
        ctx ??= canvas.getContext("2d");
        if (!ctx) return;
        ctx.drawImage(video, 0, 0, targetW, targetH);
        const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.72 });
        const bytes = new Uint8Array(await blob.arrayBuffer());
        await Native.sendFrame(id, bytes, targetW, targetH);
    } catch (e) {
        logger.debug("Frame capture failed", e);
    } finally {
        busy.delete(id);
    }
}

async function captureFrames() {
    if (!settings.store.showWebcams) return;
    if (!await Native.isConnected()) return;
    for (const [id, video] of findVideos()) await grabFrame(id, video);
}

function restartFrameLoop() {
    if (frameLoop) clearInterval(frameLoop);
    const fps = Math.max(1, Math.min(30, settings.store.webcamFps || 15));
    frameLoop = setInterval(captureFrames, 1000 / fps);
}

export default definePlugin({
    name: "OverlayBridge",
    description: "Shows your Vesktop voice call on top of games: who's in the call, who's talking, mute/deafen and webcams. Needs the Vesktop Overlay app running.",
    tags: ["Voice", "Utility"],
    authors: [{ name: "akiloyutu", id: 0n }],
    reporterTestable: ReporterTestable.None,
    settings,

    flux: {
        VOICE_STATE_UPDATES() {
            checkMembership();
            pushState();
        },
        VOICE_CHANNEL_SELECT() {
            checkMembership();
            pushState();
        },
        RTC_CONNECTION_STATE() {
            pushState();
        },
        SPEAKING({ userId, speakingFlags }: { userId: string; speakingFlags: number; }) {
            const on = speakingFlags !== 0;
            if (on === speaking.has(userId)) return;
            if (on) speaking.add(userId);
            else speaking.delete(userId);
            Native.send({ t: "speaking", id: userId, on });
        },
        USER_UPDATE() {
            pushState();
        },
        GUILD_MEMBER_UPDATE() {
            pushState();
        },
    },

    start() {
        (window as any).OverlayBridgeDebug = debugVideos;
        Native.setPort(settings.store.port || 6889);
        checkMembership();
        pushState();
        // Resend everything regularly so a restarted overlay catches up by itself.
        heartbeat = setInterval(pushState, 2000);
        restartFrameLoop();
    },

    stop() {
        delete (window as any).OverlayBridgeDebug;
        if (heartbeat) clearInterval(heartbeat);
        if (frameLoop) clearInterval(frameLoop);
        if (pushTimer) clearTimeout(pushTimer);
        heartbeat = frameLoop = pushTimer = null;
        speaking.clear();
        Native.send({ t: "state", channel: null, users: [], config: config() });
        Native.disconnect();
    },
});
