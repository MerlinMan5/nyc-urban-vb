// Private Vercel Blob storage. One file per person (users/<token>.json) so one
// person's save can never overwrite another's; catalog.json is written by the bot.
import { put, get, list, del } from '@vercel/blob';
import crypto from 'node:crypto';

const ACCESS = 'private';

export async function readJSON(path) {
  try {
    const r = await get(path, { access: ACCESS, useCache: false });
    if (!r || r.statusCode !== 200 || !r.stream) return null;
    return JSON.parse(await new Response(r.stream).text());
  } catch (e) {
    if (/not.?found/i.test(String(e?.name) + String(e?.message))) return null;
    throw e;
  }
}

export async function writeJSON(path, obj) {
  await put(path, JSON.stringify(obj, null, 2), {
    access: ACCESS,
    addRandomSuffix: false,
    allowOverwrite: true,
    contentType: 'application/json',
  });
}

export async function deletePath(path) {
  const r = await list({ prefix: path, limit: 1 });
  if (r.blobs[0]) await del(r.blobs[0].url);
}

export async function listUsers() {
  const users = [];
  let cursor;
  do {
    const r = await list({ prefix: 'users/', cursor });
    for (const b of r.blobs) {
      const u = await readJSON(b.pathname);
      if (u) users.push(u);
    }
    cursor = r.hasMore ? r.cursor : undefined;
  } while (cursor);
  return users;
}

export const userPath = (token) => `users/${token}.json`;

export function validToken(t) {
  return typeof t === 'string' && /^[A-Za-z0-9_-]{20,64}$/.test(t);
}

export function randomString(n, alphabet = 'abcdefghijkmnpqrstuvwxyz23456789') {
  const bytes = crypto.randomBytes(n);
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join('');
}

export function newUser(name, rules = []) {
  return {
    token: randomString(28, 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'),
    topic: `nyurban-${randomString(16)}`,
    name: cleanName(name),
    paused: false,
    rules,
    snooze: [],
    created: new Date().toISOString(),
  };
}

export function cleanName(n) {
  return String(n || '').replace(/[<>]/g, '').trim().slice(0, 40) || 'Player';
}

// Only the fields a person may change on their own page.
export function cleanPrefs(body) {
  const rules = Array.isArray(body?.rules)
    ? body.rules.slice(0, 20).map((r) => ({
        venue: String(r?.venue || '').slice(0, 40),
        levels: Array.isArray(r?.levels) ? r.levels.slice(0, 30).map((x) => String(x).slice(0, 80)) : [],
      })).filter((r) => r.venue)
    : [];
  const snooze = Array.isArray(body?.snooze)
    ? body.snooze.map(String).filter((d) => /^\d{1,2}\/\d{1,2}$/.test(d)).slice(0, 60)
    : [];
  return { name: cleanName(body?.name), paused: !!body?.paused, rules, snooze };
}

export function publicUser(u) {
  const { token, ...rest } = u;
  return rest;
}

export async function ntfy(topic, message, title = 'NY Urban volleyball') {
  const r = await fetch(`https://ntfy.sh/${encodeURIComponent(topic)}`, {
    method: 'POST',
    body: message,
    headers: { Title: title, Priority: 'high', Tags: 'volleyball' },
  });
  return r.ok;
}
