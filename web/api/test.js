// Send a test notification to the link owner's own ntfy topic.
import { readJSON, userPath, validToken, ntfy } from '../lib/store.js';

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'POST') return res.status(405).end();
  const t = req.query.t;
  if (!validToken(t)) return res.status(404).json({ error: 'unknown link' });
  const user = await readJSON(userPath(t));
  if (!user) return res.status(404).json({ error: 'unknown link' });
  const ok = await ntfy(user.topic, `Test for ${user.name} ✅ Alerts for your picks will arrive here.`);
  return res.status(ok ? 200 : 502).json({ ok });
}
