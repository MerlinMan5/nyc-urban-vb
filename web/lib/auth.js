// The bot authenticates with GitHub Actions' OIDC token: no shared secret.
// Only runs of the configured repo on main (or the probe branch) are accepted.
import { createRemoteJWKSet, jwtVerify } from 'jose';

const ISSUER = 'https://token.actions.githubusercontent.com';
const JWKS = createRemoteJWKSet(new URL(`${ISSUER}/.well-known/jwks`));
export const AUDIENCE = 'nyurban-prefs';

export async function verifyBot(req) {
  const tok = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
  if (!tok) return false;
  try {
    const { payload } = await jwtVerify(tok, JWKS, { issuer: ISSUER, audience: AUDIENCE });
    const repo = String(process.env.BOT_REPO || '').toLowerCase();
    return (
      !!repo &&
      String(payload.repository || '').toLowerCase() === repo &&
      ['refs/heads/main', 'refs/heads/probe'].includes(payload.ref)
    );
  } catch {
    return false;
  }
}

export function isAdmin(req) {
  const key = req.query?.k || req.headers['x-admin-key'];
  const want = process.env.ADMIN_TOKEN;
  return !!want && typeof key === 'string' && key.length === want.length && key === want;
}
