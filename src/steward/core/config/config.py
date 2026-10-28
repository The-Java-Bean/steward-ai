# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Load and validate the YAML files in the config directory.

Every file is optional (a missing file means defaults), must contain a mapping with
`config_version`, and rejects unknown keys. Settings the owner edits in the UI will
live in `state.db`; these files hold what's needed to start.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from steward.core.folders.policy import Role
from steward.core.folders.types import TypeDefinition
from steward.sdk import Lane

log = logging.getLogger(__name__)

CONFIG_DIR_ENV = "STEWARD_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("/config")
CURRENT_VERSION = 1


class ConfigError(Exception):
    """A config file is missing required values, has unknown keys, or doesn't parse."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class _ConfigFile(_Strict):
    config_version: Literal[1]


# --- steward.yaml ---------------------------------------------------------------


class LoginLimits(_Strict):
    """Temporary lockouts after failed password checks (logins and re-auth)."""

    window_minutes: int = Field(15, ge=1)
    """Failures count when they are this recent and newer than the last success."""
    max_failures_per_ip: int = Field(10, ge=1)
    """Failures from one client address (any username) before it is locked out."""
    max_failures_per_account: int = Field(50, ge=1)
    """Failures for one username (from any address) before it is locked out. Higher than
    the per-address limit so one attacker can't easily lock the owner out."""
    lockout_minutes: int = Field(15, ge=1)
    retention_days: int = Field(30, ge=1)
    """How long login attempts are kept (they're shown on the health page)."""


class AuthConfig(_Strict):
    session_idle_hours: float = Field(12, gt=0)
    """A session ends after this long without a request."""
    session_max_days: float = Field(7, gt=0)
    """A session ends this long after login, however active it is."""
    login: LoginLimits = LoginLimits()


class ApiConfig(_Strict):
    """Where `steward api` listens."""

    host: str = "0.0.0.0"  # noqa: S104 -- inside a container; publish the port to choose exposure
    port: int = Field(8420, ge=1, le=65535)
    secure_cookies: Literal["auto", "always", "never"] = "auto"
    """`auto`: mark cookies `Secure` when the request came over HTTPS (directly, or from a
    trusted proxy that sent `X-Forwarded-Proto: https`)."""


class SetupConfig(_Strict):
    library_dirs: tuple[Path, ...] = (Path("/libraries"),)
    """The setup wizard suggests each folder directly inside these as a library (the
    example compose file mounts libraries under `/libraries`)."""


class BackupConfig(_Strict):
    """`steward backup` and the nightly `core.backup` schedule (turn the schedule off in
    `pipeline.yaml` when an external backup tool calls `steward backup` instead)."""

    keep: int = Field(7, ge=1, le=1000)
    """How many backups to keep in the backup folder; older ones are deleted."""
    dir: Path | None = None
    """Where backups go. Default: `<data_dir>/backups`."""
    max_age_before_migrate_hours: int = Field(24, ge=1)
    """Before migrating existing databases, a backup at most this old must exist; if
    there's none, `steward api` and `steward db upgrade` take one first."""


class StewardConfig(_ConfigFile):
    data_dir: Path = Path("/data")
    """Databases, caches and backups. Must be on a local disk (SQLite)."""
    timezone: str = "UTC"
    """IANA name (e.g. `Europe/Berlin`), used for daily schedules."""
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    """Reverse proxies (addresses or CIDR ranges) whose `X-Forwarded-For` is believed.
    Empty: the address that connected is the client."""
    auth: AuthConfig = AuthConfig()
    api: ApiConfig = ApiConfig()
    setup: SetupConfig = SetupConfig()
    backup: BackupConfig = BackupConfig()

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def backup_dir(self) -> Path:
        return self.backup.dir if self.backup.dir is not None else self.data_dir / "backups"


# --- modules.yaml ---------------------------------------------------------------


class ModuleEntry(_Strict):
    enabled: bool | None = None
    """Unset: built-in feature modules are on, everything else is off."""
    settings: dict[str, Any] = Field(default_factory=dict)
    """The module's own config section, validated by its `config_model`."""


class ModulesConfig(_ConfigFile):
    modules: dict[str, ModuleEntry] = Field(default_factory=dict)


# --- pipeline.yaml --------------------------------------------------------------


class LaneSettings(_Strict):
    concurrency: int = Field(ge=1, le=64)


DEFAULT_CONCURRENCY: dict[Lane, int] = {
    "io": 1,
    "cpu": 2,
    "llm-local": 1,
    "llm-remote": 2,
    "events": 1,
}


class JobsSettings(_Strict):
    lease_seconds: int = Field(300, ge=30)
    """How long a claimed job is reserved before another worker may take it over."""
    max_attempts: int = Field(5, ge=1)
    backoff_base_seconds: int = Field(30, ge=1)
    backoff_max_seconds: int = Field(3600, ge=1)
    keep_done_days: int = Field(7, ge=0)
    poll_seconds: float = Field(1.0, gt=0)


class EventsSettings(_Strict):
    retention_days: int = Field(90, ge=1)


