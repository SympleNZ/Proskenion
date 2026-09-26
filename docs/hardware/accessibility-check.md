# Accessibility manual check (§24.7)

**For Simon.** A short script for the checks automation cannot do — a real
screen reader, real vision, a real printer. Everything else in §24.7's
checklist is covered by an automated test (`web/src/test/a11yScreens.test.tsx`
sweeps every screen with axe-core; `web/src/styles/contrast.test.ts` checks
every §24.4 pair against the tokens; `web/src/styles/reducedMotion.test.ts`
checks the motion rule; `tests/e2e/accessibility.spec.ts` checks real
touch-target sizes and axe's colour-contrast rule in an actual browser on the
D5 device's own viewport) — see *What's already covered* at the end if you
want the detail.

Record the date and your result against each item. A tick and a one-line note
is enough; a `WORKLOG.md` entry after a full pass is worth more than a note
here.

**Status: not yet run.**

## Before you start

- [ ] An admin session, an operator session, and a hirer PIN, all on the real
      appliance (not the dev server) — accessibility tooling reads real
      rendering, and a couple of these checks (zoom/reflow, print) look
      different against the built bundle.
- [ ] Windows 11 (D5's device, or your own laptop) with Edge, and Windows'
      built-in **Narrator** (`Ctrl+Win+Enter` to start it).
- [ ] An Android phone with **TalkBack** (Settings → Accessibility →
      TalkBack), the D5 secondary device.
- [ ] If you have NVDA installed, run the admin section with that too —
      Narrator and NVDA disagree often enough that one alone is not a full
      answer. Not required if you don't have it.

## 1. Screen reader — admin and operator (Narrator, Windows 11)

Start Narrator, open the admin interface, and work through it by keyboard
only — Tab, Shift+Tab, arrow keys, Enter, Escape — without touching the
mouse.

- [ ] Landing on any screen, Narrator announces a heading that names the
      screen (not "Edge", not silence).
- [ ] Tabbing through the sidebar, each item is announced by its label, and
      the current screen's item is announced as current (not just visually
      marked).
- [ ] The very first Tab press on any screen reaches a **Skip to main
      content** link — invisible until focused — and Enter on it jumps
      straight past the sidebar to the screen's own heading.
- [ ] Opening a device (Devices screen) or a rule (Rules screen): Narrator
      reads a sensible label for every field, not "edit box" with no name.
- [ ] A device that changes status while you are listening (unplug something,
      or watch one reconnect) is **announced without you doing anything** —
      this is the status bar's live region. If nothing changed during your
      pass, force one: in Admin → Devices, edit a device to a wrong address,
      save, and listen for the failure being read out.
- [ ] Open a sheet (Add a device, an action editor, any "Edit" button):
      Narrator announces it as a dialog, reads its title, and **focus cannot
      leave it** by tabbing — Tab from the last field returns to the first.
      Escape closes it and focus lands back on the button that opened it.
- [ ] The Rules screen's fader-law faders (Mixer, Lighting): arrow up/down
      changes the value and Narrator reads the new decibel or percentage
      figure, not a raw slider position number.
- [ ] The stage plan (operator → Stage Plan): **one** Tab press reaches it as
      a single stop (not once per fixture), and arrow keys move between
      fixtures within it, each announced by name, channel and current level —
      "Stage Wash 3, channel 7, at 72 percent" or close to it.
- [ ] Run a scene from the operator Scenes screen: the result (success,
      partial, failed) is announced without you tabbing to look for it.

## 2. Screen reader — the hirer surface (TalkBack, Android)

Sign in to `/hire` on the phone with TalkBack on, and work through it by
swipe/explore-by-touch rather than sighted tapping.

- [ ] The PIN entry screen is usable blind: TalkBack tells you which digit
      box you are on, and the keyboard that opens is announced as numeric.
- [ ] Each page tab is announced by name, and the current one as current.
- [ ] Every fader, mute button and panel button has a spoken name that makes
      sense out of context — "Wireless 1 fader" and its current dB reading,
      not "slider" alone.
- [ ] A ceiling stops the fader and TalkBack says the number it stopped at.
- [ ] The kill switch: have someone disable hire access from the admin side
      while you are on the hirer surface. TalkBack announces the "Access
      updated" dialog without you searching for it.

## 3. Colour-blind simulation

Chrome/Edge DevTools: **More tools → Rendering → Emulate vision
deficiencies**, or a browser extension if you prefer one you already trust.
Step through protanopia, deuteranopia and tritanopia on each:

- [ ] Status dots (device status bar, Devices screen): healthy, degraded,
      offline and not-configured are still distinguishable from each other —
      by the icon/shape, not the colour.
- [ ] The mixer's mute button, on and off: still readable as two different
      states (it now carries the word "MUTE" either way, plus a background
      change — confirm you don't need the colour to tell them apart).
- [ ] A muted lighting fader: the striped pattern is visible under
      simulation (this was fixed to not rely on colour alone — confirm it
      still reads once colour is degraded).
- [ ] Stage plan fixtures: shape and fill pattern still distinguish fixture
      types without the colour.
- [ ] Lighting groups and mixer scribble-strip colours: these are identity
      only (which group, not whether it's OK) — confirm nothing on screen
      asks you to read a group's *status* from its colour.
- [ ] The scene run result glow (green/amber/red bloom on a scene card): the
      text underneath still says what happened without the glow's colour.

## 4. Zoom to 200% and reflow

Ctrl+`+` in Edge to 200%, browser window at a normal desktop width (not
maximised to 4K).

- [ ] No horizontal scrollbar appears on the page itself (individual strips
      that are meant to scroll — the pages surface, wide tables — are fine).
- [ ] Every field's label stays attached to its field; nothing overlaps.
- [ ] The status bar stays usable and at the bottom; it does not get pushed
      off-screen.
- [ ] Text is not clipped or truncated somewhere it wasn't before.

Repeat at 400% zoom on one representative screen (Devices is a good one — it
has forms, tables and a sheet) as WCAG 2.2's 1.4.10 Reflow target; a single
column layout with no loss of function is enough, it does not need to look
good.

## 5. Muted text legibility, in the room

`text-muted` measures 4.94:1 on the surface it's used on — a passing number
on a calibrated monitor in daylight. §24.4 is explicit that this is "a
compliance floor, not a guarantee in a dark auditorium."

- [ ] With the house lights down, at the booth or the FOH position, read a
      screen that uses muted text for something that actually matters in the
      moment — a timestamp on the Logs screen, a device's technical detail
      line. If you have to lean in, that is worth a note even though it
      passes the measured number.

## 6. Print stylesheet

Print (Ctrl+P) or print-preview the Health screen and the Devices screen.

- [ ] Light background, dark text — not the dark theme printed as-is.
- [ ] Every status indicator prints its **word**, not just a coloured dot
      that turns invisible or grey on a black-and-white printer.

## 7. Touch targets — spot check with a finger, not a ruler

The D5 device (Windows 11, Edge, Dell P2424HT, 1920×1080 touch) and your
Android phone are both measured automatically now
(`tests/e2e/accessibility.spec.ts`, every interactive element on the hirer
surface, both viewports). This is a real-finger sanity check on the actual
hardware, which a measured bounding box can't fully stand in for:

- [ ] On the touch screen, every hirer control is comfortably tappable
      without hitting its neighbour — try the page tabs and the mute buttons
      close together.
- [ ] A slow drag on a fader scrolls nothing and a quick tap on a panel
      button never drags it ("a scroll is not a tap", CONVENTIONS.md).

## What's already covered (do not re-check by hand)

- Every screen passes an automated axe-core sweep at serious/critical impact
  — `web/src/test/a11yScreens.test.tsx`.
- Every §24.4 contrast pair is computed from the live tokens and checked
  against the spec's 4.5:1 threshold — `web/src/styles/contrast.test.ts`.
- `prefers-reduced-motion` collapses every animation/transition and stops the
  LED pulse and fixture-node transitions — `web/src/styles/reducedMotion.test.ts`.
- Faders: Arrow/Page/Home/End, `aria-valuetext` in dB, the unity detent —
  `web/src/components/fader/FaderStrip.test.tsx`.
- Ctrl/Cmd+S saves the current form on every admin screen with one, not just
  Devices — `web/src/lib/keyboard.test.ts` and each screen's own test.
- The scene editor's action cards reorder with Arrow Up/Down, announced by a
  live region — `web/src/admin/scenes/SceneEditor.test.tsx`.
- Real touch-target sizes and axe's colour-contrast rule (which needs an
  actual renderer, unlike the component sweep) on the hirer surface at the
  D5 viewport and a phone viewport, plus the login page —
  `tests/e2e/accessibility.spec.ts`.
