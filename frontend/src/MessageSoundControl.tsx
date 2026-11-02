import { useSyncExternalStore } from "react";
import { isMessageSoundEnabled, setMessageSoundEnabled, subscribeMessageSound } from "./messageSound";
import "./messageSound.css";

export function MessageSoundControl({ compact = false }: { compact?: boolean }) {
  const enabled = useSyncExternalStore(subscribeMessageSound, isMessageSoundEnabled, () => true);
  const label = enabled ? "关闭消息提示音" : "开启消息提示音";
  return <button type="button" className={`${compact ? "icon-button" : "button-secondary"} message-sound-control`} aria-label={label} aria-pressed={enabled} title={label} onClick={() => setMessageSoundEnabled(!enabled)}>
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M11 4 5 9H2v6h3l6 5V4Z"/>{enabled ? <><path d="M15 8a6 6 0 0 1 0 8"/><path d="M18 5a10 10 0 0 1 0 14"/></> : <path d="m16 9 6 6m0-6-6 6"/>}</svg>
    {!compact && <span>消息提示音：{enabled ? "开" : "关"}</span>}
  </button>;
}
