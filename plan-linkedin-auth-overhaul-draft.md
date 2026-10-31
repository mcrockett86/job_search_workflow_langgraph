# PROPOSED ONLY — LinkedIn authentication overhaul (draft for review)

Status: **not implemented**. Decision needed on which option to build.
Written 2026-10-31 after jen2's 2FA login could not be completed through the
current relay.

## Why the current design keeps failing

The current sign-in (`agents/web_login.py` + viewer relay) is a **hand-rolled
remote browser**: a worker thread screenshots headless Chrome every 1.5 s,
ships the PNG as base64 through HTTP polling, and translates viewer clicks
back into CDP coordinates. Every failure class we've hit comes from that
architecture:

| Failure | Cause |
|---|---|
| Clicks land in the wrong place ("Send me a code" unclickable) | screenshot px ≠ assumed window px (fixed in #12, but the class of bug remains: any viewport/DPR change re-breaks it) |
| Laggy, hard to navigate | 1.5 s poll, full-PNG base64, no cursor/scroll/keyboard-state feedback |
| 2FA dead-ends | the flow must survive pick-method → send → wait → type code → resubmit, all through a laggy relay, with LinkedIn re-rendering the page underneath |
| Two panes to babysit | our viewer + the picture of LinkedIn's page, with state that can desync |
| Session dies mid-wait | idle timers vs. minutes-long email codes (patched twice: #10, #11) |

We cannot make a DIY remote browser as good as a real browser. The robust
move is to stop relaying login at all — **do the interactive part where a
real browser already exists (the user's own Mac), and give the server only
the resulting session.**

## What must survive the redesign

- Per-profile isolation (`cache/linkedin_profile/<slug>/`) — unchanged.
- No password ever stored or transmitted by us — unchanged (stronger: the
  server never even sees credentials).
- Scrape flow (`_setup_driver` with persistent profile) — unchanged.
- Cloudflare stealth (`_deheadless_ua`) — unchanged.
- VPS deploy shape (no published ports, Caddy gate) — unchanged.

## Options

### A. Session import (recommended) — log in on your Mac, hand the server the cookie

LinkedIn's session is a cookie (`li_at`, plus friends). It is not IP-bound:
a session started on the Mac works from the VPS (that is exactly how a
laptop user travelling works in real life).

Flow per profile:
1. Viewer → "LinkedIn sign-in" → shows a one-time pairing code + instructions.
2. On the Mac: `python -m agents.linkedin_login --pair <code> --profile jen2`
   (or `--profile` picker) opens a **real, visible Chrome** with a throwaway
   profile. The user signs into LinkedIn normally — 2FA, push, passkey,
   Cloudflare, anything: it's just a normal browser login, zero relay.
3. The script watches for the logged-in cookie set, POSTs the cookies
   (TLS, basic-auth + pairing code) to the viewer API.
4. Server injects them into `cache/linkedin_profile/<slug>/` via CDP
   `Network.setCookie` (handles httpOnly `li_at`), verifies `/feed` loads
   logged-in, and reports success to the viewer.
5. Cookies live only in the profile dir on the VPS volume (mode 700). The
   pairing endpoint never echoes them back; nothing is logged.

Also offer a **manual paste fallback**: a password-type field in the viewer
where the user pastes `li_at` copied from DevTools (no Mac/script needed).

- **Complexity:** Medium (~1 new module + helper script + 2 endpoints; the
  whole web_login relay + viewer picture pane gets deleted, ~700 lines out).
- **Pros:** 2FA becomes a non-event (real browser, real keyboard); no lag
  (there is no remote browser); fewer moving parts than today; works for
  every future LinkedIn page redesign; profile add = 2 minutes on the Mac.
- **Cons:** a session token (password-equivalent) transits to the VPS once
  per login — mitigated: TLS + basic-auth + pairing code, memory-only
  handling, 700 perms at rest. Requires the user to run one command on the
  Mac (or paste a cookie). Occasional LinkedIn "new device" email notice
  (informational; session stays valid).

### B. noVNC — stream a real interactive browser to the viewer

Run Xvfb + x11vnc + websockify + noVNC inside the container; the viewer
embeds a noVNC canvas. The user drives the actual Chrome on the server with
real mouse/keyboard over WebSocket — smooth, scrollable, no coordinate math.

- **Complexity:** High (Dockerfile: xvfb/x11vnc/novnc; per-profile
  Xvfb+Chrome lifecycle endpoints; Caddy WebSocket route; auth on the VNC
  path; cleanup on crash).
- **Pros:** login fully server-side (no Mac needed); handles 2FA because
  the user is really in the browser; also useful later for manual
  troubleshooting of any page.
- **Cons:** real new infrastructure to secure and maintain; still a remote
  desktop (latency, phone-hostile); heavier image; more ways to break than
  today's problem count.

### C. Upgrade the current relay with CDP screencast

Replace 1.5 s polling with `Page.startScreencast` (pushed frames) + full
mouse/key/scroll dispatch.

- **Complexity:** Medium.
- **Pros:** keeps today's UX, much less lag; viewport size comes with each
  frame (no scaling bugs).
- **Cons:** still a DIY remote browser — the thing that has repeatedly
  failed; 2FA still relayed; user explicitly asked to stop polishing this.

### D. Local login + profile sync (no server API at all)

`python -m agents.linkedin_login` on the Mac (already works — visible
window), then `rsync` the profile dir to the VPS.

- **Complexity:** Low.
- **Pros:** zero new server surface.
- **Cons:** clunky (CLI + manual sync), easy to forget which profile, no
  feedback in the viewer. Fine as an emergency path, not as the product.

## Recommendation

**A (session import), with the manual `li_at` paste as the built-in
fallback.** It deletes the entire buggy relay instead of repairing it, and
moves every interactive step — including 2FA — into a real browser on the
user's own machine, which is the only place 2FA is ever painless. If we
later need server-side interactive browsing for other reasons, B can be
added on top without disturbing A.

## Implementation sketch (Option A)

- `agents/session_import.py` — pairing codes (single-use, TTL 10 min),
  cookie injection via CDP, `/feed` verification, per-profile status.
- `agents/web.py` — `POST /api/login/pair/start`, `POST /api/login/pair/claim`
  (from the Mac helper, pairing-code auth), `POST /api/login/paste`
  (manual li_at), `GET /api/login/session` (status: active/expired/none).
- `agents/linkedin_login.py` — add `--pair`/`--server` mode: visible Chrome,
  throwaway profile, watch for login, upload, exit. Keep `--status/--reset`.
- `agents/viewer.html` — replace the picture pane with a sign-in card:
  pairing code + copyable Mac command, manual-paste field, session status
  chip per profile, "sign out / reset session" button.
- Delete `agents/web_login.py` + relay JS + their tests; port the still-useful
  tests (per-profile scoping, 409s) to the new endpoints.
- Security: cookies only in memory → CDP → profile dir; never in responses,
  logs, or errors; pairing code is the only secret in the URL-free body.

## Test plan

- Unit: pairing code lifecycle; CDP injection call shapes (FakeDriver);
  paste endpoint rejects junk; nothing token-shaped appears in any response
  body or captured log lines.
- Integration: TestClient flow start → claim → status=active.
- Live: jen2 end-to-end from the Mac; mike re-login; expired-session path
  (status chip flips, scan run reports "sign in needed" as today).

## Rollout

Feature branch → full suite green → PR → merge on approval → VPS redeploy →
jen2 live test with the user → update the project skill (retire the relay
pitfalls, add the import flow).