DEFAULT_IGNORE_PATTERNS = (
    "*.part",
    "*.crdownload",
    "*.tmp",
    "~$*",
    ".~lock*",
    ".sync*",
    ".DS_Store",
    "Thumbs.db",
    ".obsidian/workspace*.json",
    ".steward-tmp-*",
)


class DiscoverySettings(_Strict):
    watch: bool = True
    """Watch libraries for changes (inotify). The periodic full scan runs regardless."""
    debounce_seconds: float = Field(10, ge=0)
    """Wait this long after the last change to a path before scanning it."""
    scan_on_start: bool = True
    """Scan every library when the worker starts."""
    ignore_patterns: tuple[str, ...] = DEFAULT_IGNORE_PATTERNS
    """Names (or `dir/name` tails) of temporary files never indexed, in every folder.
    Folder types add their own `ignore` lists."""
    move_window_hours: int = Field(72, ge=0)
    """A file that went missing this recently and reappears elsewhere keeps its identity."""
    idle_io_priority: bool = True
    """Run the `io` lane's threads at idle disk priority (Linux)."""
    max_read_mib_per_second: float = Field(0, ge=0)
    """Cap on hashing reads, for slow or shared disks. 0 means no cap."""


class PipelineConfig(_ConfigFile):
    lanes: dict[Lane, LaneSettings] = Field(default_factory=dict)
    """Concurrency per worker lane; unset lanes use the defaults."""
    jobs: JobsSettings = JobsSettings()
    events: EventsSettings = EventsSettings()
    discovery: DiscoverySettings = DiscoverySettings()
    schedules: dict[str, str] = Field(default_factory=dict)
    """Per-schedule timing override: `every 6h`, `daily 03:00`, or `off`."""

    @field_validator("lanes")
    @classmethod
    def _worker_lanes_only(cls, value: dict[Lane, LaneSettings]) -> dict[Lane, LaneSettings]:
        if "executor" in value:
            raise ValueError("the executor lane always has concurrency 1 (single writer)")
        return value

    def concurrency(self, lane: Lane) -> int:
        if lane == "executor":
            return 1
        configured = self.lanes.get(lane)
        return configured.concurrency if configured else DEFAULT_CONCURRENCY[lane]


# --- libraries.yaml and folders.yaml (seed data, imported into state.db) ----------


class LibraryEntry(_Strict):
    path: str
    """Absolute path of the library root as steward-ai sees it (inside the container)."""
    name: str | None = None


class LibrariesConfig(_ConfigFile):
    libraries: dict[str, LibraryEntry] = Field(default_factory=dict)


class MappingEntry(_Strict):
    type: str | None = None
    locked: bool | None = None
    permanent: bool = False


class FoldersConfig(_ConfigFile):
    """Custom types, overrides of built-in types, mappings and roles.

    `mappings` is `{library_id: {relative/path: type_name | {type, locked, permanent}}}`;
    `roles` is `{role: "library:relative/path"}`.
    """

    types: dict[str, TypeDefinition] = Field(default_factory=dict)
    mappings: dict[str, dict[str, str | MappingEntry]] = Field(default_factory=dict)
    roles: dict[Role, str] = Field(default_factory=dict)


# --- privacy.yaml ---------------------------------------------------------------

DEFAULT_SECRET_PATTERNS = (
    "*.kdbx",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.gpg",
    "*.asc",
    "id_rsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".env",
    "*.env",
    ".ssh/**",
    ".gnupg/**",
    "wallet.dat",
    "*.ovpn",
)


class PrivacyRule(_Strict):
    pattern: str = Field(min_length=1)
    """A name glob (`*.pdf`) or a path pattern with `/` (`taxes/**`, `**/medical/*.pdf`);
    see `steward.core.privacy.rules`."""
    sensitivity: Literal["local-only", "excluded"]
    """Rules can only raise sensitivity, so `normal` isn't allowed here."""


class PrivacyConfig(_ConfigFile):
    """Which files are more sensitive than their folder type says (plan §9.1)."""

    secret_patterns: tuple[str, ...] = DEFAULT_SECRET_PATTERNS
    """Files matching these are `excluded`: their content is never read, not even hashed.
    Setting this replaces the default list; use `extra_secret_patterns` to add to it."""
    extra_secret_patterns: tuple[str, ...] = ()
    rules: tuple[PrivacyRule, ...] = ()


# --- llm.yaml -------------------------------------------------------------------


class EndpointConfig(_Strict):
    base_url: str = Field(pattern=r"^https?://")
    """An OpenAI-compatible API, e.g. `http://<model-server>:11434/v1`."""
    trust: Literal["local", "remote"]
    """Required: `local` endpoints may receive `local-only` content; `remote` ones never."""
    api_key_env: str | None = None
    """Name of the environment variable holding the API key, if the endpoint needs one."""
    timeout_s: float = Field(600, gt=0)
    max_concurrency: int = Field(2, ge=1, le=64)


