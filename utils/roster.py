"""
Roster math. Pure functions over signup rows, so the rules are testable alone.

The first `slots` IN rows by (joined_at, id) play; the rest of the IN rows are
the waitlist, in the same order. Nothing about position is stored, so when a
confirmed player drops, the waitlist head is promoted with no extra write.
"""

from dataclasses import dataclass, field

from utils.database import IN, MAYBE


@dataclass
class Roster:
    slots: int
    confirmed: list[dict] = field(default_factory=list)
    waitlist: list[dict] = field(default_factory=list)
    maybe: list[dict] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.confirmed)

    @property
    def need(self) -> int:
        return max(0, self.slots - len(self.confirmed))

    @property
    def full(self) -> bool:
        return self.need == 0


def build_roster(signups: list[dict], slots: int) -> Roster:
    ordered = sorted(signups, key=lambda s: (s["joined_at"], s["id"]))
    playing = [s for s in ordered if s["status"] == IN]
    return Roster(
        slots=slots,
        confirmed=playing[:slots],
        waitlist=playing[slots:],
        maybe=[s for s in ordered if s["status"] == MAYBE],
    )


def promoted(before: Roster, after: Roster) -> list[dict]:
    """Rows that were waitlisted (or absent) before and are confirmed now."""
    was_confirmed = {s["id"] for s in before.confirmed}
    was_waiting = {s["id"] for s in before.waitlist}
    return [s for s in after.confirmed if s["id"] in was_waiting and s["id"] not in was_confirmed]
