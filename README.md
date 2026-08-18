# git-leaderboard

Štatistiky prispievania z GitHub orgu — leaderboard, dashboard a **per-person
história od prvého commitu po dnešok**.

FastAPI + Jinja + Postgres. Dáta sa ťahajú z GitHub GraphQL API (commit history
naprieč všetkými branchami, deduplikované podľa commit OID) a ukladajú sa do
týždenných bucketov (`weekly_stats`) per človek + repo.

## Spustenie

```bash
cp .env.example .env      # doplň GITHUB_TOKEN a GITHUB_ORG
docker compose up --build
# http://localhost:8000
```

Token potrebuje `repo` (čítanie privátnych repozitárov orgu) a `read:org`.

### Viac organizácií

```
GITHUB_ORGS=EsportDynamics,DruhaFirma
GITHUB_TOKEN_ESPORTDYNAMICS=github_pat_...
GITHUB_TOKEN_DRUHAFIRMA=github_pat_...
```

Názov premennej s tokenom je `GITHUB_TOKEN_<ORG>` — org veľkými písmenami, všetko
okrem písmen a číslic nahradené podčiarkovníkom (`Druha-Firma` →
`GITHUB_TOKEN_DRUHA_FIRMA`). Kto vlastný token nemá, použije spoločný
`GITHUB_TOKEN`. Jedna organizácia sa dá stále zadať aj postaru cez `GITHUB_ORG`.

V hlavičke appky potom pribudne prepínač organizácie (pri jednej sa nezobrazuje).
Výber sa drží v cookie, takže platí naprieč stránkami; `?org=NazovOrgu` v URL ho
prebije, čo sa hodí na priame odkazy a na API. Prepínač ovplyvní všetko —
leaderboard, dashboard, ľudí, repá, CSV aj JSON. „Sync all" sa pri vybranom orgu
zmení na sync len tej organizácie.

Repá si org pamätajú v DB, takže dáta organizácie, ktorú z configu odoberieš,
nezmiznú — v prepínači ostane a dá sa v nej listovať, len sa už nesynchronizuje.

## Stránky

| URL | Čo ukazuje |
| --- | --- |
| `/` | Leaderboard za týždeň / 30 dní / all-time, voliteľne filtrovaný na jedno repo |
| `/dashboard` | Org-wide trendy po týždňoch, top ľudia a repá |
| `/people` | **Každý človek × mesiac** ako mriežka (default), alebo súhrn za celú históriu |
| `/people/{login}` | Detail človeka: mesačné grafy a tabuľka, rozpad po rokoch a repách |
| `/people.csv` | Mesačné dáta v long formáte (`?by=total` dá súhrn) — priamo do kontingenčky |
| `/api/people/monthly` | To isté ako JSON — na postavenie vlastného frontendu |
| `/repos` | Zoznam repozitárov, dokedy má ktoré backfillnutú históriu, manuálny sync |

## Metriky

Per človek sa počíta:

- commity, pridané / zmazané riadky, net riadky, zmenené súbory
- aktívne týždne (týždeň, v ktorom má aspoň jeden commit) a počet repozitárov
- **riadky / commit** a súbory / commit
- **+ riadky / aktívny týždeň**, **commity / aktívny týždeň**

Rýchlosti sú normalizované na *aktívny* týždeň, nie na kalendárny — inak by
dovolenky a nábeh nových ľudí robili z čísel nezmysel.

Všetko sa dá pozerať **po mesiacoch**: `/people` je defaultne mriežka ľudia ×
mesiace (metrika a rozsah sa prepínajú v hlavičke, podfarbenie ukazuje intenzitu),
detail človeka má mesačnú tabuľku aj grafy a CSV export je defaultne v long
formáte `login, month, …`. Týždeň patrí mesiacu, v ktorom začal — týždenný
bucket sa nedá rozdeliť medzi dva mesiace.

Aplikácia sama nikde neurčuje žiadne deliace obdobie — dáta sú súvislá história,
interpretácia je na tebe.

Merge commity sa nezapočítavajú (ich diff duplikuje obsah vetvy) a commity,
ktorých autor nie je nalinkovaný na GitHub účet, sa preskakujú.

## Prihlásenie

Appka vie HTTP Basic auth. Zapne sa vyplnením oboch premenných v `.env`:

```
AUTH_USER=milo
AUTH_PASSWORD=nieco-dlhe-a-nahodne
```

