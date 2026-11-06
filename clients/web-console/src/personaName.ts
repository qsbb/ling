/**
 * 对话里"她"叫什么名字。
 *
 * 说话人标签不能写死 `ling`：那是账号名，不是她本人。名字以
 * `/v1/persona` 的 `display_name` 为准（人设页改完这里就跟着变），
 * 读不到时退回「她」——宁可用代词，也不冒用用户名。
 *
 * 全局缓存一次：切页回来不复请求，也不会在一次对话里闪名。
 */
import { useEffect, useSyncExternalStore } from 'react';
import { api } from './api';

const FALLBACK = '她';

let current = FALLBACK;
let started = false;
const listeners = new Set<() => void>();

function publish(name: string): void {
  current = name;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function usePersonaName(): string {
  useEffect(() => {
    if (started) return;
    started = true;
    api
      .persona()
      .then((persona) => {
        const name = (persona.display_name || persona.name || '').trim();
        if (name) publish(name);
      })
      .catch(() => {
        // 读不到名字不影响对话：用代词继续。
      });
  }, []);
  return useSyncExternalStore(subscribe, () => current);
}
