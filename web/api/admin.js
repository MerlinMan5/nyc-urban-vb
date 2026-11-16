// Admin (?k=ADMIN_TOKEN): list people with their private links, add or remove people.
import { listUsers, writeJSON, deletePath, userPath, newUser, validToken, cleanPrefs, ntfy } from '../lib/store.js';
import { isAdmin } from '../lib/auth.js';

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (!isAdmin(req)) return res.status(401).json({ error: 'unauthorized' });
  const base = `https://${req.headers['x-forwarded-host'] || req.headers.host}`;
  const view = (u) => ({
    name: u.name, link: `${base}/?t=${u.token}`, token: u.token, topic: u.topic,
    paused: !!u.paused, rules: u.rules || [], snooze: u.snooze || [],
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
