"""Pull a player's CAREER figures from the SHL APIs into data.json.

Two hosts are involved. The portal is the live source of record for identity,
TPE and the 28 attributes, and gives all of it in one call. The index is the
sim's output and supplies the club name and the on-ice stats.

The index keeps one record per season per club. This script fetches all of them
for the leagues in CAREER_LEAGUES and adds them up, so data.json's single stats
object holds career totals to date. Counting figures are plain sums. The
possession and rate figures (CF%, FF%, PDO, the per-60s) cannot be added, so
they are averaged across seasons weighted by time on ice.

Everything is deterministic: the same API answers always produce the same file.
That keeps it byte-stable between runs, so the workflow can skip commits when
nothing moved, and leaves the commit log usable as a dated record of progress.
"""
import json
import pathlib
import re
import ssl
import sys
import urllib.error
import urllib.request

# Set both before the first run. PLAYER_ID is the number in your player's page URL on
# portal.simulationhockey.com. PLAYER_NAME is the name exactly as the portal spells it;
# if the portal returns anyone else under that id, the run stops before writing anything.
PLAYER_ID = 2435
PLAYER_NAME = "Jagger Eriksson"

PORTAL_PLAYER = "https://portal.simulationhockey.com/api/v1/player?pid={pid}"
INDEX_TEAM = "https://index.simulationhockey.com/api/v1/teams/{team}?league={league}"
INDEX_STATS = "https://index.simulationhockey.com/api/v1/players/stats/{iid}?league={league}&type={phase}"
INDEX_SEARCH = "https://index.simulationhockey.com/api/v2/player/playerSearch?league={league}"

# Club crests are not an API. Each league ships one SVG sprite stack holding
# every team's mark as a nested <svg> keyed by the team's city, spaces becoming
# underscores. Checked against the live team list: 24/24 SHL and 16/16 SMJHL
# resolve by that rule. (The national leagues key on nameDetails.second instead,
# which a club signature never needs.)
INDEX_STACK = "https://index.simulationhockey.com/stack/{league}.stack.svg"

# The index documents these as "rs", "ps" and "po". Those values are silently
# ignored and fall through to regular season, so passing them looks like it
# works and quietly gives the wrong data. Only the full words select anything.
REGULAR = "regular"
PLAYOFFS = "playoffs"

# The index numbers its leagues in /api/v1/leagues, and the portal's own
# indexRecords use that same numbering, so one map serves both lookups.
LEAGUE_IDS = {"SHL": 0, "SMJHL": 1, "IIHF": 2, "WJC": 3}

# Only these two carry a club season. IIHF and WJC are tournaments, and a
# national-team run must never stand in for a league record on a club signature.
CLUB_LEAGUES = ("SHL", "SMJHL")

# Which leagues' seasons are added into the career totals. ("SHL",) is the SHL
# career alone. Use ("SMJHL", "SHL") to count junior seasons as well. A league
# the player has no index record in is skipped.
CAREER_LEAGUES = ("SHL",)

# Club colours for the signature, by team abbreviation. A trade picks up the new
# club's entry on the next run. A club missing from here gets the default palette.
TEAM_COLORS = {
    "SFP": {"primary": "#d4af37", "secondary": "#360854", "base": "#360854"},
}

# The crest only changes on a trade, so it is re-downloaded only when logo.svg is
# missing, unreadable, or holds a different club's mark. Pass --refresh-logo to
# force a fresh copy anyway (say, after the league redraws its crests).
REFRESH_LOGO = "--refresh-logo" in sys.argv[1:]

DATA_PATH = pathlib.Path(__file__).parent / "data.json"
LOGO_PATH = pathlib.Path(__file__).parent / "logo.svg"

# The portal answers 403 to urllib's default "Python-urllib/3.x". Identify the
# job and where it comes from, so whoever runs the API can see who is calling.
# Add your repo URL after the name if you have one, e.g. "(+https://github.com/you/repo)".
USER_AGENT = "SHL-Career-Sig/1.0"

