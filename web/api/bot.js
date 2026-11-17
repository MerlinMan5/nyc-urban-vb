// Bot endpoints (GitHub Actions OIDC only):
//   GET  /api/bot          -> everyone's alert preferences + topics
//   POST /api/bot          -> store the catalog of venues/levels/dates the bot saw
import { listUsers, writeJSON } from '../lib/store.js';
import { verifyBot } from '../lib/auth.js';

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (!(await verifyBot(req))) return res.status(401).json({ error: 'unauthorized' });

  if (req.method === 'GET') {
    const users = (await listUsers()).map((u) => ({
      id: u.token.slice(0, 6),
      name: u.name,
      topic: u.topic,
      paused: !!u.paused,
      levels: u.levels || [],
      rules: u.rules || [],
      snooze: u.snooze || [],
    }));
    return res.json({ users });
  }
  if (req.method === 'POST') {
    const venues = req.body?.venues;
    if (!venues || typeof venues !== 'object') return res.status(400).json({ error: 'venues required' });
    await writeJSON('catalog.json', { updated: new Date().toISOString(), venues });
    return res.json({ ok: true });
  }
  return res.status(405).end();
}
