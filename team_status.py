"""Team portals that are switched off.

A paused team keeps all of its data (users, tenders, runs, configs, downloads)
but gets no service: its users cannot sign in, existing sessions stop working,
and no scrape runs for it — scheduled, catch-up or manual.

To bring a portal back, remove its slug from PAUSED_TEAMS and restart the app.
See "TMI Portal shutdown.md" for the full story of the TMI pause.
"""

# Paused 2026-09-11: TMI team not actively using the portal (see "TMI Portal shutdown.md")
PAUSED_TEAMS = {"tmi"}


def is_team_paused(team_id: str | None) -> bool:
    return (team_id or "cnk").strip().lower() in PAUSED_TEAMS


def paused_message(team_id: str | None) -> str:
    name = (team_id or "").strip().upper() or "This"
    return f"The {name} portal is paused for now. Please contact the CNK team to have it turned back on."
