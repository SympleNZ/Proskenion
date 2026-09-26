# The controller this replaces

The auditorium currently runs a small C# service, `AuditoriumControlSimple`.
It was read on 2026-09-18 for what it says about the real
devices and the KNX addresses the wall panels use today. Its source is not in
this repository. **No credentials are copied here.** The projector's PJLink
password belongs in `HANDOVER.md`, which is never committed (§20.3).

## Devices and addresses

| Device | Address | Notes |
|---|---|---|
| Projector, Panasonic PT-EZ570E | 10.2.30.249 | PJLink, authentication **on** |
| KNX IP interface | 10.2.30.252 | the old service tunnelled to it directly |
| Old controller's Art-Net source | 10.2.30.250 / 255.255.255.0 | |

A Panasonic PT-VW360 is available as a bench stand-in while the PT-EZ570E is
out of reach of the development network.

## PJLink, as the old service drives it

- **A connection per command.** It opens TCP 4352, reads the greeting, sends
  one command terminated with CR, reads one reply, and closes.
- **Greeting.** `PJLINK 0` means no authentication. `PJLINK 1 <nonce>` means
  the MD5 of the nonce concatenated with the password, in hex, is prefixed to
  the command. This matches §7.4.
- **Replies.** `=ERR1` undefined command, `=ERR2` out of parameter, `=ERR3`
  unavailable time, `=ERR4` projector failure, ` ERRA` authentication failure.
- **Commands used:** `%1POWR ?|0|1`, `%1INPT ?` and `%1INPT <type><port>`, and
  `%1ERST ?`, `%1LAMP ?`, `%1NAME ?`, `%1INF1 ?`, `%1INF2 ?`, `%1INFO ?`. All
  are PJLink Class 1; nothing in it uses Class 2.
- The PJLink code derives from `github.com/uow-dmurrell/ProjectorControl`.

## KNX group addresses the panels use today

For slice B, to set beside the integrator's export. The integrator's file is
authoritative; this is what the current system listens to and writes.

| Address | Meaning in the old service | Direction |
|---|---|---|
| 1/0/0 | Audio volume | from panel |
| 2/0/0 | HDMI matrix input select (values 1–4); a read asks for the current input | from panel |
| 3/0/0 | Projector on/off | from panel |
| 4/0/0 | Stage lights row 1 (front) | from panel |
| 4/0/1 | Stage lights row 2 | from panel |
| 4/0/2 | Stage lights row 3 | from panel |
| 4/0/3 | Stage lights row 4 (rear) | from panel |
| 4/0/4 – 4/0/7 | Rows 1–4 on/off feedback | written by the service |
| 4/0/8 | Stage lights all on/off | from panel |
| 4/0/9 | All-on feedback | written by the service |
| 6/1/0 | Projector power status feedback | written by the service |

The stage rows map to §8.8's four stage banks, and 4/0/4 to 4/0/7 and 4/0/9 to
their derived statuses. The old service wrote feedback as events rather than
deriving it from state, which is the pattern B51 replaced.