PLAYER_FIELDS = {
    "name": str,
    "position": str,
    "handedness": str,
    "height": str,
    "weight": int,
    "birthplace": str,
    "jerseyNumber": int,
    "draftSeason": int,
    "totalTPE": int,
    "appliedTPE": int,
    "bankedTPE": int,
    "currentLeague": str,
    "currentTeamID": int,
}

ATTRIBUTES = (
    "screening", "gettingOpen", "passing", "puckhandling", "shootingAccuracy",
    "shootingRange", "offensiveRead", "checking", "hitting", "positioning",
    "stickchecking", "shotBlocking", "faceoffs", "defensiveRead", "acceleration",
    "agility", "balance", "speed", "stamina", "strength", "fighting", "aggression",
    "bravery", "determination", "teamPlayer", "leadership", "temperament",
    "professionalism",
)

PHASE_FIELDS = (
    "gamesPlayed", "goals", "assists", "points", "plusMinus", "pim",
    "hits", "shotsBlocked", "takeaways", "giveaways", "shotsOnGoal", "timeOnIce",
    "ppPoints", "shPoints", "ppTimeOnIce", "shTimeOnIce",
)
STAT_FIELDS = ("season",) + PHASE_FIELDS

ADVANCED_FIELDS = ("CFPct", "FFPct", "PDO", "GF60", "GA60", "SF60", "SA60")


class ShapeError(Exception):
    """The API answered, but not with what we need to build a valid signature."""


CERT_HELP = (
    "Fix: run `pip install --upgrade certifi` (this script uses it automatically once installed). "
    "On macOS with a python.org Python, running 'Install Certificates.command' in the Python "
    "folder inside Applications does the same. If you are on a work network or have antivirus "
    "that inspects HTTPS, export its root certificate as a .pem file and set the SSL_CERT_FILE "
    "environment variable to that file's path."
)


def make_ssl_context():
    """The system's trusted roots, plus certifi's bundle when it is installed.

    Python on macOS and some Windows setups ships without usable roots, which is
    what produces 'unable to get local issuer certificate'. Adding certifi's
    bundle on top of the system store fixes that without dropping any root a
    work network may have installed. Verification stays on.
    """
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        return context
    context.load_verify_locations(cafile=certifi.where())
    return context


SSL_CONTEXT = make_ssl_context()


