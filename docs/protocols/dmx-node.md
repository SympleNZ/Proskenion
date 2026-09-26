# DMXking eDMX8 MAX — as installed

For: an engineer maintaining or taking over the system.

**Status:** partially commissioned — read from the real device on the VLAN;
universe assignment and the DMX-IN behaviour with a desk attached still wait
on the integrator (below).
**Scope:** this one node, as installed in the auditorium: its address,
its port assignment, and the two facts about it that the rest of the
application depends on but cannot see for itself.
**Applies to:** §7.2.7 (external lighting desk), §7.2.8 (health).

This file is facts about the physical box at `10.2.30.245`. The Art-Net
protocol it speaks — packet layout, ArtPoll/ArtPollReply, the one shared
socket — is `docs/protocols/artnet.md`; nothing here duplicates it.

## Identity

Read with `tools/artnet_probe.py` from a laptop on `10.2.30.0/24`
(21 September 2026 — a node answers a poll by broadcast, which a router will
not carry, so this only works from the same subnet):

| | |
|---|---|
| Address | `10.2.30.245` |
| Node name | `OBHS eDMX8 MAX` |
| Serial | `001A19440130` |
| MAC | `00:1a:19:44:01:30` |
| Firmware | 4.11 |

`docs/hardware/network_map.md` carries this same row in the site's address
table; this file is the fuller treatment §19.2 asks for.

## Port assignment

Eight physical ports, fixed in this direction by the node's own
configuration, not by anything this application sends:

| Ports | Direction | Universes |
|---|---|---|
| 6 | Output | 2, 3, 4, 5, 6, 7 |
| 2 | Input | 0, 1 |

The lighting output's universe is ordinary configuration
(`proskenion/core/dmx/artnet.py` takes it as a plain integer) — nothing here
is a code assumption. But it has to be set to match the node: a frame sent to
universe 0 or 1 today would arrive at a port configured as an *input* and
light nothing, because the node's own two input ports sit on exactly those
numbers.

## Health and desk detection

**ArtPollReply is broadcast, not unicast, whatever port the poll came
from.** The node sends every reply to the VLAN's broadcast address
(`10.2.30.255:6454`) rather than back to the sender — confirmed on site,
24 September 2026. `ArtNetEndpoint` (`proskenion/core/dmx/endpoint.py`) is
therefore the one socket bound to `0.0.0.0:6454` with `SO_BROADCAST`, in both
directions; a socket on an ephemeral port would see this node as permanently
unreachable even while it answers every poll, which is exactly the fault the
previous implementation had.

**Desk detection counts only frames whose source address is this node's
own — `10.2.30.245`, never the controller's.** This is a deliberate
deviation from a literal reading of §7.2.7 ("ArtDmx seen on the input
universe"), folded into §7.2.7 and §7.2.8 rather than left undocumented
(`ARCHITECTURE.md`, Phase 2 slice B). It matters because of what else is on
this VLAN during the parallel run with the previous system:

- the older **DMXking eDMX4 DIN at `10.2.30.220`** still drives the rig
  today, and broadcasts its own DMX input as ArtDmx on **universe 0** — one
  of this node's two configured input universes;
- the **legacy controller PC at `10.2.30.250`** polls for Art-Net nodes on
  its own account.

Without the source-address filter, either of those would hold the room under
external control indefinitely, because their traffic satisfies a naive read
of "ArtDmx seen on universe 0". A visiting desk reaches this application
only through the eDMX8 MAX's own DMX-IN port, broadcast from the node's own
address, so the filter loses nothing a real desk needs — it only excludes
traffic this node itself never sent.

## Open — waiting on the integrator

Two decisions have not been made, and nothing below is a code defect; both
are configuration on the node and the rig, not this application:

1. **Which universes the system drives.** Left as the node stands, DMX
   output uses universes 2–7. The alternative is repatching the node to
   output on universe 0, matching the wiring the old eDMX4 rig uses today.
   Whichever is chosen, the eDMX4 at `.220` is on universe 0 right now, so
   running both nodes on universe 0 at once would mean two nodes answering
   for the same addresses — this has to be resolved before both can be live
   together, not just before cutover.
2. **The DMX-IN broadcast behaviour with a real desk plugged in.** §7.2.7
   depends on the node's DMX-IN port being configured with its Broadcast
   Threshold set to zero, so it broadcasts whatever a visiting desk sends as
   ArtDmx rather than waiting for a change. This has been read from the
   node's own port configuration, but never watched with an actual desk
   patched into the booth's DMX-IN and driving it — the bench test that
   would confirm it is one of the sessions still owed from Phase 2 slice B
   (`docs/plans/phase-completion.md`, wave D). Until it runs, whether a
   visiting desk's frames actually reach this application the way the
   configuration implies is confirmed on paper, not on the bench.

Both are listed again in this task's report as items for the coordinator to
carry forward; this file records the facts, not the decision.
