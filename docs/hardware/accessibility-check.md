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

**Status: partly run, 1 Oct 2026 (Simon, on site, v0.1.17).**
- §1, Narrator: navigation fell short (focus stayed on the tab; "current"
  not announced; skip link not reached) and status announcements were
  noisy. Both were fixed for the next build (`47a48c5`); re-test §1 after
  it. Note: Narrator also narrates Windows itself, which makes a sighted
  pass hard.
- §3, colour-blind simulation: **pass**. Everything stays distinguishable,
  with good contrast.
- §4, zoom to 200 % and 400 %: **pass**. It scales and stays usable; heavy
  scrolling at 400 % is as expected.
- §6, print preview: **pass**. Light, outline-style output.
- §7, touch targets with a finger (Simon's touchscreen laptop): **pass**.
  Usable; fader-knob sizing may need tuning after real use.
- Not yet run: §2 (TalkBack, waiting until the hirer pages are configured),
  §5 (muted text with the house lights down), §8 (reduced motion) and §9
  (stage plan text at the operator's distance), the last two added after the
  re-run milestone audit and **not yet run**.

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

## 8. Reduced motion (not yet run)

The CSS rule is unit-tested (`reducedMotion.test.ts`); this checks the whole
interface for layout that *depends* on an animation finishing.

- [ ] Windows 11: Settings → Accessibility → Visual effects → **Animation
      effects** off (Edge follows it as `prefers-reduced-motion`). Reload the
      operator interface.
- [ ] Open and close a sheet (Add a device), a dialog, a menu and the inline
      help popover: each appears and disappears at once, with no slide or
      fade, and nothing else on the screen jumps as it does.
- [ ] Trigger a toast (save something) and a banner (unplug a device or force
      a status change): both appear in place without motion, and neither
      shifts the controls underneath them unexpectedly.
- [ ] Run a scene: the result reveal is present immediately, and the status
      LEDs do not pulse.
- [ ] Turn Animation effects back on afterwards.

Date and result: ______

## 9. Stage plan text at the operator's distance (not yet run)

The stage plan's fixture labels are SVG text, which zoom and contrast tests
cannot judge for real.

- [ ] At the booth or FOH position, on the touch PC at its normal scale, read
      every fixture label on the stage plan from where the operator actually
      sits, without leaning in. Labels must be legible and must not overlap
      one another or the fixture shapes.
- [ ] Repeat on a phone held at arm's length for the busiest plan.
- [ ] Anything that fails is a note against the label size or the plan's
      fixture spacing, not against this check.

Date and result: ______

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
- Real touch-target sizes on every admin screen and operator tab (44×44 px)
  at the D5 viewport and a phone viewport, and the muted-text contrast of
  the menu, toast, popover, sheet and dialog layers —
  `tests/e2e/accessibility.spec.ts` and `web/src/styles/contrast.test.ts`.
