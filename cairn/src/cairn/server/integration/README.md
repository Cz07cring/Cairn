# Ring product mode (B1)

`CAIRN_PRODUCT_MODE=ring` enables the Ring product entry. The default is
`standalone`, which retains Cairn's original local protocol. Any other mode
value fails startup. A database with Ring bindings cannot start in standalone
mode.

Ring mode requires:

- `CAIRN_RING_BASE_URL`: internal Ring Control API origin, for server-side reads.
- `CAIRN_PUBLIC_ORIGIN`: browser-facing Cairn origin, for strict Origin checks.
- Both origins must use HTTPS, or HTTP on loopback for local development. The
  server forwards the user's Ring session cookie to `CAIRN_RING_BASE_URL`.
- A reverse proxy that serves Cairn at `/` and Ring OIDC endpoints at
  `/api/v1/auth/*` on that same origin. Ring must allow the Cairn origin as an
  OIDC return origin and set the `ring_session` cookie for `/`.

The browser obtains a Ring OIDC session through Ring's login endpoint. Cairn
forwards only that user's `ring_session` cookie to Ring
`GET /api/v1/auth/session`, `GET /api/v1/goals/{id}`,
`GET /api/v1/goals/{id}/snapshot`, and, for `DONE`,
`GET /api/v1/goals/{id}/release`. Cairn never stores or sends a Ring service key to
the browser. All Cairn writes in Ring mode require the Ring session's CSRF
token and an exact `Origin` match.

`POST /projects/{id}/ring-binding` accepts existing Ring project and Goal UUIDs.
It checks the Ring user project scope, Goal and snapshot versions, then writes
one SQLite binding. It does not create or start a Ring Goal. An identical
binding request is idempotent; a different or duplicate Goal binding conflicts.
`GET /projects/{id}/ring-status` reads Ring on demand. A `DONE` label requires
the matching, valid ReleaseManifest. Unknown version or read failure yields
`UNKNOWN` or a fail-closed HTTP error.

Every business request first validates the current Ring `/auth/session` response.
If identity is unavailable or its contract is unknown, reads fail with 503.
Project lists and local graph reads then use the Cairn ACL and the session's
current `project_ids`; they remain available while Ring Goal or snapshot reads
are temporarily unavailable. A revoked ACL or Ring project scope hides the
project. The Ring status endpoint reports `UNKNOWN` when Goal or snapshot
versions cannot be read, and does not turn that state into `DONE`. Binding
creation and bound-project writes still require fresh Goal and snapshot checks.

The project creator owns the Cairn ACL. The owner can add or remove a viewer
with `PUT` and `DELETE /projects/{id}/members/{user_id}`; `GET` lists members.
Every bound project request checks the current user's Ring project scope and
the Cairn ACL. Writes also check fresh Goal visibility and version. Existing
standalone projects have no ACL owner and remain inaccessible in Ring mode
until an explicit migration is designed.

Binding requires an active, idle Cairn project with no unfinished Intent,
including an unclaimed Intent. Before binding, operators must also confirm
that its dispatcher has stopped and no Cairn CLI container or process is still
running. An expired SQLite worker lease alone does not prove a host process has
stopped; B1 does not provide an atomic cross-system process fence.

Ring-bound projects reject Cairn reason/worker claims, Intent worker writes,
`complete`, `reopen`, status changes and deletion. The dispatcher also skips
Ring-bound projects. B1 does not publish Intent candidates, run Ring tasks, or
reconcile Ring effects; those remain later batches of the integration plan.