def read_url(url, headers, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
            if response.status != 200:
                raise ShapeError(f"{url} returned HTTP {response.status}")
            return response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise ShapeError(
                f"{url} failed certificate verification ({exc.reason.verify_message}). {CERT_HELP}"
            ) from exc
        raise ShapeError(f"{url} unreachable: {exc}") from exc


def get_json(url):
    body = read_url(url, {"Accept": "application/json"}, timeout=30)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ShapeError(f"{url} returned malformed JSON: {exc}") from exc


def require_fields(record, spec, where):
    """Check presence and type, reporting every problem at once rather than the first."""
    problems = []
    for field, want in spec.items():
        if field not in record:
            problems.append(f"missing {field!r}")
        elif not isinstance(record[field], want) or isinstance(record[field], bool):
            problems.append(f"{field!r} is {type(record[field]).__name__}, want {want.__name__}")
    if problems:
        raise ShapeError(f"{where}: " + "; ".join(problems))


def fetch_player():
    if PLAYER_ID is None or not PLAYER_NAME:
        raise ShapeError("set PLAYER_ID and PLAYER_NAME at the top of this file first")
    payload = get_json(PORTAL_PLAYER.format(pid=PLAYER_ID))
    if not isinstance(payload, list) or len(payload) != 1:
        raise ShapeError(
            f"portal /player?pid={PLAYER_ID} returned "
            f"{len(payload) if isinstance(payload, list) else type(payload).__name__} "
            "records, want exactly 1"
        )
    player = payload[0]
    require_fields(player, PLAYER_FIELDS, "portal player record")
    if player["name"] != PLAYER_NAME:
        raise ShapeError(
            f"portal pid {PLAYER_ID} is {player['name']!r}, not {PLAYER_NAME!r}; "
            "check PLAYER_ID and PLAYER_NAME"
        )

    attributes = player.get("attributes")
    if not isinstance(attributes, dict):
        raise ShapeError("portal player record: 'attributes' is not an object")
    missing = [a for a in ATTRIBUTES if a not in attributes]
    if missing:
        raise ShapeError(f"portal attributes missing {len(missing)}: {', '.join(missing)}")
    unexpected = sorted(set(attributes) - set(ATTRIBUTES))
    if unexpected:
        raise ShapeError(f"portal returned unrecognised attributes: {', '.join(unexpected)}")
    for name, value in attributes.items():
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 20:
            raise ShapeError(f"attribute {name!r} is {value!r}, want an integer 0-20")

    if player["currentLeague"] not in LEAGUE_IDS:
        raise ShapeError(
            f"unknown league {player['currentLeague']!r}; known: {', '.join(LEAGUE_IDS)}"
        )
    return player


def index_ids_by_league(player):
    """Every league the index has opened a record for the player in, as {leagueID: indexID}."""
    records = player.get("indexRecords")
    if not isinstance(records, list):
        raise ShapeError("portal player record: 'indexRecords' is not a list")
    found = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        league_id, index_id = record.get("leagueID"), record.get("indexID")
        if isinstance(league_id, int) and isinstance(index_id, int):
            found[league_id] = index_id
    if not found:
        raise ShapeError("portal player record: no usable indexRecords entries")
    return found


def search_index_id(name, league_id):
    """The player's index ID in a league by exact name, or None unless exactly one matches.

    The portal can be slow to link a new index record: a player may be several
    games into a season in the index while the portal still lists only the
    previous league. Refusing an ambiguous match means a namesake can never
    stand in for the player.
    """
    roster = get_json(INDEX_SEARCH.format(league=league_id))
    if not isinstance(roster, list):
        raise ShapeError(f"index playerSearch?league={league_id} is not a list")
    matches = {
        entry["PlayerID"]
        for entry in roster
        if isinstance(entry, dict) and entry.get("Name") == name
        and isinstance(entry.get("PlayerID"), int)
    }
    return matches.pop() if len(matches) == 1 else None


def resolve_index_ids(player):
    """{leagueID: indexID} from the portal's links, topped up from the index itself.

    The portal can lag the index, so when the current club league has no link the
    index's own player search is tried before giving up on that league.
    """
    available = index_ids_by_league(player)
    current = player["currentLeague"]
    if current in CLUB_LEAGUES and LEAGUE_IDS[current] not in available:
        found = search_index_id(player["name"], LEAGUE_IDS[current])
        if found is not None:
            available = {**available, LEAGUE_IDS[current]: found}
    return available


def career_sources(player):
    """[(league name, league id, index id)] for every career league the player has a record in."""
    unknown = [name for name in CAREER_LEAGUES if name not in CLUB_LEAGUES]
    if unknown:
        raise ShapeError(
            f"CAREER_LEAGUES has {', '.join(unknown)}; only {', '.join(CLUB_LEAGUES)} can count"
        )
    available = resolve_index_ids(player)
    sources = [
        (name, LEAGUE_IDS[name], available[LEAGUE_IDS[name]])
        for name in CAREER_LEAGUES
        if LEAGUE_IDS[name] in available
    ]
    if not sources:
        raise ShapeError(
            f"no index record in {', '.join(CAREER_LEAGUES)}; the portal links league IDs "
            f"{sorted(available)}. Add the right league to CAREER_LEAGUES."
        )
    return sources


def fetch_team(team_id, league_id):
    team = get_json(INDEX_TEAM.format(team=team_id, league=league_id))
    if not isinstance(team, dict) or not team:
        raise ShapeError(f"index /teams/{team_id}?league={league_id} returned no team")
    require_fields(team, {"name": str, "abbreviation": str}, f"index team {team_id}")
    if team.get("id") != team_id:
        raise ShapeError(f"asked index for team {team_id}, got {team.get('id')}")
    # Returned whole rather than trimmed here: the crest lookup needs
    # nameDetails, which does not belong in data.json.
    return team


def fetch_phase(index_id, league_id, phase):
    records = get_json(INDEX_STATS.format(iid=index_id, league=league_id, phase=phase))
    if not isinstance(records, list):
        raise ShapeError(f"index {phase} stats for {index_id} is not a list")
    spec = {f: int for f in STAT_FIELDS}
    spec["team"] = str  # who they played these games for, which is not always their current club
    for record in records:
        require_fields(record, spec, f"index {phase} stats {index_id}")
    return records


def fetch_records(sources):
    """Every season played, as (regular records, playoff records), league-tagged.

    The index has one record per season per club, so a mid-season trade is two
    records and both are wanted. What must never happen is the same record
    arriving twice, which would silently double a season; that stops the run.
    """
    regular, playoffs = [], []
    for league_name, league_id, index_id in sources:
        for phase, bucket in ((REGULAR, regular), (PLAYOFFS, playoffs)):
            seen = {}
            for record in fetch_phase(index_id, league_id, phase):
                if record["gamesPlayed"] <= 0:
                    continue
                key = (record["season"], record["team"])
                if record in seen.get(key, []):
                    raise ShapeError(
                        f"index {phase} stats for {league_name} {index_id} list S{key[0]} "
                        f"{key[1]} twice with identical figures; summing would double-count it"
                    )
                seen.setdefault(key, []).append(record)
                bucket.append({**record, "league": league_name})
    if not regular:
        raise ShapeError("the index shows no regular-season games played in any career league")
    return regular, playoffs


def total_counting(records):
    """Plain sums. Time on ice stays total seconds, so the build's per-game averages work."""
    return {field: sum(record[field] for record in records) for field in PHASE_FIELDS}


def career_advanced(records):
    """Career-wide possession and rate figures: a season average weighted by time on ice.

    These are rates, so adding them would be meaningless. Time on ice is the
    closest weight the index gives; it is exact for the per-60s only if the
    index's rates and time on ice cover the same situations, and an
    approximation for CF%, FF% and PDO, which are really weighted by events.
    Seasons the index has no complete advancedStats for (older ones may not)
    are left out of the average rather than counted as zero.
    """
    def complete(record):
        advanced = record.get("advancedStats")
        return isinstance(advanced, dict) and all(
            isinstance(advanced.get(f), (int, float)) and not isinstance(advanced.get(f), bool)
            for f in ADVANCED_FIELDS
        )

    usable = [record for record in records if complete(record)]
    if not usable:
        raise ShapeError("no season carries a complete advancedStats block")
    weights = [record["timeOnIce"] for record in usable]
    if sum(weights) <= 0:
        weights = [record["gamesPlayed"] for record in usable]
    total = sum(weights)
    if len(usable) < len(records):
        print(f"note: advanced stats average covers {len(usable)} of {len(records)} season records")
    return {
        field: round(sum(r["advancedStats"][field] * w for r, w in zip(usable, weights)) / total, 3)
        for field in ADVANCED_FIELDS
    }


def career_stats(regular, playoffs):
    """The single stats object: career totals, labelled with the newest season in them."""
    latest = max(regular, key=lambda r: (r["season"], r["gamesPlayed"]))
    stats = {
        "firstSeason": min(r["season"] for r in regular),  # the career span runs from here...
        "season": latest["season"],  # ...through the newest season the totals include
        "team": latest["team"],      # whose sweater that season was earned in
        "league": latest["league"],
        "regular": total_counting(regular),
    }
    stats["regular"]["advanced"] = career_advanced(regular)
    # Playoff cards appear once any playoff games exist in the career, as before.
    if playoffs:
        stats["playoffs"] = total_counting(playoffs)
    return stats


def get_text(url):
    # The stacks run to a few megabytes, so this wants a longer rope than the
    # JSON calls get.
    return read_url(url, {}, timeout=120)


def symbol_id(team):
    """The sprite key for a club: its city with spaces as underscores."""
    details = team.get("nameDetails")
    if not isinstance(details, dict) or not isinstance(details.get("first"), str):
        raise ShapeError(f"index team {team.get('name')!r} has no nameDetails.first to key on")
    return details["first"].replace(" ", "_")


def kept_mark(key):
    """The crest already in logo.svg, if it is this club's and looks complete; else None.

    Every mark lifted from a stack keeps its sprite id on the opening tag, so the
    file says by itself whose crest it is. A hand-placed logo with no id never
    matches, and is replaced by a fetched one on the next run.
    """
    try:
        text = LOGO_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    opening = re.match(r"<svg\b[^>]*>", text)
    if not opening or 'viewBox="' not in opening.group(0) or not text.endswith("</svg>"):
        return None
    identifier = re.search(r'\sid="([^"]+)"', opening.group(0))
    if not identifier or identifier.group(1) != key or "<path" not in text:
        return None
    return text


def fetch_mark(team, league):
    """The club crest, lifted out of its league's sprite stack.

    Written to its own file rather than into data.json: it is markup, not data,
    and data.json is documented as raw API values only. It also changes only on
    a trade, so an unchanged club leaves the file byte-identical and the nightly
    workflow stays quiet.
    """
    key = symbol_id(team)
    if not REFRESH_LOGO:
        kept = kept_mark(key)
        if kept:
            print(f"note: {LOGO_PATH.name} already holds the {key} crest; skipped the download")
            return kept
    stack = get_text(INDEX_STACK.format(league=league.lower()))
    match = re.search(r'<svg[^>]*\sid="%s"[^>]*>.*?</svg>' % re.escape(key), stack, re.DOTALL)
    if not match:
        available = sorted(set(re.findall(r'<svg[^>]*\sid="([^"]+)"', stack)))
        raise ShapeError(
            f"no mark {key!r} in the {league} stack; it holds {len(available)} symbols "
            f"including {', '.join(available[:6])}"
        )
    mark = match.group(0)
    if 'viewBox="' not in mark[: mark.index(">") + 1]:
        raise ShapeError(f"mark {key!r} has no viewBox, so the build cannot scale it")
    return mark


def collect():
    """Everything the build needs: the JSON payload, and the club crest markup."""
    player = fetch_player()
    club_league = player["currentLeague"]
    team = fetch_team(player["currentTeamID"], LEAGUE_IDS[club_league])

    regular, playoffs = fetch_records(career_sources(player))
    stats = career_stats(regular, playoffs)

    team_block = {"name": team["name"], "abbreviation": team["abbreviation"]}
    colors = TEAM_COLORS.get(team["abbreviation"])
    if colors:
        team_block["colors"] = dict(colors)
    else:
        print(
            f"note: no colours for {team['abbreviation']} in TEAM_COLORS; "
            "the signature will use the default palette"
        )

    data = {
        "player": {field: player[field] for field in PLAYER_FIELDS},
        "attributes": {name: player["attributes"][name] for name in ATTRIBUTES},
        "team": team_block,
        "stats": stats,
    }
    return data, fetch_mark(team, club_league)


def main():
    try:
        data, mark = collect()
    except ShapeError as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1

    # sort_keys and a trailing newline keep the file byte-stable between runs,
    # so an unchanged API produces an unchanged file and the workflow can tell.
    DATA_PATH.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    LOGO_PATH.write_text(mark + "\n", encoding="utf-8", newline="\n")

    player, stats, team = data["player"], data["stats"], data["team"]
    playoffs = stats.get("playoffs")
    # Name the source of the stats whenever it is not the club the player is on now,
    # so a call-up is visible in the log rather than looking like stale data.
    elsewhere = (
        "" if stats["team"] == team["abbreviation"] and stats["league"] == player["currentLeague"]
        else f" [stats from {stats['league']} {stats['team']}]"
    )
    print(
        f"{player['name']}: {player['totalTPE']} TPE "
        f"({player['appliedTPE']} applied, {player['bankedTPE']} banked), "
        f"{team['name']} ({player['currentLeague']}), career through S{stats['season']}: "
        f"{stats['regular']['gamesPlayed']}gp regular"
        + (f" + {playoffs['gamesPlayed']}gp playoffs" if playoffs else " (no playoff games)")
        + elsewhere
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