class RoleConfig(_Strict):
    endpoint: str
    model: str = Field(min_length=1)
    dims: int | None = Field(None, ge=1)
    """Embedding size (embedding roles)."""
    capabilities: tuple[Literal["vision"], ...] = ()
    structured: Literal["json_schema", "json_mode", "prompted"] = "prompted"
    tools: Literal["native", "prompted", "none"] = "none"
    context: int | None = Field(None, ge=512)
    local_fallback: str | None = None
    """A role on a `local` endpoint to use instead when the request may not go to this
    role's endpoint (chat surfaces offer this; background jobs wait instead)."""


class LlmConfig(_ConfigFile):
    """Model endpoints and the roles mapped to them (plan §11). Empty means no model:
    steward-ai runs without one, and the stages that need a role stay off."""

    endpoints: dict[str, EndpointConfig] = Field(default_factory=dict)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _references(self) -> LlmConfig:
        for name, role in self.roles.items():
            if role.endpoint not in self.endpoints:
                raise ValueError(f"role {name!r} uses unknown endpoint {role.endpoint!r}")
            fallback = role.local_fallback
            if fallback is None:
                continue
            target = self.roles.get(fallback)
            if target is None:
                raise ValueError(f"role {name!r}: local_fallback {fallback!r} isn't a role")
            if self.endpoints[target.endpoint].trust != "local":
                raise ValueError(f"role {name!r}: local_fallback {fallback!r} isn't local")
        return self


# --- loading --------------------------------------------------------------------

FILES: dict[str, type[_ConfigFile]] = {
    "steward.yaml": StewardConfig,
    "modules.yaml": ModulesConfig,
    "pipeline.yaml": PipelineConfig,
    "libraries.yaml": LibrariesConfig,
    "folders.yaml": FoldersConfig,
    "privacy.yaml": PrivacyConfig,
    "llm.yaml": LlmConfig,
}


@dataclass(frozen=True)
class Config:
    config_dir: Path
    steward: StewardConfig = field(default_factory=lambda: StewardConfig(config_version=1))
    modules: ModulesConfig = field(default_factory=lambda: ModulesConfig(config_version=1))
    pipeline: PipelineConfig = field(default_factory=lambda: PipelineConfig(config_version=1))
    libraries: LibrariesConfig = field(default_factory=lambda: LibrariesConfig(config_version=1))
    folders: FoldersConfig = field(default_factory=lambda: FoldersConfig(config_version=1))
    privacy: PrivacyConfig = field(default_factory=lambda: PrivacyConfig(config_version=1))
    llm: LlmConfig = field(default_factory=lambda: LlmConfig(config_version=1))

    @property
    def data_dir(self) -> Path:
        return self.steward.data_dir


def config_dir_from_env() -> Path:
    return Path(os.environ.get(CONFIG_DIR_ENV) or DEFAULT_CONFIG_DIR)


def load_config(config_dir: Path | None = None) -> Config:
    """Load every known file in `config_dir`; missing files fall back to defaults."""
    config_dir = config_dir if config_dir is not None else config_dir_from_env()
    if not config_dir.is_dir():
        raise ConfigError(
            f"config directory {config_dir} does not exist "
            f"(set {CONFIG_DIR_ENV} or pass --config-dir)"
        )

    for path in sorted(config_dir.glob("*.yaml")):
        if path.name not in FILES and not path.name.endswith(".example.yaml"):
            log.warning("ignoring unknown config file", extra={"fields": {"file": path.name}})

    loaded: dict[str, _ConfigFile] = {}
    for name, model in FILES.items():
        path = config_dir / name
        if path.exists():
            loaded[name] = _load_file(path, model)

    return Config(
        config_dir=config_dir,
        steward=_get(loaded, "steward.yaml", StewardConfig),
        modules=_get(loaded, "modules.yaml", ModulesConfig),
        pipeline=_get(loaded, "pipeline.yaml", PipelineConfig),
        libraries=_get(loaded, "libraries.yaml", LibrariesConfig),
        folders=_get(loaded, "folders.yaml", FoldersConfig),
        privacy=_get(loaded, "privacy.yaml", PrivacyConfig),
        llm=_get(loaded, "llm.yaml", LlmConfig),
    )


def _get[T: _ConfigFile](loaded: dict[str, _ConfigFile], name: str, model: type[T]) -> T:
    value = loaded.get(name)
    return value if isinstance(value, model) else model(config_version=1)


def load_folders_file(path: Path) -> FoldersConfig:
    """Load a file in `folders.yaml` format from anywhere (for `steward folders import`)."""
    loaded = _load_file(path, FoldersConfig)
    assert isinstance(loaded, FoldersConfig)  # noqa: S101 -- _load_file validates with this model
    return loaded


def _load_file(path: Path, model: type[_ConfigFile]) -> _ConfigFile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: not valid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: must contain a mapping of settings")
    version = raw.get("config_version")
    if version is None:
        raise ConfigError(f"{path.name}: add `config_version: {CURRENT_VERSION}` at the top")
    if version != CURRENT_VERSION:
        raise ConfigError(
            f"{path.name}: config_version {version!r} isn't supported "
            f"(this steward-ai reads version {CURRENT_VERSION})"
        )
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path.name} is invalid:\n{exc}") from exc
