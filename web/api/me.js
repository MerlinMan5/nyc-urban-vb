// A person's own preferences, addressed by their private link token (?t=...).
import { readJSON, writeJSON, userPath, validToken, cleanPrefs, publicUser, pruneSnooze } from '../lib/store.js';

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  const t = req.query.t;
  if (!validToken(t)) return res.status(404).json({ error: 'unknown link' });
  const path = userPath(t);
  const user = await readJSON(path);
  if (!user) return res.status(404).json({ error: 'unknown link' });

  if (req.method === 'GET') {
    const catalog = (await readJSON('catalog.json')) || { venues: {} };
    const pruned = pruneSnooze(user.snooze || []);
    const stale = pruned.length !== (user.snooze || []).length;
    if (stale) user.snooze = pruned;
    if (stale || !user.lastSeen || Date.now() - Date.parse(user.lastSeen) > 10 * 60 * 1000) {
      user.lastSeen = new Date().toISOString();
      user.visits = (user.visits || 0) + 1;
      try { await writeJSON(path, user); } catch {}
    }
    return res.json({ user: publicUser(user), catalog });
  }
  if (req.method === 'POST') {
    const prefs = cleanPrefs(req.body || {});
    const now = new Date().toISOString();
    const updated = { ...user, ...prefs, updated: now, saves: (user.saves || 0) + 1, lastSeen: now };
    await writeJSON(path, updated);
    return res.json({ ok: true, user: publicUser(updated) });
  }
  res.setHeader('Allow', 'GET, POST');
  return res.status(405).end();
}
