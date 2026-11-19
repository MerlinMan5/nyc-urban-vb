// Admin (?k=ADMIN_TOKEN): list people with their private links, add or remove people.
import { listUsers, readJSON, writeJSON, deletePath, userPath, newUser, validToken, cleanPrefs, ntfy, userQuiet, pruneSnooze } from '../lib/store.js';
import { isAdmin } from '../lib/auth.js';

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (!isAdmin(req)) return res.status(401).json({ error: 'unauthorized' });
  const base = `https://${req.headers['x-forwarded-host'] || req.headers.host}`;
  let stats = { alerts: {} };
  const view = (u) => ({
    name: u.name, link: `${base}/?t=${u.token}`, token: u.token, topic: u.topic,
    paused: !!u.paused, levels: u.levels || [], rules: u.rules || [], snooze: pruneSnooze(u.snooze || []), maxFreq: u.maxFreq || 0, quiet: userQuiet(u),
    created: u.created || null, updated: u.updated || null, lastSeen: u.lastSeen || null,
    visits: u.visits || 0, saves: u.saves || 0,
    alerts: stats.alerts[u.token.slice(0, 6)] || { pushes: 0, openings: 0 },
  });

  if (req.method === 'GET' && req.query.action === 'create') {
    // GET variant so a person can be added from a plain link
    let rules = [];
    try { rules = JSON.parse(req.query.rules || '[]'); } catch {}
    const prefs = cleanPrefs({ name: req.query.name, rules });
    const u = newUser(prefs.name, prefs.rules);
    await writeJSON(userPath(u.token), u);
    return res.json({ ok: true, user: view(u) });
  }
  if (req.method === 'GET') {
    stats = (await readJSON('stats.json')) || stats;
    const users = await listUsers();
    users.sort((a, b) => String(a.created).localeCompare(String(b.created)));
    return res.json({ users: users.map(view) });
  }
  if (req.method === 'POST') {
    const { action } = req.body || {};
    if (action === 'create') {
      const prefs = cleanPrefs({ name: req.body.name, rules: req.body.rules });
      const u = newUser(prefs.name, prefs.rules);
      await writeJSON(userPath(u.token), u);
      return res.json({ ok: true, user: view(u) });
    }
    if (action === 'delete' && validToken(req.body.token)) {
      await deletePath(userPath(req.body.token));
      return res.json({ ok: true });
    }
    if (action === 'test' && validToken(req.body.token)) {
      const users = await listUsers();
      const u = users.find((x) => x.token === req.body.token);
      if (!u) return res.status(404).json({ error: 'not found' });
      return res.json({ ok: await ntfy(u.topic, `Test for ${u.name} ✅`) });
    }
    return res.status(400).json({ error: 'unknown action' });
  }
  return res.status(405).end();
}
