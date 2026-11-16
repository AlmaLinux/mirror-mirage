"""Configuration models and loader.

Two YAML files compose the runtime configuration:

* ``config.yaml`` — operational settings (CDNs, watches, daemon tuning).
* ``secrets.yaml`` — credentials, keyed by CDN name. Must be mode ``0600``
  and owned by the daemon's user.

The public entrypoint is :func:`load`, which parses both files, validates
them, and returns a :class:`Config` whose ``cdns`` mapping is annotated
with credentials.

Validation is strict: the loader refuses to return a ``Config`` if any
binding references an unknown CDN, any CDN lacks credentials, any watch
path does not exist on disk, or the secrets file has overly permissive
mode.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class EdgeAliasRule(BaseModel):
    """One rule selecting alias URLs to rewrite at the edge.

    ``match`` is a glob over the URL path without the leading slash
    (``almalinux/*/isos/*/*.iso``), with the same syntax as ``ignore``
    globs: ``*`` matches within one path component and ``**`` matches any
    number of components. ``name`` labels the rule in logs and CLI output.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    match: str = Field(min_length=1)

    @field_validator("match")
    @classmethod
    def _no_leading_slash(cls, value: str) -> str:
        if value.startswith("/"):
            raise ValueError("match is a URL path glob without the leading slash")
        return value


class EdgeAliasesConfig(BaseModel):
    """Publish symlink alias → real path mappings to a Fastly edge dictionary.

    Every URL path that matches a rule and reaches a real file through at
    least one symlink becomes one dictionary item, alias path → real path.
    A VCL snippet on the service rewrites ``req.url`` with it, so all the
    alias URLs of a file share the real path's cache entry. See
    ``docs/cdn-providers.md`` for the Fastly-side setup.

    * ``dictionary`` — name of the edge dictionary on the active service
      version. Mirror Mirage owns its contents: items it did not write
      are deleted.
    * ``max_items`` — refuse to sync a mapping larger than this (Fastly's
      default dictionary limit is 1000 items).
    * ``settle_seconds`` — after a relevant change, wait until changes
      have stopped for this long (at most 60 s) before syncing.
    * ``resync_interval_seconds`` — full resync period, which also catches
      anything a missed event would have changed.
    """

    model_config = ConfigDict(extra="forbid")

    dictionary: str = Field(min_length=1)
    rules: list[EdgeAliasRule] = Field(min_length=1)
    max_items: int = Field(default=1000, ge=1)
    settle_seconds: float = Field(default=5.0, ge=0)
    resync_interval_seconds: float = Field(default=600.0, gt=0)

    @model_validator(mode="after")
    def _unique_rule_names(self) -> EdgeAliasesConfig:
        names = [r.name for r in self.rules]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate edge alias rule names: {dupes}")
        return self


class FastlyCdn(BaseModel):
    """Fastly CDN configuration block.

    When ``purge_all_service_domains`` is True, the daemon calls Fastly's
    API at first use to discover every domain attached to ``service_id``
    and expands each URL purge across all of them — so a single
    filesystem event flushes the cache entry on every host the Fastly
    service is serving, without needing one binding per host in this
    file. The API token must include ``global:read`` (or wider) scope in
    addition to ``purge_select`` for discovery to succeed; most
    automation tokens already have this.

    ``edge_aliases`` turns on edge dictionary sync (see
    :class:`EdgeAliasesConfig`). Writing dictionary items needs a token
    with ``global`` scope; limit it to the services Mirror Mirage manages.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["fastly"]
    service_id: str
    purge_all_service_domains: bool = False
    edge_aliases: EdgeAliasesConfig | None = None


class CloudFrontCdn(BaseModel):
    """AWS CloudFront CDN configuration block."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["cloudfront"]
    distribution_id: str
    region: str = "us-east-1"


class GcpCdnConfig(BaseModel):
    """GCP Cloud CDN configuration block.

    ``quota_per_minute`` enforces a client-side cap on invalidation
    requests so that Mirage does not exceed the project's per-minute
    invalidation quota. The default of 450 leaves headroom under GCP's
    default quota of 500 paths/minute, because the client-side window
    and GCP's own count never line up exactly; raise this if your
    project has an elevated quota. Mirage blocks (does not drop) excess
    work, so a burst above quota is paced out across subsequent minutes.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["gcp_cdn"]
    project: str
    url_map: str
    quota_per_minute: int = 450


CdnConfig = Annotated[
    FastlyCdn | CloudFrontCdn | GcpCdnConfig,
    Field(discriminator="type"),
]


class Binding(BaseModel):
    """Single CDN binding inside a watch.

    ``purge_aliases`` (default ``True``) also purges every other URL that
    reaches a changed file through symlinks — ``/almalinux/10/...`` when
    ``10 -> 10.3`` and the file lives under ``10.3/``, or the
    ``-latest-`` name of a file a link points at. Turn it off for a CDN
    where the extra invalidation paths cost too much (CloudFront bills
    per path); that CDN then only gets the real path's URL.
    """

    model_config = ConfigDict(extra="forbid")

    cdn: str
    url_prefix: str
    purge_aliases: bool = True


class WatchConfig(BaseModel):
    """One filesystem root to watch and its CDN bindings.

    ``flush_on_delete`` controls whether file removals (``IN_DELETE`` /
    ``IN_MOVED_FROM``) trigger CDN purges. The default is ``False``:
    deletions are silently ignored. Set to ``True`` if you want the CDN
    cache for the removed URL to be invalidated too — note that
    CloudFront charges per invalidation path, so leaving this off can
    materially reduce cost on mirrors with high churn.
    """

    model_config = ConfigDict(extra="forbid")

    path: Path
    ignore: list[str] = []
    flush_on_delete: bool = False
    bindings: list[Binding]


class RetryConfig(BaseModel):
    """Retry policy for failed CDN purges."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = 8
    initial_backoff_seconds: float = 5.0
    max_backoff_seconds: float = 300.0


