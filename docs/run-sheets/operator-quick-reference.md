> For: the staff member running an event from the touch-screen PC.

Proskenion is dark-themed on purpose — that's normal, not a fault. The
strip of tabs near the top is how you move between screens. The strip
along the very bottom of the screen never changes: device status on the
left, a timer and the clock on the right, your account on the far right.

## The screens

| Tab | What it's for |
|---|---|
| **Pages** | The default screen when you sign in. Custom button panels an admin has built for this venue — whatever mix of scenes, mixer channels and lighting the panel was set up with. |
| **Scenes** | A grid of buttons, one per scene. Tap one to run it. |
| **Mixer** | The virtual channel strips for the CQ-20B: faders, mutes, and the desk-scene band along the top for recalling a full mixer scene. |
| **Lighting** | Group and individual lighting faders, a master, and the fade time. |
| **Stage Plan** | A drawing of the stage showing where each lighting bar and fixture is. |
| **Video** | Which HDMI source is routed to which output on the matrix. |
| **Projector** | Power, input, and the projector's own status (warming up, cooling down, and so on). |

## Recalling a scene

Go to **Scenes** and tap the button. It runs immediately — there is no
confirmation step, so make sure it's the one you mean before you tap it.
The button briefly changes colour to show how it went (a moment of colour is
normal; if it settles on red, see "Reading the colours" below and tell
someone). A "Protected" badge just means an admin has locked that scene from
being edited or deleted on the admin side — it runs exactly the same as any
other scene.

## Reading the colours

Every status in Proskenion is shown with a colour **and a shape**, never
colour alone, so it still reads correctly if you're colour-blind:

| Colour | Shape | Means |
|---|---|---|
| Green | filled dot | Connected and working |
| Amber | triangle with `!` | Degraded, or busy — see below |
| Red | cross | Offline |
| Grey | hollow circle | Not set up |
| Teal, spinning | spinner | Connecting right now |

The five device indicators along the bottom of the screen — KNX, DMX, Mixer,
Projector, HDMI — always show one of these. Tap one to see more detail
(host, when it was last seen, the last error).

**Amber on the projector usually means "busy," not broken.** The projector
only accepts one connection at a time. If something else has a connection
open to it — most likely the legacy controller PC, if it's still in the
rack — the projector shows amber rather than red, because it's not actually
offline. It will pick up as soon as the other connection lets go. Only call
someone if it stays amber (or turns red) for more than a few minutes with
nothing else obviously using it.

## "Under external control"

If a visiting production plugs its own lighting desk into the booth's DMX
input, a banner appears saying the room is under external control. This is
automatic — nobody needs to press anything:

- The Lighting screen shows the desk's own live levels, read-only.
- The four KNX stage-lighting panels are locked out at the wall.
- **House lighting is not affected** — it's on KNX, and the visiting desk
  cannot touch it.
- Everything else (mixer, projector, HDMI) works as normal.

It clears itself about 5 seconds after the visiting desk is switched off —
nothing to press for that either. See `hire-handover.md` in this folder for
what to check before and after a hire that involves a visiting desk.

## Who to call

The contact list for the KNX integrator, the audio engineer and the school's
IT contact is kept with the rack (`HANDOVER.md`, and the laminated card). If
the controller itself has stopped responding, see `recovery-card.md` in this
folder before calling anyone.
