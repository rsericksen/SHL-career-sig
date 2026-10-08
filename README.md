# SHL career signature

A 620×200 animated SVG signature for a Simulation Hockey League player: jersey
number, attribute radar, club crest, a TPE bar, and a rotating ticker of career
stats. It pulls the data from the league's APIs, draws the SVG in the club's
colours, and (optionally) rebuilds and republishes itself every night on GitHub
Pages.

## How it fits together

| File | Job |
| --- | --- |
| `fetch_career.py` | Pulls your player's data from the SHL portal and index, adds up every season, writes `data.json` and `logo.svg`. |
| `build_career.py` | Fills `sig.career.template.svg` from `data.json` and writes the finished SVG. Refuses to write if any check fails. |
| `sig.career.template.svg` | The design. Colours, spacing and card layout live here. |
| `data.json` | The fetched data. Regenerated each run; the commit history is a dated record of your career. |
| `logo.svg` | Your club's crest, downloaded once and reused until the club changes. |
| `.github/workflows/update-signature.yml` | The nightly job. |

Both scripts use only Python's standard library. Nothing to install.

## Set up

1. **Set your player.** Open `fetch_career.py` and fill in the two lines near the top:
   `PLAYER_ID` (the number in your player's page URL on portal.simulationhockey.com)
   and `PLAYER_NAME` (exactly as the portal spells it). If the id returns anyone
   else, the run stops before writing anything.
2. **Set your club's colours** in `TEAM_COLORS`, keyed by team abbreviation:
   ```python
   TEAM_COLORS = {
       "SFP": {"primary": "#d4af37", "secondary": "#360854", "base": "#360854"},
   }
   ```
   - `primary` (required) is the main accent: radar, position line, applied-TPE bar.
   - `secondary` (optional) is the banked-TPE segment and the faint season label.
     Leave it out and a complementary colour is derived.
   - `base` (optional) only tints the dark background. Use it when a club's main
     colour is too dark to read as an accent (navy, maroon).
   - A colour too dark for the background is lightened automatically, and the
     build prints a note. A pair too close to tell apart is refused.
   - A club missing from the table gets the default cyan and pink. After a trade,
     add the new club's entry.
3. **Choose which leagues count** with `CAREER_LEAGUES` (default `("SHL",)`).
   Use `("SMJHL", "SHL")` to include junior seasons.
4. **Run it once locally** to check:
   ```
   python fetch_career.py
   python build_career.py
   ```
   Open `jagger.career.svg` in a browser.

### If the fetch fails with a certificate error

Your Python can't find trusted root certificates. Run `pip install --upgrade certifi`
(the script uses it automatically). On a Mac with python.org Python you can run
`Install Certificates.command` from the Python folder in Applications instead. On
a work network that inspects HTTPS, set the `SSL_CERT_FILE` environment variable
to your organisation's root certificate (a `.pem` file).

### If the label at the bottom right says CAREER

`data.json` has no `firstSeason`. Run `python fetch_career.py` again with the
current script; it records your first and newest season, and the label then reads
`S88–90` (or just `S90` in a first season).

## Put it on GitHub Pages, updated nightly

1. Create a **public** repository (Pages is free for public repos) and push these
   files, with the workflow at `.github/workflows/update-signature.yml`.
   An optional `.gitignore`:
   ```
   jagger.career.svg
   __pycache__/
   .DS_Store
   ```
2. In the repository, open **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Open **Settings → Actions → General → Workflow permissions** and choose
   **Read and write permissions**, so the job can commit `data.json`.
4. Open the **Actions** tab, pick **Update signature**, and press **Run workflow**.
   The first run takes a minute or two.
5. Your signature is now at
   `https://<your-username>.github.io/<repository-name>/signature.svg`.
   Use that address as the image link in your forum signature, for example
   `[img]https://<your-username>.github.io/<repository-name>/signature.svg[/img]`.

From then on it runs by itself every night (07:17 UTC by default; change the
`cron` line to land after your league's sim). It also runs when you push a change
to the scripts, the template or the workflow, and whenever you press Run workflow.

### Things worth knowing

- **If a night's fetch fails** (the portal is down, say), the job stops before
  publishing and last night's signature stays up. GitHub emails you about the failure.
- **Forums cache images.** Pages tells browsers to keep a file for about ten
  minutes, and many forums or image proxies hold on much longer, so an update can
  take a while to show on your posts.
- **Scheduled jobs pause on quiet repos.** GitHub switches off scheduled workflows
  in a public repo after 60 days with no activity. The job writes a `.heartbeat`
  file once a month for exactly this reason, so the offseason doesn't switch it off.
- **Action versions.** The workflow uses current major versions of GitHub's own
  actions. If GitHub ever warns that one is deprecated, bump its version number.

## Adjusting the look

- **Jersey number intensity:** `NUMBER_FACE_FADE`, `NUMBER_TRIM_FADE` and
  `NUMBER_SHADOW_FADE` at the top of `build_career.py` (0 is full colour, 1 fades
  it entirely into the background).
- **Name width:** `NAME_MAX_WIDTH`. The name is given a fixed width so it looks
  the same whether or not a reader has Arial Narrow installed.
- **Force a fresh crest download:** `python fetch_career.py --refresh-logo`.

## Notes on the numbers

- Counting stats (games, goals, hits and so on) are plain sums across seasons.
  Time on ice is total seconds, so the per-game averages are right.
- CF%, FF%, PDO and the per-60 rates can't be added. They are averaged across
  seasons, weighted by time on ice. That is exact for rates measured over the
  same ice time and an approximation for CF%, FF% and PDO.
- Seasons the index has no advanced stats for are left out of that average.