class DaemonConfig(BaseModel):
    """Daemon-wide tuning.

    ``max_aliases_per_event`` caps how many symlink alias paths one event
    can expand to. Real trees need a handful; the cap only matters for a
    symlink loop such as ``a -> .``, which would otherwise expand forever.
    """

    model_config = ConfigDict(extra="forbid")

    batch_quiet_window_seconds: float = 10.0
    batch_max_size: int = 500
    batch_max_age_seconds: float = 60.0
    max_aliases_per_event: int = Field(default=32, ge=0)
    queue_db: Path = Path("/var/lib/mirror-mirage/queue.db")
    retry: RetryConfig = RetryConfig()


class Config(BaseModel):
    """The fully-validated runtime configuration.

    The ``credentials`` mapping is populated by :func:`load`; tests may
    construct a ``Config`` directly with credentials inline.
    """

    model_config = ConfigDict(extra="forbid")

    cdns: dict[str, CdnConfig]
    watches: list[WatchConfig]
    daemon: DaemonConfig = DaemonConfig()
    credentials: dict[str, dict[str, str]] = Field(default_factory=dict)


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or validated."""


def load(config_path: Path, secrets_path: Path) -> Config:
    """Parse ``config_path`` and ``secrets_path``, validate, and return a Config.

    Raises :class:`ConfigError` on any of:
    * YAML parse failure
    * Schema validation failure (unknown fields, bad types)
    * ``secrets_path`` mode wider than ``0600``
    * A binding referencing a CDN not defined under ``cdns:``
    * A CDN with no entry in ``secrets.yaml``
    * A ``watches[].path`` that does not exist on disk
    * A Fastly CDN with ``edge_aliases`` that no watch binds
    """
    # Mode check first — refuse to even read a world-readable secrets file.
    try:
        st = secrets_path.stat()
    except OSError as e:
        raise ConfigError(f"cannot stat secrets file {secrets_path}: {e}") from e
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"secrets file {secrets_path} has insecure permission {mode:#o}; "
            f"expected mode 0600 (group/world bits must be clear)"
        )

    try:
        config_text = config_path.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read config file {config_path}: {e}") from e
    try:
        config_data = yaml.safe_load(config_text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse {config_path}: {e}") from e

    try:
        secrets_text = secrets_path.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read secrets file {secrets_path}: {e}") from e
    try:
        secrets_data: Any = yaml.safe_load(secrets_text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse {secrets_path}: {e}") from e
    if not isinstance(secrets_data, dict):
        raise ConfigError(f"secrets file {secrets_path} must be a mapping")

    try:
        cfg = Config.model_validate(config_data)
    except ValidationError as e:
        raise ConfigError(f"invalid configuration: {e}") from e

    cdn_names = set(cfg.cdns)
    for watch in cfg.watches:
        if not watch.path.exists():
            raise ConfigError(f"watch path {watch.path} does not exist")
        for binding in watch.bindings:
            if binding.cdn not in cdn_names:
                raise ConfigError(
                    f"watch {watch.path}: binding references unknown cdn "
                    f"{binding.cdn!r}; defined cdns: {sorted(cdn_names)}"
                )

    bound_cdns = {b.cdn for w in cfg.watches for b in w.bindings}
    for cdn_name, cdn in cfg.cdns.items():
        if isinstance(cdn, FastlyCdn) and cdn.edge_aliases and cdn_name not in bound_cdns:
            raise ConfigError(
                f"cdn {cdn_name!r} has edge_aliases but no watch binds it, "
                f"so there are no paths to publish"
            )

    for cdn_name in cfg.cdns:
        creds = secrets_data.get(cdn_name)
        if not creds or not isinstance(creds, dict):
            raise ConfigError(f"no credentials for cdn {cdn_name!r} in {secrets_path}")
        cfg.credentials[cdn_name] = {str(k): str(v) for k, v in creds.items()}

    return cfg
