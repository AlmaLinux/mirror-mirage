# CDN provider details

Per-CDN notes on the API used, authentication, batch limits, and how Mirror Mirage
classifies errors as retryable or terminal.

All providers implement the same protocol:

```python
class CdnProvider(Protocol):
    async def purge(self, urls: list[str]) -> PurgeResult: ...

@dataclass(frozen=True)
class PurgeResult:
    ok: bool
    retryable: bool
    message: str
    per_url: Mapping[str, PurgeResult] | None = None
```

`urls` is the list of public URLs Mirror Mirage has decided need flushing. Each
provider is responsible for translating that list into the appropriate
API calls.

**Per-URL retry.** Fastly and GCP make one API call per URL, so they report
each URL's outcome in `per_url` (built with `PurgeResult.from_per_url`).
When an attempt partly fails, the queue worker:

- drops the URLs that succeeded — they are never purged again;
- logs the URLs that failed with a terminal error (`event=purge_failed`)
  and drops them from the job;
- retries only the URLs that failed with a retryable error.

CloudFront sends the whole batch in one invalidation, so its result
(`per_url` unset) applies to every URL and the whole batch is retried.

**Wildcard URLs (directory symlinks).** When a symlink to a directory is
created or retargeted, e.g. `9 -> 9.8` becomes `9 -> 9.9`, everything under
the link's URL changes. The mapper then queues two URLs per binding: the
link's own URL (`.../almalinux/9`) and a wildcard (`.../almalinux/9/*`). The
link's target (`9.9`) is not purged, because its own URLs did not change.
Providers handle the wildcard according to what the CDN supports:

| CDN        | Wildcard `.../9/*`                                        |
|------------|-----------------------------------------------------------|
| CloudFront | Sent as the invalidation path `/almalinux/9/*`.           |
| GCP        | Sent as the invalidation path `/almalinux/9/*`.           |
| Fastly     | Skipped and logged (`event=fastly_wildcard_skipped`).     |

## Fastly

