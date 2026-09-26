# Hirer device check (Phase 5, Q10)

A manual session on real devices: an **iPad** (Safari) and an **Android
phone** (Chrome), against the appliance on its **self-signed certificate**
(§6.16). It covers what a browser automated against `localhost` cannot: a
real touch screen, iOS's certificate handling, and the install prompt's
suppression. The rest of the §18 milestone is proven by
`tests/integration/test_phase5_milestone.py` and
`tests/e2e/hirer-milestone.spec.ts`.

**Status: not yet run.** Nothing below has been checked. Record the date,
the devices (model and OS version) and the result of each step in `WORKLOG.md`
when it is.

## Before you start

- [ ] The appliance is commissioned and on the network, serving the
      self-signed certificate: signed in as staff,
      `https://<hostname>/api/v1/auth/session` answers
      `"certificate": "self_signed"`.
- [ ] A mixer is connected (the CQ-20B, or the stub mixer driver on the bench)
      and at least one lighting output.
- [ ] Admin → Pages: a page with two mixer inputs, Main, one output, and a
      panel with one button whose rule runs a scene (a lighting look is
      easiest) and whose lamp is a derived status that the scene lights.
      A second page with one lighting channel.
- [ ] Admin → Hirer Access: both pages ticked; a ceiling of **−6 dB** on the
      first input and **−4 dB** on Main; a new six-digit PIN set (write it
      down); **Hire guest access enabled** ticked.
- [ ] A staff laptop signed in as admin, on Hirer Access, for the kill switch.

## 1. Trust the certificate (§6.16) — iPad

The certificate the appliance serves is
`/data/certs/live/<hostname>/fullchain.pem`. The appliance does not yet offer
it for download (see *Known gaps* below), so copy it off over SSH and send it
to the iPad (AirDrop, or email as an attachment).

- [ ] On the iPad, open the file. iOS says *Profile Downloaded*.
- [ ] Settings → **Profile Downloaded** (or Settings → General → **VPN &
      Device Management**) → the certificate → **Install**, and enter the
      passcode.
- [ ] Settings → General → About → **Certificate Trust Settings** → turn on
      full trust for the certificate. *(This step is not in §6.16's text. Note
      whether the certificate appears here at all: iOS lists only certificates
      it treats as a root authority.)*
- [ ] Safari → `https://<hostname>/` loads with no certificate warning.

Before trusting, for the record: open `https://<hostname>/hire` in Safari,
accept the warning, sign in, and note whether the live values ever arrive
(§6.16 says the WebSocket fails outright; the surface should show values
never arriving rather than working).

## 2. Sign in (§21.8)

- [ ] `https://<hostname>/hire` goes to the sign-in page.
- [ ] Tap the first PIN box: the **numeric** keypad opens. Type the PIN; it
      signs in on the sixth digit, with no separate button press.
- [ ] Lands on the first assigned page. **Two tabs**, named as the pages are,
      and no others. No admin link, no Mixer or Lighting, no configuration.
- [ ] Sign out (clear the site's data), then sign in with a **wrong** PIN:
      *Incorrect PIN*, the boxes shake and clear.

## 3. Targets (§24.6: 72 px)

- [ ] Every tab, fader, mute and panel button is comfortably a finger's
      width. With Safari's Web Inspector (Mac → Develop → the iPad), check one
      of each measures at least **72 × 72** CSS px.
- [ ] Portrait and landscape: nothing overlaps, the panel does not scroll
      vertically, and the status bar sits at the bottom.
- [ ] A swipe across the strips scrolls them and **fires nothing** (a scroll
      is not a tap).

## 4. The ceiling (§21.15)

- [ ] The ceilinged input's track shows the limit mark.
- [ ] Drag that fader fast from the bottom to past the top of the screen: it
      **stops at the mark** and stays there when released, with no error shown.
- [ ] The desk (MixPad, or `GET /mixer/state` as staff) shows **−6.0 dB**, not
      higher.
- [ ] Main does the same at **−4.0 dB**. The output on the page is not shown.
- [ ] Staff lower the ceiling to −12 dB on Hirer Access: the iPad's fader
      moves down on its own within a second. Raise it again: the fader can
      travel back up.

## 5. The button and its lamp (§21.9)

- [ ] Tap the panel button: the plain **Done** notification, the look comes
      up in the room, and the button's LED lights within a second.
- [ ] Staff take the look down: the LED goes out.

## 6. The kill switch (§6.6, §21.8)

- [ ] With the iPad on the page, staff untick **Hire guest access enabled**
      and confirm. Within a second the iPad shows **Access updated** — *"Your
      access has been updated by venue staff…"* — with **no PIN boxes and no
      sign-in prompt**.
- [ ] Hirer Access on the staff laptop says **1 hirer session closed**.
- [ ] Tap **Back to the start**, then try the PIN again: refused with *"Hire
      guest access is not currently available. Please contact venue staff."*
- [ ] Re-enable access. The iPad does **not** come back by itself; the old
      session stays dead. Signing in with the PIN works again.
- [ ] Repeat with a **PIN change** instead of the switch: same *Access
      updated*; the old PIN is refused; the new one works.

## 7. The install prompt (§21.8, Q10)

On the self-signed certificate:

- [ ] iPad, after the first sign-in: **no** *Add to Home Screen* instruction.
      Instead: *"This device can't be added to the Home Screen yet"*. Note
      whether this still appears **after** the certificate has been trusted in
      step 1 (the appliance reports the certificate as self-signed either way).
- [ ] Android, Chrome, after the first sign-in: **no** *Add this to your Home
      Screen?* card, and the same notice.

If a **trusted** certificate is available (Phase 6, or a test domain):

- [ ] Android: *Add this to your Home Screen?* → **Add** opens Chrome's own
      install dialog; the installed app is named **Auditorium Controls** and
      opens full-screen at `/hire`. **Not now** hides it; it does not return
      on reload.
- [ ] iPad: the one-time *Tap Share, then Add to Home Screen* instruction;
      shown once, never again. The home-screen icon is the hirer icon (PNG),
      not a screenshot.

## 8. Android phone

Repeat steps 2–6 on the Android phone in Chrome. For step 1, Chrome on
Android offers **Advanced → Proceed** on the warning; note whether the live
values then arrive (the socket) or not.

## Known gaps to record against

- There is no route on the appliance to download its certificate, which
  §6.16 and the iPad guidance in `proskenion/core/certs.py` assume ("Download
  the certificate from this controller").
- §21.8 says the suppressed prompt is "replaced with a link to the
  certificate trust instructions"; the notice has no link yet.
- A device that is offline behind a **panel button** only (a projector
  powered by a rule, say) raises no offline banner on the hirer surface; only
  mixer and lighting items are mapped to devices.
