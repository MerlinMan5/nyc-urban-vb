// A person's own preferences, addressed by their private link token (?t=...).
import { readJSON, writeJSON, userPath, validToken, cleanPrefs, publicUser } from '../lib/store.js';

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  const t = req.query.t;
  if (!validToken(t)) return res.status(404).json({ error: 'unknown link' });
  const path = userPath(t);
  const user = await readJSON(path);
  if (!user) return res.status(404).json({ error: 'unknown link' });

  if (req.method === 'GET') {
    const catalog = (await readJSON('catalog.json')) || { venues: {} };
    return res.json({ user: publicUser(user), catalog });
  }
  if (req.method === 'POST') {
    const prefs = cleanPrefs(req.body || {});
    const updated = { ...user, ...prefs, updated: new Date().toISOString() };
    await writeJSON(path, updated);
    return res.json({ ok: true, user: publicUser(updated) });
  }
  res.setHeader('Allow', 'GET, POST');
  return res.status(405).end();
}
