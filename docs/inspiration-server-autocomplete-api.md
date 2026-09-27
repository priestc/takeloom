# Inspiration Server: Autocomplete API

This document specs out a read-only endpoint for the **inspiration
server** (referred to in code as "radioserver") — the separate service that
hosts a studio's music library and that Takeloom queries for backing
tracks. It is written for whoever implements it in the inspiration
server's own codebase; it assumes no prior familiarity with Takeloom.

> **Titles endpoint still not needed**: the Add to Setlist dialog's
> "Inspiration" tab lets a musician add one exact inspiration-server
> track directly by artist/title, in addition to "inspiration filter"
> setlist slots (which draw a random matching track fresh each session,
> see `takeloom/inspiration.py`'s `search_tracks_by_filter`). Direct add
> resolves the exact track via the existing `/library/api/tracks/`
> filter-search endpoint plus an exact-match check on the client side
> (`search_inspiration_tracks`/`select_best_match` in
> `takeloom/inspiration.py`), not via a dedicated Titles autocomplete —
> so the Titles endpoint this doc originally specced still has no client
> and can be skipped/removed if not already built. Only the Artists
> endpoint below is live, backing both the Inspiration tab's Artist
> field and an inspiration filter's Artist field.

## Background: what's calling this, and why

Takeloom is a desktop app for recording multi-instrument backing-track
sessions. The "Add to Setlist" dialog's "Inspiration Filter" tab lets a
musician set up a standing filter (artist/genre/year range/length range)
that draws a random matching track from the inspiration server's library
each session, rather than adding one fixed song. Its Artist field is
wired up to autocomplete-as-you-type — a dropdown that populates with
suggestions pulled from the inspiration server's library as the user
types. Right now the suggestion source is a stub that always returns an
empty list, because the inspiration server has no endpoint to ask "what
artists do you have matching this partial text" yet. That's what this
document specs out. **No Takeloom-side changes are needed once this
endpoint exists** — the client code that will call it is already written
and waiting; see "Client-side integration point" at the end of this
document for exactly where.

## Existing API contract (for reference/consistency)

The inspiration server already exposes these two endpoints, which Takeloom
calls today. Match their conventions (auth, response shape, error
handling) in the new endpoints so the client's existing HTTP error
handling keeps working unmodified.

**Base URL**: whatever the studio configured as `inspiration_server`
(e.g. `http://myserver:8000`) — all paths below are relative to that.

**Auth**: every request carries `Authorization: Bearer <api_key>`, a
per-studio static API key configured on the Takeloom side. Return `401`
for a missing/invalid key.

### `POST /library/api/tracks/`

Request body:
```json
{"filters": [{"genre": "Rock"}, {"artist": "Miles Davis"}]}
```
Each object in `filters` is a set of AND'd field constraints; the objects
themselves are OR'd together. Response:
```json
{"tracks": [
  {"id": 4821, "artist": "Miles Davis", "title": "So What", "year": 1959, "format": "flac", "duration": 545.2}
]}
```

### `GET /library/api/tracks/<id>/download/`

Returns the raw audio file bytes for that track.

---

## Endpoint

Only the Artists endpoint is still needed — see the "Superseded" note
above regarding the Titles endpoint this section originally specced
alongside it.

### `GET /library/api/autocomplete/artists/`

Suggests artist names matching partial text typed into the dialog's
Artist field.

**Query params**

| Param   | Required | Description |
|---------|----------|-------------|
| `q`     | yes      | Partial, case-insensitive text the user has typed so far. |
| `limit` | no       | Max results to return. Default `10`, hard cap `25` (reject/clamp anything higher — this runs on every keystroke). |

**Response** — `200 OK`:
```json
{"suggestions": ["Miles Davis", "Miles Davis Quintet"]}
```
A flat list of distinct artist name strings, no other metadata needed —
the client just drops these into the field, it doesn't need IDs at this
stage — matching is resolved later, when the filter slot actually draws
a track via the existing `/library/api/tracks/` call.

**Empty/short query**: if `q` is missing or empty, return `{"suggestions": []}`
rather than every artist in the library. (The client won't currently send
this case — it only calls out once there's at least one typed character —
but the endpoint should degrade gracefully rather than 500 or dump the
whole table.)

**Errors**: `400` if `q` is present but not a string, or `limit` isn't a
positive integer. `401` per the existing auth convention above.

**Example**:
```
GET /library/api/autocomplete/artists/?q=mile&limit=10
Authorization: Bearer <api_key>

200 OK
{"suggestions": ["Miles Davis", "Miles Davis Quintet", "Milestones"]}
```

---

## Matching & ranking behavior

- Match **substring**, not just prefix — a musician typing "davis" for
  "Miles Davis" should still get a hit. Case-insensitive throughout.
- **Rank prefix matches above substring-only matches** (e.g. querying
  "mile" should put "Miles Davis" ahead of "Two Miles From Nowhere"),
  then alphabetically within each group. This is the single biggest
  factor in whether autocomplete feels useful vs. annoying.
- **Deduplicate** — if the same artist string appears on many tracks (the
  overwhelmingly common case), it should appear once in the suggestion
  list, not once per track. `SELECT DISTINCT` (or your ORM's equivalent)
  on the artist column, not a naive per-track scan.
- Minimum query length: consider requiring at least 2 characters before
  returning non-empty results, to avoid a single keystroke matching
  thousands of rows. This is a judgment call against your library size —
  skip it if the dataset is small enough that it doesn't matter.

## Performance

This endpoint is called on **every keystroke** from the client (there's
no server-side debouncing — that's a client-side concern and is out of
scope for this doc, but don't assume requests are rate-limited on
arrival). Each request needs to be cheap:

- Add a database index on whatever column backs the artist
  lookup if one doesn't already exist — a `LIKE '%q%'`/`ILIKE` scan over
  an unindexed text column on a large library will get slow fast. If
  you're on Postgres and want substring (not just prefix) matches to stay
  index-friendly, look at `pg_trgm` trigram indexes; a plain B-tree index
  only accelerates prefix matches.
- Cap `limit` server-side (see table above) regardless of what the client
  requests — don't trust client input for result size.

## Error handling

Match the existing `/library/api/tracks/` endpoint's conventions:
- `401` for missing/invalid `Authorization` header.
- `400` for malformed query params (see per-endpoint notes above).
- `200` with `{"suggestions": []}` for "no matches" — this is a normal,
  expected result, not an error. (Contrast with `/library/api/tracks/`,
  which treats zero matches as an error condition on the Takeloom side —
  autocomplete should NOT follow that pattern, since "no suggestions yet"
  is the common case while typing, not a failure.)

## Testing checklist

- [ ] `GET .../autocomplete/artists/?q=<3+ char partial match>` returns
      expected, deduplicated, ranked results.
- [ ] Missing `q` returns `{"suggestions": []}`, not a 500.
- [ ] `limit` above the hard cap gets clamped, not rejected or ignored.
- [ ] Missing/bad `Authorization` header returns `401`, matching the
      existing endpoints' behavior.
- [ ] Query with no matches returns `200 {"suggestions": []}`.
- [ ] Reasonably fast (sub-100ms range, ideally) against production-sized
      library data — this is what determines whether the feature feels
      responsive while typing.

## Client-side integration point (context only — no action needed here)

This side is already done and live in the Takeloom repo:
`search_artist_suggestions` in `takeloom/inspiration.py` calls this
endpoint, feeding an inspiration filter slot's Artist autocomplete field
(`takeloom/ui/filter_fields.py`).
