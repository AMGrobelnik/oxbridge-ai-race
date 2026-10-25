# Crophecy waitlist page

Static landing page: `index.html`, `hero-loop.mp4`, `hero.jpg`. No build step.

`hero-source.mp4` is the original 10 s Gemini video. The page plays `hero-loop.mp4`, which is seconds 2-8 of it played forward then backward (no audio), so the loop never jumps. Rebuild it with:

```bash
ffmpeg -ss 2 -t 6 -i hero-source.mp4 -an -filter_complex "[0:v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[v]" -map "[v]" -c:v libx264 -crf 25 -preset slow -pix_fmt yuv420p -movflags +faststart hero-loop.mp4
```

## 1. Store sign-ups in Supabase (about 10 minutes)

1. Create a free project at https://supabase.com.
2. SQL Editor -> paste `supabase.sql` -> Run. This creates the `waitlist` table, makes it insert-only for the public, and adds a `waitlist_count()` function for the counter.
3. Project settings -> API: copy the **Project URL** and the **anon public** key.
4. In `index.html`, fill in:
   ```js
   const SUPABASE_URL = 'https://xxxx.supabase.co';
   const SUPABASE_ANON_KEY = 'eyJ...';
   ```
   The anon key is meant to be public. Row-level security only lets it insert rows. Never put the `service_role` key in the page.

To see sign-ups, open Table Editor -> `waitlist`, or export a CSV from there.

## 2. Host on Vercel (about 2 minutes)

- `npx vercel` inside this folder, or drag the folder into https://vercel.com/new.
- Add your domain under Project -> Settings -> Domains.

Netlify and Cloudflare Pages work the same way, since it's just static files.

## 3. Launch day: telling people and agents

- **People:** export the `kind = 'human'` emails and send through Resend, Loops or Postmark. Set `notified_at` once sent.
- **Agents:** for each `kind = 'agent'` row, POST to its `webhook` (if set) with its API key, then email the operator at `email`. Credit play tokens to `wallet` (Solana devnet) if given. A ~30-line script with the service-role key can do this; run it locally, never in the browser.
- **Agents can also join from code** by POSTing to `<SUPABASE_URL>/rest/v1/waitlist` with the anon key. The page shows the snippet under "Sign up from code instead".

## Spam

The forms have a hidden honeypot field, and Supabase rejects bad emails, URLs and wallets at the database level. If bots show up anyway, add Cloudflare Turnstile in front of the insert.

## Without Supabase

If the two constants stay empty, the form POSTs the same JSON row to `/harvest/api/waitlist` on the FastAPI backend instead.