- **Type key:** `fastly`
- **Auth:** API token in `Fastly-Key` header. Token stored in
  `secrets.yaml` under `api_token`. The token is scoped to one Fastly
  service. Required token scopes:
  - `purge_select` for URL purges (always needed).
  - `global:read` (or wider) *additionally* when
    `purge_all_service_domains` is enabled — needed so the daemon can
    call `GET /service/{id}` and list the active version's domains.
  - `global` when `edge_aliases` is configured, to write dictionary items
    (see [Fastly edge aliases](#fastly-edge-aliases)).
- **Config required:** `service_id` — recorded for operator clarity
  (which CDN binding maps to which Fastly service); the URL-purge API
  itself does not require it in the request.
- **Config optional:**
  - `concurrency` (default `10`) — maximum in-flight purge requests at
    any moment. Tune up for very large batches if your Fastly account
    has elevated API quota.
  - `purge_all_service_domains` (default `false`) — when `true`, the
    daemon fetches the list of domains attached to ``service_id`` on
    first use and expands every purge across all of them, so one
    filesystem event flushes the cache entry on every hostname the
    service is serving. Useful when a single Fastly service fronts
    multiple mirror hostnames (e.g. `repo.example.org` *and*
    `mirror.example.org`) — saves having to list each one as a
    separate binding. Discovery is cached for the daemon's lifetime;
    restart to pick up newly-added domains. Retries are tracked per
    original URL: if any domain's purge hits a retryable error, that URL
    is retried on every domain.
- **API used:** Single-URL purge — `POST https://api.fastly.com/purge/{host}{path}[?{query}]`.
  Fastly does not expose a bulk URL purge endpoint; we issue one HTTP
  request per URL with bounded concurrency. (Fastly does support bulk
  purges via *surrogate keys*, but that requires the origin to emit
  `Surrogate-Key` headers — a different config model than the daemon's
  per-filesystem-path event flow.)
- **Wildcards:** not supported by URL purge. Wildcard URLs from a
  retargeted directory symlink are skipped (no API call), logged as
  `event=fastly_wildcard_skipped`, and treated as done. The link's own URL
  is still purged. Content under the link stays cached on Fastly until its
  TTL expires.
- **Idempotency:** safe. Re-purging an already-purged URL is a no-op.
- **Retryable status codes:** `408`, `425`, `429`, `500`, `502`, `503`,
  `504`, plus any transport-level error (connection reset, timeout).
- **Terminal status codes:** `400` (malformed URL), `401` (bad token —
  fix credentials), `403`, `404` (URL not part of this service).
- **Upstream docs:** <https://www.fastly.com/documentation/reference/api/purging/>

## Fastly edge aliases

Optional, per Fastly CDN. A file reachable through symlinks has several
URLs (`/almalinux/10/...` when `10 -> 10.3`, `-latest-` names), and Fastly
caches each one separately. With `edge_aliases`, Mirror Mirage keeps an
edge dictionary of **alias URL path → real URL path** for the URL paths
your rules select, and a VCL snippet rewrites `req.url` with it. All the
aliases of a file then share the real path's cache entry:

- one cached copy of each file, not one per alias;
- every cached object is a real file, so the normal purge of the real path
  keeps it fresh (a re-spin in place needs nothing extra);
- retargeting a `-latest-` link or a version link is a dictionary update
  (seconds), not a purge. Nothing is fetched cold that did not change.

```yaml
cdns:
  fastly_main:
    type: fastly
    service_id: SU1Z0isxPaozGVKXdv0eY
    edge_aliases:
      dictionary: mirage_aliases
      rules:
        - name: isos
          match: "almalinux/*/isos/*/*.iso"
        - name: cloud-images
          match: "almalinux/*/cloud/*/images/*"
      # max_items: 1000               # refuse to sync more than this
      # settle_seconds: 5             # quiet time after a change before syncing
      # resync_interval_seconds: 600  # full resync period
```

`match` is a glob over the URL path without the leading slash, with the
same syntax as `ignore` globs. Rules only say *which* URL paths to look
at; where each one points comes from the symlinks on disk. A path becomes
an item when it reaches a regular file through at least one symlink and
that file is served through a binding of the same CDN. Prefer anchored
patterns: a leading `**` walks the whole tree on every sync.

### Fastly setup (once)

1. Create the edge dictionary (`mirage_aliases` above) on a new service
   version. It must not be write-only: Mirror Mirage reads it to compute
   the diff.
2. Add a VCL snippet on the same version:

```vcl
# Type: recv. Give it a lower priority number than any other recv snippet
# that reads req.url, so the rewrite happens first.
if (table.contains(mirage_aliases, req.url.path)) {
  set req.http.Mirage-Alias = req.url.path;
  if (req.url.qs == "") {
    set req.url = table.lookup(mirage_aliases, req.url.path);
  } else {
    set req.url = table.lookup(mirage_aliases, req.url.path) "?" req.url.qs;
  }
}
```

   The rewrite applies to every domain on the service. If the service
   also serves hosts whose paths mean something else, wrap the snippet
   in a `req.http.host` check. It is idempotent (a real path is never a
   key), so the shield hop, restarts and segmented-caching inner
   requests are unaffected. `Mirage-Alias` keeps the requested path for
   logging.
3. Activate the version. The dictionary is empty, so nothing changes yet.
4. Give the CDN's `api_token` `global` scope. Writing dictionary items
   needs it (Fastly has no narrower scope that can), so restrict the
   token to this one service. Don't share it with CDNs on other services.

### Rollout

1. Deploy the config, then preview without writing anything:
   `mirror-mirage --config ... --edge-aliases show` (the items the rules
   produce) and `--edge-aliases diff` (against the live dictionary).
2. Restart the daemon. It syncs at startup, logs
   `event=edge_aliases_synced ... upserts=N`, and the rewrites start.

### Sync behavior

- **Desired state, not a log.** Each sync recomputes the whole mapping from
  the filesystem (glob expansion following symlinks, then `realpath`),
  reads the live dictionary, and applies the difference in batches of up
  to 1000 operations. A missed event is corrected at the next sync.
- **Triggers:** startup; any symlink change; any purge whose URL matches a
  rule (a new image appearing under `10.3/` adds a `/10/` item), debounced
  by `settle_seconds` (at most 60 s); and every `resync_interval_seconds`.
- **Ownership:** the dictionary belongs to Mirror Mirage. Items it did not
  write are deleted at the next sync.
- **Safety:** an item is only published when its target exists, is a
  regular file, and is under a watch bound to the same CDN. A missing or
  broken tree removes items, which falls back to un-rewritten URLs
  (today's behavior) rather than rewriting to the wrong file. A mapping
  larger than `max_items` is not written at all.
- **Skipped paths** are counted in the sync log line
  (`skipped_<reason>=N`): `outside_watches` (the link resolves outside
  every watch, e.g. an absolute target through a bind mount's device
  path), `target_not_on_cdn`, `target_ignored`, `not_a_file`,
  `needs_url_encoding`, `too_long` (keys are capped at 255 characters,
  values at 8000), `conflict`.
- **Failures** (`event=edge_aliases_sync_failed`) are retried with
  backoff from 5 s to 5 min. They never affect purging.
- **Dry run:** with `--dry-run` the sync computes the mapping and logs
  `event=edge_aliases_dry_run items=N` without calling Fastly.
- **API used:** `GET /service/{id}` (active version),
  `GET /service/{id}/version/{v}/dictionary/{name}` (dictionary ID),
  `GET` and `PATCH /service/{id}/dictionary/{dict_id}/items`.

Purging is unchanged: alias URLs are still purged when `purge_aliases` is
on.

## AWS CloudFront

- **Type key:** `cloudfront`
- **Auth:** AWS access key + secret in `secrets.yaml` (`access_key_id`,
  `secret_access_key`). Alternative: omit credentials in `secrets.yaml` and
  rely on the daemon's IAM instance role / environment.
- **Config required:** `distribution_id`. `region` defaults to
  `us-east-1`; CloudFront control plane is global but boto3 still needs a
  region.
- **API used:** `cloudfront.create_invalidation(DistributionId, InvalidationBatch={...})`,
  called via `asyncio.to_thread` because boto3 is synchronous.
- **Batch limit:** CloudFront accepts up to 3,000 paths per invalidation;
  Mirror Mirage caps batches at `batch_max_size` (default 500) to keep latency
  predictable and to stay well under the limit. Each path counts toward
  the per-month free tier (1,000 paths free, then $0.005/path).
- **Path format:** CloudFront wants the *path* portion of the URL, not the
  full URL. The provider strips the scheme and host before submitting.
  E.g. `https://cdn.example.com/foo/bar.rpm` → `/foo/bar.rpm`.
- **Alias paths:** with `purge_aliases` on (the default), a change to a
  file also invalidates the other URLs that reach it through symlinks, so
  files under a version link like `9 -> 9.9` cost two paths each. Set
  `purge_aliases: false` on the CloudFront binding to opt out.
- **Wildcards:** `.../9/*` is sent as the path `/9/*`, which CloudFront
  invalidates as a prefix. A wildcard is billed as one path, but counts
  against a separate cap of 15 wildcard paths in progress per
  distribution; hitting it is handled like the 3,000-path cap below.
- **Idempotency:** safe. Multiple invalidations on the same path are
  billed but functionally fine.
- **Retryable errors:** `Throttling`, `ThrottlingException`,
  `RequestLimitExceeded`, `ServiceUnavailable`, 5xx, transport errors.
- **Terminal errors:** `AccessDenied` (revoked credentials),
  `NoSuchDistribution`, `InvalidArgument`, `MalformedXML`.
- **Active-invalidation limit:** AWS allows up to 3,000 individual file
  paths in progress per distribution at once (wildcard paths have a
  separate cap of 15). `create_invalidation` returns as soon as the
  invalidation is accepted, not when it finishes, so a large sync
  reaches this limit quickly: with 500-path batches, six invalidations
  in progress use it all up. CloudFront then rejects new batches with
  `TooManyInvalidationsInProgress`. The provider treats that as
  backpressure, not a failed attempt: it holds the batch and resubmits
  the same invalidation every 30 seconds until CloudFront accepts it.
  Each CDN has one queue worker, so this pauses the CloudFront queue
  without using up any job's retry attempts. It logs
  `event=cloudfront_in_progress_limit` when the wait starts and
  `event=cloudfront_in_progress_cleared` when the batch is accepted.
  After 30 minutes of waiting it gives up and returns a retryable
  failure, and the normal retry backoff takes over.
- **Upstream docs:** <https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/Invalidation.html>,
  <https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/InvalidationLimits.html>

## GCP Cloud CDN

- **Type key:** `gcp_cdn`
- **Auth:** Service account JSON file. Path stored in `secrets.yaml` under
  `service_account_file`. Token minted at startup via `google-auth`,
  refreshed automatically before expiry.
- **Config required:** `project` (GCP project ID), `url_map` (the load
  balancer URL map that fronts the CDN).
- **Config optional:** `quota_per_minute` (default `450`) — client-side
  cap on invalidation requests per rolling minute. Mirror Mirage will block
  excess requests until the window has room, instead of letting GCP
  reject them with `429`. Each call takes its slot just before it is
  sent, but the default still sits below GCP's 500/minute quota because
  the client-side window and GCP's own count never line up exactly.
  Raise this only if your project has an elevated quota, and keep
  similar headroom.
- **API used:** `compute.urlMaps.invalidateCache` —
  `POST https://compute.googleapis.com/compute/v1/projects/{project}/global/urlMaps/{url_map}/invalidateCache`
  with body `{"path": "/foo/bar", "host": "cdn.example.com"}`.
- **Batch limit:** **One path per call.** Mirror Mirage dispatches multiple
  invalidations concurrently with a bounded semaphore (default 10).
  A batch of N URLs becomes N API calls.
- **Path format:** path component of the URL; `host` is the URL's
  hostname, sent as a separate field.
- **Wildcards:** `.../9/*` is sent as the path `/9/*`. GCP treats a path
  ending in `*` as a prefix, so this invalidates everything under `/9/`
  (and, because of the slash, not siblings such as `/9.9/`). It is one
  API call and counts once against `quota_per_minute`.
- **Idempotency:** safe. Cache invalidations are absorbed if the path is
  already invalidated.
- **Quota:** GCP enforces invalidation quotas per project; default is
  500 paths/minute (overridable in the Cloud Console). Mirror Mirage paces
  itself client-side via ``quota_per_minute`` (default 450) to stay
  under this cap.
  As a backstop, rate-limit responses (`429`, or a `403` carrying a
  rate-limit reason) are still treated as retryable, but in normal
  operation the daemon should rarely trip them.
- **Retryable errors:** `429`, `500`, `503`, transport errors, and `403`
  when the error body shows rate limiting: status `RESOURCE_EXHAUSTED`,
  or reason `rateLimitExceeded`, `userRateLimitExceeded` or
  `RATE_LIMIT_EXCEEDED` (in `error.errors[]` or an `ErrorInfo` detail).
- **Terminal errors:** `401`/`UNAUTHENTICATED` (rotate the service
  account), any other `403` (e.g. `PERMISSION_DENIED`), `404` (unknown
  URL map), `400`.
- **Upstream docs:** <https://cloud.google.com/cdn/docs/invalidating-cached-content>

## Adding a new provider

1. Create `src/mirror_mirage/providers/<name>.py`.
2. Define a class implementing `CdnProvider`. Constructor takes the
   relevant config + credentials. Async `purge(urls)` returns a
   `PurgeResult`.
3. Register the type key in `providers/__init__.py:build_providers`.
4. Document it in this file: API, auth, batch limits, retryable vs
   terminal error classification.
5. Add a `tests/test_providers.py` test class exercising request shape and
   error classification (typically with `respx` for HTTP providers).