Platí na všetko vrátane `/api/*` a `/static/*`; jediná výnimka je `/healthz`
(kvôli healthchecku) a CORS preflight `OPTIONS`. Kým sú premenné prázdne, appka
je otvorená komukoľvek, kto sa na ňu dostane — do logu to pri štarte píše
varovanie.

Basic auth posiela heslo v každom requeste (base64, nie šifrované), takže na
servri to daj **za HTTPS** — reverse proxy s certifikátom, alebo len do VPN.

Vlastný frontend sa autentifikuje rovnako:

```js
fetch("https://.../api/people/monthly", {
  headers: { Authorization: "Basic " + btoa("milo:heslo") },
})
```

## API pre vlastný frontend

`GET /api/people/monthly` vráti per človek / per mesiac: **commity, pridané a
zmazané riadky (aj net), počet upravených repozitárov**, zmenené súbory, aktívne
týždne a riadky/commit.

```
GET /api/people/monthly                    # celá história, všetky organizácie
GET /api/people/monthly?months=12          # posledných 12 mesiacov
GET /api/people/monthly?login=jkovac       # jeden človek
GET /api/people/monthly?org=EsportDynamics # jedna organizácia
```

```jsonc
{
  "org": "...", "generated_at": "...",
  "range": { "months": 12, "since": "2025-09-01" },
  "coverage": { "first_week": "...", "repos_total": 12, "repos_backfilled": 12, "complete": true },
  "org": "EsportDynamics",                   // null = všetky dokopy
  "orgs": ["EsportDynamics", "DruhaFirma"],  // čo sa dá vybrať
  "months": ["2025-09", "..."],              // všetky mesiace v odpovedi
  "people": [{
    "login": "...", "avatar_url": "...", "html_url": "...",
    "first_activity": "2019-02-03", "last_activity": "2026-08-09",
    "totals_in_range": { "commits": 0, "additions": 0, "deletions": 0, "net_lines": 0, "...": 0 },
    "months": [{ "month": "2025-09", "commits": 12, "additions": 3400, "deletions": 900,
                 "net_lines": 2500, "changed_files": 88, "repos": 3,
                 "active_weeks": 4, "lines_per_commit": 283.33 }]
  }]
}
```

`totals_in_range` je súčet za mesiace v odpovedi, nie za celú históriu — aby
sedel s poľom `months` aj pri filtri. Mesiace bez aktivity sa nevypisujú;
zoznam `months` na najvyššej úrovni je union naprieč všetkými ľuďmi.

Ak frontend beží na inej doméne, povoľ ju v `API_CORS_ORIGINS` (comma-separated,
prázdne = CORS vypnuté). API je len na čítanie, GET-only.

## Sync

Na stránke **Repos** je `⤓ Načítať zoznam repozitárov` — stiahne len zoznam repo
z orgu (pár sekúnd, žiadna commit história), takže hneď vidíš, čo tam je, a
každé repo si vieš odsyncovať zvlášť tlačidlom `↻ Sync` / `⟲ Full`.

Samotné sťahovanie dát má dva režimy, oba sa dajú spustiť ručne aj bežia automaticky:

- **Full backfill** — prejde celú históriu repa od prvého commitu. Beží
  automaticky pre každé repo, ktoré ešte nemá `history_synced_from`, čiže po
  nasadení tejto verzie sa história dotiahne sama pri najbližšom syncu.
  Tlačidlo *⟲ Full backfill* (celý org) alebo *⟲ Full* (jedno repo) ho vynúti
  znovu.
- **Inkrementálny** — nočný cron (`SYNC_CRON_HOUR`), znovu prečíta posledných
  `SYNC_OVERLAP_DAYS` dní a prepíše dotknuté týždne. Prekryv pokrýva rebase a
  force-push; týždne mimo okna zostávajú nedotknuté.

Zápis je delete-then-insert v rámci okna, nie slepý upsert — inak by
force-push nikdy nevedel číslo znížiť a opakované čítanie by sa pripočítavalo.

Prvý full backfill veľkého orgu je najdrahšia operácia (chodí sa cez všetky
branche); GraphQL volania majú backoff na rate limit. Ak by to bolo priveľa,
`SYNC_HISTORY_DAYS` sa dá nastaviť na strop v dňoch.

## Dev

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

Schéma sa vytvára cez `init_db()` pri štarte (`Base.metadata.create_all` +
zopár `ALTER TABLE ... IF NOT EXISTS` inline migrácií). Keď sa schéma začne
meniť častejšie, nasadiť Alembic.
