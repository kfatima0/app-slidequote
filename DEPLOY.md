# Deploying the demo

One container, one URL. The backend serves the built page, so there is nothing
to wire together and nothing is cross-origin.

Every external dependency is still mocked, so the running container makes **no
outbound calls** and needs **no API keys or secrets**. There is nothing to leak
and nothing to pay per request.

## What gets deployed

```
Dockerfile          node builds the page -> python serves page + API
  /                 the demo
  /api/...          the backend
  /docs             the backend's own console (do not show this to anyone)
```

Verified locally before writing this: the image builds, runs, and a real browser
drives the whole demo through it — request, parallel canvassing, transcript,
confirmation, placement, reset.

```bash
docker build -t yquo .
docker run --rm -p 8080:8000 yquo     # then open http://127.0.0.1:8080
```

## Choosing a host

The demo holds every visitor's world in memory, which decides two things:

* **One instance only.** A second one would serve some requests from a process
  that does not have that visitor's order in it. `Dockerfile` therefore runs a
  single worker. Horizontal scaling waits for the Supabase swap
  (MIGRATION_PLAN.md §2), which is where state stops being in-process.
* **A restart wipes everything.** Fine for a demo; nothing here is precious.

| Host | Cost | Cold starts |
|---|---|---|
| **Render** | free tier, or ~$7/mo | free tier sleeps after 15 min idle, then ~50s to wake |
| **Fly.io** | small free allowance | configurable; can stay warm |
| **Railway** | ~$5/mo | none |

**Cold starts are the thing to think about.** A free instance that sleeps means
the first person to open your link waits most of a minute staring at nothing, and
concludes it is broken. For a link going to Giulio or a beta tester, pay the few
euros for always-on. For a link you send five minutes before a call you are
already on, the free tier is fine because you can warm it yourself first.

## Render, step by step

`render.yaml` in this repo describes the service, so Render can read it rather
than you filling in a form.

1. Push to GitHub (Render deploys from the repo).
2. render.com -> **New** -> **Blueprint** -> pick the `YQuo` repo.
3. It reads `render.yaml` and offers one web service. Confirm.
4. First build takes a few minutes. You get a URL like
   `https://yquo-demo.onrender.com`.

To avoid sleeping, upgrade that service to a paid instance in its settings. No
other change is needed.

## After deploying, check these

```bash
curl https://YOUR-URL/api/health
```

Expect `"all_mocked": true`. If it ever says false, something real got switched
on by accident and should be switched back.

Then open the URL itself and run the walkthrough once. The things worth
confirming on a real host rather than assuming:

* the page loads and the live indicator goes green (the event stream survives
  the host's proxy)
* a request produces supplier rows that fill in one at a time
* a transcript opens
* **a second browser sees an empty world** — this is what makes the link safe to
  send to more than one person at a time

## Sending the link

Turn off browser translation before showing it to anyone Italian. The page is
Italian on purpose, and an English browser will offer to translate it, which
turns the supplier names into English in front of the one audience that must see
the real thing.

`/docs` is a developer tool. It works on the deployed URL too, and it looks like
what it is. Do not include it in anything you send.
