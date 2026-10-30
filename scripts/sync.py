#!/usr/bin/env python3
"""Sincronización de assets. Python 3.10+, biblioteca estándar, sin backend."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from video_titles import normalize_title

ROOT = Path(__file__).resolve().parents[1]
VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?\Z")
DEFAULT_RULES = {"minDurationSeconds": 420, "maxDurationSeconds": None,
                 "music": False, "regionCodes": ["AR"], "maxAgeDays": None}
DEFAULT_LIMITS = {"videosPerDay": 50, "maxPagesPerSource": 100}
METADATA_MAX_AGE = timedelta(days=28)


class SyncError(Exception):
    """Sólo mensajes seguros: nunca URLs, claves ni respuestas crudas."""


class QuotaError(SyncError):
    pass


class IncompleteSource(SyncError):
    """Se alcanzó un límite sin confirmar la cobertura de la fuente."""


def changed_channels(config: dict, previous: dict) -> set[str]:
    """Git provee la configuración anterior; no se guarda estado operativo."""
    old = {channel["id"]: channel for channel in previous.get("channels", [])}
    # El presupuesto limita la ejecución, pero no cambia el contenido solicitado.
    ignored = {"channels", "quotaBudget"}
    global_change = {k: v for k, v in config.items() if k not in ignored} != {
        k: v for k, v in previous.items() if k not in ignored}
    return {channel["id"] for channel in config["channels"]
            if global_change or channel != old.get(channel["id"])}


def previous_config(root: Path, revision: str) -> dict:
    if not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", revision):
        raise SyncError("--changed-since requiere el SHA completo del commit anterior.")
    if set(revision) == {"0"}:
        return {"channels": []}
    result = subprocess.run(["git", "show", f"{revision}:sources.json"], cwd=root,
                            capture_output=True, encoding="utf-8", check=False)
    try:
        if result.returncode:
            raise ValueError
        config = json.loads(result.stdout)
        if not isinstance(config, dict) or not isinstance(config.get("channels"), list):
            raise ValueError
        if any(not isinstance(c, dict) or not isinstance(c.get("id"), str) for c in config["channels"]):
            raise ValueError
        return config
    except (ValueError, TypeError):
        raise SyncError("No se pudo leer sources.json del commit anterior; no se consultó YouTube.") from None


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="auto").replace("+00:00", "Z")


def progress(event: str, **details):
    """Avance inmediato en stderr; stdout conserva el reporte JSON final."""
    print(json.dumps({"event": event, "at": stamp(datetime.now(timezone.utc)), **details},
                     ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)


def date(value: str) -> datetime:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value):
            raise ValueError
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except (ValueError, TypeError):
        raise SyncError("Fecha UTC inválida; debe terminar en Z.") from None


def publication_date(value) -> datetime | None:
    """YouTube usa RFC 3339; normaliza también desplazamientos a UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def age_cutoff(now: datetime, days: int | None) -> datetime | None:
    if days is None:
        return None
    try:
        return now - timedelta(days=days)
    except OverflowError:
        return datetime.min.replace(tzinfo=timezone.utc)


def within_window(published: datetime | None, cutoff: datetime | None, now: datetime) -> bool:
    return cutoff is None or (published is not None and cutoff <= published <= now)


def load(path: Path, default=None):
    if not path.exists() and default is not None:
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise SyncError(f"No se pudo leer JSON válido: {path.name}.") from None


def save(path: Path, value, *, compact=False) -> bool:
    """Guarda atómicamente; compact migra también JSON con datos equivalentes."""
    serialized = json.dumps(value, ensure_ascii=False, allow_nan=False,
                            **({"separators": (",", ":")} if compact else {"indent": 2}))
    if not compact:
        serialized += "\n"
    if path.exists() and (path.read_bytes() == serialized.encode("utf-8") if compact else load(path) == value):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sync-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


@contextmanager
def lock(root: Path):
    path = root / ".sync.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SyncError("Ya hay una sincronización en ejecución (.sync.lock).") from None
    try:
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def source_type(source: str) -> tuple[str, str]:
    if not isinstance(source, str) or source != source.strip():
        raise SyncError("Cada fuente debe ser un ID o @handle sin espacios externos.")
    if source.startswith("video:") and VIDEO.fullmatch(source[6:]):
        return "video", source[6:]
    if source.startswith("playlist:") and re.fullmatch(r"[A-Za-z0-9_-]+", source[9:]):
        return "playlist", source[9:]
    if CHANNEL.fullmatch(source):
        return "channel", source
    if re.fullmatch(r"@[^\s/@?#\\]+", source):
        return "handle", source
    if VIDEO.fullmatch(source):
        return "video", source
    if re.fullmatch(r"[A-Za-z0-9_-]{12,}", source):
        return "playlist", source
    raise SyncError("Fuente inválida: usá UC..., @handle, ID de playlist o ID de video.")


def positive_int(value, name: str, minimum=1):
    if type(value) is not int or value < minimum:
        raise SyncError(f"{name} debe ser un entero >= {minimum}.")
    return value


def safe_file(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not re.fullmatch(r"channels/[a-z0-9][a-z0-9-]{0,63}\.json", value):
        raise SyncError("file debe ser channels/<id>.json.")
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()):
        raise SyncError("Ruta de canal fuera del repositorio.")
    return path


def validate_playlist(playlist: dict, mode: str):
    if not isinstance(playlist, dict) or not isinstance(playlist.get("videos"), list) or not playlist["videos"]:
        raise SyncError("Un canal publicado debe tener videos.")
    for row in playlist["videos"]:
        if not isinstance(row, dict) or not VIDEO.fullmatch(row.get("youtubeVideoId", "")):
            raise SyncError("ID inválido en un archivo de reproducción.")
        for field in ("title", "youtubeChannelName"):
            if row.get(field) is not None and (not isinstance(row[field], str) or not row[field].strip()):
                raise SyncError(f"{field} debe ser un texto no vacío cuando está presente.")
        if mode == "cycle":
            positive_int(row.get("durationSeconds"), "durationSeconds")
    if mode == "cycle":
        date(playlist.get("cycleStartsAt"))
        if sum(row["durationSeconds"] for row in playlist["videos"]) > 2**63 - 1:
            raise SyncError("La duración total supera el límite de la app.")
    elif (len(playlist["videos"]) != 1 or "cycleStartsAt" in playlist or
          "durationSeconds" in playlist["videos"][0]):
        raise SyncError("Un directo requiere un solo ID, sin duración ni origen de ciclo.")


def read_config(root: Path):
    config = load(root / "sources.json")
    index = load(root / "channels.json")
    if not isinstance(config, dict) or config.get("version") != 1 or not isinstance(config.get("channels"), list):
        raise SyncError("sources.json requiere version=1 y channels=[...].")
    if not isinstance(index, dict) or not isinstance(index.get("channels"), list):
        raise SyncError("channels.json requiere channels=[...].")
    positive_int(config.get("quotaBudget", 2500), "quotaBudget")
    for field in ("defaults", "limits"):
        if not isinstance(config.get(field, {}), dict):
            raise SyncError(f"{field} debe ser un objeto.")
    validate_limits(config.get("limits", {}))
    ids, numbers, files = set(), set(), set()
    for entry in index["channels"]:
        identifier = entry.get("id", "")
        if not SLUG.fullmatch(identifier) or identifier in ids:
            raise SyncError("ID inválido o duplicado en channels.json.")
        number = positive_int(entry.get("number"), "number")
        if number in numbers or entry.get("file") in files:
            raise SyncError("Número o archivo duplicado en channels.json.")
        if not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise SyncError("Falta el nombre de un canal.")
        if entry.get("playbackMode") not in {"cycle", "live"}:
            raise SyncError("playbackMode debe ser cycle o live.")
        if entry.get("guideMode", "schedule") not in {"schedule", "continuous"}:
            raise SyncError("guideMode debe ser schedule o continuous.")
        validate_playlist(load(safe_file(root, entry["file"])), entry["playbackMode"])
        ids.add(identifier); numbers.add(number); files.add(entry["file"])
    configured = set()
    requested_numbers = set()
    for channel in config["channels"]:
        if not isinstance(channel, dict):
            raise SyncError("Cada canal debe ser un objeto.")
        identifier = channel.get("id", "")
        if not SLUG.fullmatch(identifier) or identifier in configured:
            raise SyncError("ID inválido o duplicado en sources.json.")
        configured.add(identifier)
        if not isinstance(channel.get("name"), str) or not channel["name"].strip():
            raise SyncError("Cada configuración requiere name.")
        if type(channel.get("active", True)) is not bool:
            raise SyncError("active debe ser true o false.")
        if channel.get("updateFrequency", "daily") not in ("daily", "weekly"):
            raise SyncError("updateFrequency debe ser daily o weekly.")
        validate_limits(channel.get("limits", {}))
        if not isinstance(channel.get("rules", {}), dict):
            raise SyncError("rules debe ser un objeto.")
        video_order = channel.get("videoOrder", "random")
        if not isinstance(video_order, str) or video_order not in {"random", "ordered"}:
            raise SyncError("videoOrder debe ser random u ordered.")
        mode = channel.get("playbackMode", "cycle")
        if channel.get("guideMode", "schedule") not in {"schedule", "continuous"}:
            raise SyncError("guideMode debe ser schedule o continuous.")
        if mode not in {"cycle", "live"}:
            raise SyncError("playbackMode debe ser cycle o live.")
        existing = next((entry for entry in index["channels"] if entry["id"] == identifier), None)
        if existing and existing["playbackMode"] != mode:
            raise SyncError("No se cambia automáticamente el modo de un canal existente.")
        if "number" in channel:
            positive_int(channel["number"], "number")
            if channel["number"] in requested_numbers:
                raise SyncError("Número duplicado en sources.json.")
            requested_numbers.add(channel["number"])
        excluded = channel.get("excludedVideoIds", [])
        if not isinstance(excluded, list) or any(not isinstance(identifier, str) or not VIDEO.fullmatch(identifier) for identifier in excluded):
            raise SyncError("excludedVideoIds debe ser una lista de IDs de video válidos.")
        refs = channel.get("sources")
        if not isinstance(refs, list):
            raise SyncError("sources debe ser una lista de fuentes independientes.")
        for ref in refs:
            source_type(ref)
        if len(refs) != len(set(refs)):
            raise SyncError("Hay fuentes duplicadas en un canal.")
        if mode == "live" and refs and (len(refs) != 1 or source_type(refs[0])[0] != "video"):
            raise SyncError("Para live configurá únicamente el video de la emisión.")
        update_mode = channel.get("updateMode", "append")
        if not isinstance(update_mode, str) or update_mode not in {"append", "replace"}:
            raise SyncError("updateMode debe ser append o replace.")
        if update_mode == "replace" and not refs:
            raise SyncError("updateMode=replace requiere al menos una fuente.")
        rules = {**DEFAULT_RULES, **config.get("defaults", {}), **channel.get("rules", {})}
        if rules["maxAgeDays"] is not None:
            positive_int(rules["maxAgeDays"], "maxAgeDays")
        positive_int(rules["minDurationSeconds"], "minDurationSeconds", 0)
        maximum = rules["maxDurationSeconds"]
        if maximum is not None and positive_int(maximum, "maxDurationSeconds") <= rules["minDurationSeconds"]:
            raise SyncError("maxDurationSeconds debe superar el mínimo.")
        if type(rules["music"]) is not bool or not isinstance(rules["regionCodes"], list) or not rules["regionCodes"]:
            raise SyncError("rules requiere music booleano y regionCodes no vacío.")
        if any(not isinstance(code, str) or not re.fullmatch(r"[A-Z]{2}", code) for code in rules["regionCodes"]):
            raise SyncError("regionCodes requiere códigos de país de dos letras mayúsculas.")
    # También permite intercambiar números en una misma edición de sources.json.
    effective = []
    by_id = {channel["id"]: channel for channel in config["channels"]}
    for entry in index["channels"]:
        if entry["id"] in by_id:
            effective.append(by_id[entry["id"]].get("number", entry["number"]))
    if len(effective) != len(set(effective)):
        raise SyncError("Los números configurados colisionan con canales publicados.")
    for channel in config["channels"]:
        if channel["id"] not in ids and channel.get("number") in effective:
            raise SyncError("El número solicitado ya está ocupado.")
    return config, index


def validate_limits(limits):
    if not isinstance(limits, dict) or set(limits) - set(DEFAULT_LIMITS):
        raise SyncError("limits admite videosPerDay y maxPagesPerSource.")
    for field, value in limits.items():
        positive_int(value, field)


class YouTube:
    """Los tres endpoints usados cuestan 1 unidad por solicitud, incluidos reintentos."""
    def __init__(self, key: str, budget: int, opener=urlopen, sleep=time.sleep):
        self.key, self.budget, self.used = key, budget, 0
        self.opener, self.sleep = opener, sleep
        self.video_cache = {}

    def get(self, resource: str, **params):
        if resource not in {"channels", "playlistItems", "videos"}:
            raise SyncError("Endpoint no permitido por el presupuesto de cuota.")
        url = "https://www.googleapis.com/youtube/v3/" + resource + "?" + urlencode({**params, "key": self.key})
        for attempt in range(4):
            if self.used >= self.budget:
                progress("quota_exhausted", reason="budget", resource=resource,
                         quotaUnits=self.used, quotaBudget=self.budget)
                raise QuotaError("Se alcanzó el presupuesto de cuota; la próxima ejecución empieza desde cero.")
            self.used += 1
            try:
                with self.opener(url, timeout=30) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    raise SyncError("Respuesta de YouTube incompleta; no se avanza esta página.")
                if any(not isinstance(item, dict) for item in payload["items"]):
                    raise SyncError("YouTube devolvió elementos inválidos; no se avanza esta página.")
                if resource == "videos":
                    requested = set(params.get("id", "").split(","))
                    for item in payload["items"]:
                        if item.get("id") not in requested or any(
                            key in item and not isinstance(item[key], dict)
                            for key in ("snippet", "contentDetails", "status", "liveStreamingDetails")
                        ):
                            raise SyncError("Metadatos de YouTube inválidos; no se modifica disponibilidad.")
                return payload
            except HTTPError as exc:
                code = exc.code
                try:
                    body = json.loads(exc.read())
                    reasons = {item.get("reason") for item in body.get("error", {}).get("errors", [])}
                except (ValueError, AttributeError, TypeError, OSError, HTTPException):
                    reasons = set()
                finally:
                    exc.close()
                if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
                    progress("quota_exhausted", reason="youtube", resource=resource,
                             quotaUnits=self.used, quotaBudget=self.budget)
                    raise QuotaError("YouTube agotó la cuota; se conservan las listas publicadas válidas.") from None
                if code not in {408, 429, 500, 502, 503, 504} and not reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
                    raise SyncError(f"YouTube HTTP {code}; se conserva la programación.") from None
            except (URLError, TimeoutError, OSError, HTTPException, ValueError):
                pass
            if attempt < 3:
                delay = 2**attempt + random.random()
                # Nunca registrar exc, URL, parámetros, clave ni respuesta cruda.
                progress("api_retry", resource=resource, attempt=attempt + 1,
                         maxAttempts=4, waitSeconds=round(delay, 3),
                         quotaUnits=self.used, quotaBudget=self.budget)
                self.sleep(delay)
        progress("api_failed", resource=resource, attempts=4,
                 quotaUnits=self.used, quotaBudget=self.budget)
        raise SyncError("YouTube no respondió después de cuatro intentos.") from None

    def videos(self, ids: list[str]) -> dict:
        missing = list(dict.fromkeys(identifier for identifier in ids if identifier not in self.video_cache))
        for start in range(0, len(missing), 50):
            batch = missing[start:start + 50]
            payload = self.get("videos", part="snippet,contentDetails,status,liveStreamingDetails", id=",".join(batch),
                               fields="items(id,snippet(title,channelTitle,publishedAt,defaultAudioLanguage,liveBroadcastContent),contentDetails(duration,regionRestriction,contentRating),status(privacyStatus,uploadStatus,embeddable),liveStreamingDetails(actualStartTime,actualEndTime))")
            found = {item.get("id"): item for item in payload["items"]}
            for identifier in batch:
                self.video_cache[identifier] = found.get(identifier)
        return {identifier: self.video_cache[identifier] for identifier in ids}


def duration(value: str) -> int | None:
    match = DURATION.fullmatch(value)
    if not match or not any(match.groups()):
        return None
    seconds = sum(float(part or 0) * multiplier for part, multiplier in zip(match.groups(), (86400, 3600, 60, 1)))
    return int(seconds) if math.isfinite(seconds) and seconds > 0 and seconds.is_integer() else None


def assess(item: dict | None, rules: dict, mode: str) -> tuple[str, str, dict]:
    if item is None:
        return "unavailable", "missing_or_private", {}
    snippet, details, status = (item.get(key, {}) for key in ("snippet", "contentDetails", "status"))
    live = item.get("liveStreamingDetails", {})
    metadata = {"durationSeconds": duration(details.get("duration", ""))}
    if not all(key in status for key in ("privacyStatus", "uploadStatus", "embeddable")):
        return "pending", "availability_unknown", metadata
    # Active broadcasts can remain "uploaded" until the recording is processed.
    active_live = mode == "live" and live.get("actualStartTime") and not live.get("actualEndTime")
    playable_upload = status.get("uploadStatus") == "processed" or (
        active_live and status.get("uploadStatus") == "uploaded")
    if status.get("privacyStatus") not in {"public", "unlisted"} or not playable_upload:
        return "unavailable", "not_public_or_processed", metadata
    if status.get("embeddable") is not True:
        return "unavailable", "not_embeddable", metadata
    restriction = details.get("regionRestriction", {})
    for region in rules["regionCodes"]:
        if region in restriction.get("blocked", []) or ("allowed" in restriction and region not in restriction["allowed"]):
            return "unavailable", "region_restricted", metadata
    if details.get("contentRating", {}).get("ytRating") == "ytAgeRestricted":
        return "unavailable", "age_restricted", metadata
    if mode == "live":
        if not live.get("actualStartTime") or live.get("actualEndTime"):
            return "pending", "not_live", metadata
    else:
        if snippet.get("liveBroadcastContent") in {"live", "upcoming"} or (live and not live.get("actualEndTime")):
            return "pending", "live_or_upcoming", metadata
        seconds = metadata["durationSeconds"]
        if seconds is None:
            return "pending", "duration_unknown", metadata
        if seconds <= rules["minDurationSeconds"] or (rules["maxDurationSeconds"] is not None and seconds >= rules["maxDurationSeconds"]):
            return "rejected", "duration_out_of_range", metadata
    if not rules["music"]:
        audio = snippet.get("defaultAudioLanguage")
        if not audio:
            return "pending", "audio_language_unknown", metadata
        if audio.lower().split("-")[0] != "es":
            return "rejected", "audio_not_spanish", metadata
    return "approved", "validated", metadata


def anchor_update(old: dict, rows: list[dict], now: datetime) -> str:
    """Mantiene el video/segundo UTC actual aunque cambie el módulo del ciclo."""
    origin = date(old["cycleStartsAt"])
    total = sum(row["durationSeconds"] for row in old["videos"])
    elapsed = math.floor((now - origin).total_seconds())
    position = elapsed % total
    offset = 0
    current = None
    current_index = 0
    occurrence = 0
    counts = {}
    for current_index, row in enumerate(old["videos"]):
        identifier = row["youtubeVideoId"]
        counts[identifier] = counts.get(identifier, 0) + 1
        if offset + row["durationSeconds"] > position:
            current = identifier
            occurrence = counts[identifier]
            break
        offset += row["durationSeconds"]
    before = 0
    count = 0
    for row in rows:
        if row["youtubeVideoId"] == current:
            count += 1
        if (row["youtubeVideoId"] == current and count == occurrence
                and position - offset < row["durationSeconds"]):
            # Origen del ciclo viejo en curso + desplazamiento antes del programa.
            return stamp(origin + timedelta(seconds=(elapsed // total) * total + offset - before))
        before += row["durationSeconds"]
    # Si se retira el programa actual, alinear el siguiente sobreviviente al final
    # nominal de ese programa. El reproductor de la app termina el video ya cargado.
    boundary = origin + timedelta(seconds=(elapsed // total) * total + offset + old["videos"][current_index]["durationSeconds"])
    kept = {row["youtubeVideoId"] for row in rows}
    following = old["videos"][current_index + 1:] + old["videos"][:current_index]
    target = next((row["youtubeVideoId"] for row in following if row["youtubeVideoId"] in kept), rows[0]["youtubeVideoId"])
    before = 0
    for row in rows:
        if row["youtubeVideoId"] == target:
            return stamp(boundary - timedelta(seconds=before))
        before += row["durationSeconds"]
    raise SyncError("No se pudo alinear la programación.")


class Sync:
    """Los JSON de reproducción son la única lista de IDs ya incorporados."""
    def __init__(self, root: Path, api: YouTube | None, now: datetime | None = None):
        self.root, self.api = root, api
        self.now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        self.when = stamp(self.now)
        self.config, self.index = read_config(root)
        self.report = {"channels": {}}
        self.errors = []
        self.resolved = {}
        self.verified_ids = set()
        self.started = time.monotonic()

    def progress(self, event, **details):
        used = self.api.used if self.api else 0
        budget = self.config.get("quotaBudget", 2500)
        progress(event, elapsedSeconds=round(time.monotonic() - self.started, 3),
                 quotaUnits=used, quotaBudget=budget, quotaRemaining=max(0, budget - used), **details)

    def rules(self, channel):
        return {**DEFAULT_RULES, **self.config.get("defaults", {}), **channel.get("rules", {})}

    def limits(self, channel):
        return {**DEFAULT_LIMITS, **self.config.get("limits", {}), **channel.get("limits", {})}

    def selection(self, selected=None, frequency=None):
        ids = {selected} if isinstance(selected, str) else set(selected) if selected is not None else None
        if ids and ids - {channel["id"] for channel in self.config["channels"]}:
            raise SyncError("El canal seleccionado no existe en sources.json.")
        return [channel for channel in self.config["channels"] if channel.get("active", True)
                and (ids is None or channel["id"] in ids)
                and (frequency is None or channel.get("updateFrequency", "daily") == frequency)]

    def emit_report(self, mode, result):
        self.report.update(at=self.when, mode=mode, quotaUnits=self.api.used if self.api else 0,
                           result="partial" if result else "success", errors=self.errors)
        # Sólo stdout: Actions conserva los logs, nunca se escribe un reporte al repo.
        print(json.dumps(self.report, ensure_ascii=False, allow_nan=False), flush=True)

    def baseline(self, channel):
        identifier = channel["id"]
        entry = next((entry for entry in self.index["channels"] if entry["id"] == identifier), None)
        path = safe_file(self.root, entry["file"] if entry else f"channels/{identifier}.json")
        if not entry and path.exists():
            raise SyncError("Ya existe un archivo sin entrada de índice; incorporalo manualmente antes de sincronizar.")
        return entry, path, load(path) if entry else None

    def iter_source(self, source, cutoff=None, limits=None, days=None, stats=None):
        """Recorrido acotado. Tokens y caché viven sólo durante esta ejecución."""
        limits = limits or DEFAULT_LIMITS
        kind, value = source_type(source)
        if kind == "video":
            yield [value]
            return
        if kind in {"channel", "handle"}:
            if source not in self.resolved:
                result = self.api.get("channels", part="contentDetails", **({"forHandle": value} if kind == "handle" else {"id": value}),
                                      fields="items/contentDetails/relatedPlaylists/uploads")
                if not result["items"]:
                    raise SyncError("No se encontró el canal de YouTube.")
                playlist = result["items"][0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                if not isinstance(playlist, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", playlist):
                    raise SyncError("El canal no tiene playlist de subidas verificable.")
                self.resolved[source] = playlist
            value = self.resolved[source]
        token = None
        seen_tokens = set()
        maximum = days * limits["videosPerDay"] if days is not None and kind in {"channel", "handle"} else None
        remaining = maximum
        for _ in range(limits["maxPagesPerSource"]):
            requested = min(50, remaining) if remaining is not None else 50
            page = self.api.get("playlistItems", part="contentDetails", playlistId=value, maxResults=requested,
                                fields="nextPageToken,items/contentDetails(videoId,videoPublishedAt)", **({"pageToken": token} if token else {}))
            if len(page["items"]) > requested:
                raise SyncError("YouTube devolvió más candidatos que el límite solicitado.")
            if stats is not None:
                stats["listPages"] += 1
                stats["candidatesScanned"] += len(page["items"])
            ids = list(dict.fromkeys(item.get("contentDetails", {}).get("videoId", "") for item in page["items"]))
            if any(not isinstance(identifier, str) or not VIDEO.fullmatch(identifier) for identifier in ids):
                raise SyncError("Página con IDs incompletos; no se incorporan sus videos.")
            if cutoff is not None:
                # La fecha de publicación del video es distinta de su alta en la
                # playlist. Sin fecha, consultar videos.list antes de decidir.
                ids = list(dict.fromkeys(
                    item["contentDetails"]["videoId"] for item in page["items"]
                    if (published := publication_date(item["contentDetails"].get("videoPublishedAt"))) is None
                    or within_window(published, cutoff, self.now)
                ))
            yield ids
            token = page.get("nextPageToken")
            if token is None:
                return
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise SyncError("YouTube devolvió un token de paginación inválido o repetido.")
            seen_tokens.add(token)
            # Sólo para la playlist de subidas, recorrida de reciente a antiguo.
            # Nunca se aplica a playlists editoriales ni se corta por un solo ID viejo.
            dates = [publication_date(item["contentDetails"].get("videoPublishedAt")) for item in page["items"]]
            if kind in {"channel", "handle"} and cutoff is not None and dates and all(
                    published is not None and published < cutoff for published in dates):
                return
            if remaining is not None:
                remaining -= len(page["items"])
                if remaining <= 0:
                    raise IncompleteSource("Se alcanzó maxAgeDays × videosPerDay; la ventana podría estar incompleta.")
        raise IncompleteSource("Se alcanzó maxPagesPerSource; el recorrido quedó incompleto.")

    def discover(self, channel, rows, stats):
        excluded = set(channel.get("excludedVideoIds", []))
        seen = {row["youtubeVideoId"] for row in rows} | excluded
        discovered = set()
        rules = self.rules(channel)
        cutoff = age_cutoff(self.now, rules["maxAgeDays"])
        for source_index, source in enumerate(channel["sources"], 1):
            stats["sourcesScanned"] += 1
            pages_before = stats["listPages"]
            self.progress("source_start", channel=channel["id"], sourceIndex=source_index,
                          sourceCount=len(channel["sources"]), sourceType=source_type(source)[0])
            source_result = "success"
            try:
                for ids in self.iter_source(source, cutoff, self.limits(channel), rules["maxAgeDays"], stats):
                    self.progress("source_batch", channel=channel["id"], sourceIndex=source_index,
                                  sourcePages=stats["listPages"] - pages_before,
                                  listPages=stats["listPages"], candidatesScanned=stats["candidatesScanned"])
                    discovered.update(ids)
                    new_ids = [identifier for identifier in ids if identifier not in seen]
                    if not new_ids:
                        continue
                    # En replace, rows empieza vacío y se revalidan también los
                    # videos que ya estaban publicados.
                    items = self.api.videos(new_ids)
                    for identifier in new_ids:
                        item = items[identifier]
                        status, _, metadata = assess(item, rules, channel.get("playbackMode", "cycle"))
                        published = publication_date(item.get("snippet", {}).get("publishedAt")) if item else None
                        if status == "approved" and within_window(published, cutoff, self.now):
                            row = {"youtubeVideoId": identifier}
                            self.apply_video_metadata(row, item)
                            if channel.get("playbackMode", "cycle") == "cycle":
                                row["durationSeconds"] = metadata["durationSeconds"]
                            rows.append(row)
                            stats["addedVideos"] += 1
                        else:
                            stats["filteredVideos"] += 1
                        seen.add(identifier)
            except SyncError as exc:
                source_result = "partial"
                self.progress("source_error", channel=channel["id"], sourceIndex=source_index, error=str(exc))
                stats["discoveredIds"] = len(discovered)
                stats["errors"].append(str(exc))
                if isinstance(exc, QuotaError):
                    raise
            finally:
                self.progress("source_complete", channel=channel["id"], sourceIndex=source_index,
                              result=source_result, sourcePages=stats["listPages"] - pages_before,
                              listPages=stats["listPages"], candidatesScanned=stats["candidatesScanned"],
                              addedVideos=stats["addedVideos"])
        stats["discoveredIds"] = len(discovered)

    def maintain(self, channel, rows, stats):
        # Se retiran sólo bajas verificadas; un error de red conserva el resto.
        ids = list(dict.fromkeys(row["youtubeVideoId"] for row in rows))
        by_id = self.rows_by_id(rows)
        unavailable = set()
        rules = self.rules(channel)
        cutoff = age_cutoff(self.now, rules["maxAgeDays"])
        try:
            for start in range(0, len(ids), 50):
                batch = ids[start:start + 50]
                items = self.api.videos(batch)
                for identifier in batch:
                    item = items[identifier]
                    for row in by_id[identifier]:
                        self.apply_video_metadata(row, item)
                        current_duration = duration(item.get("contentDetails", {}).get("duration", "")) if item else None
                        if "durationSeconds" in row and current_duration is not None:
                            row["durationSeconds"] = current_duration
                    if assess(item, rules, channel.get("playbackMode", "cycle"))[0] == "unavailable":
                        unavailable.add(identifier)
                    elif channel.get("updateMode", "append") == "replace" and item is not None and not within_window(
                            publication_date(item.get("snippet", {}).get("publishedAt")), cutoff, self.now):
                        unavailable.add(identifier)
                stats["checkedVideos"] += len(batch)
                self.progress("maintenance_batch", channel=channel["id"],
                              checkedVideos=stats["checkedVideos"], totalVideos=len(ids))
        finally:
            # Un solo filtro O(N), también si un lote posterior falla. En replace,
            # run conserva la lista anterior completa ante cualquier error.
            stats["removedVideos"] += sum(len(by_id[identifier]) for identifier in unavailable)
            rows[:] = [row for row in rows if row["youtubeVideoId"] not in unavailable]

    def publish(self, channel, entry, path, old, rows, allow_empty=False):
        mode = channel.get("playbackMode", "cycle")
        if not rows:
            if allow_empty and entry:
                # Un reemplazo completo sin resultados retira el canal del índice:
                # Android no admite listas vacías y no debe mostrar material viejo.
                self.index["channels"].remove(entry)
                save(self.root / "channels.json", self.index, compact=True)
                path.unlink(missing_ok=True)
                return "unpublished_empty"
            # Android no admite listas vacías. Mantener la última válida.
            return "held_empty" if old else "awaiting_valid_videos"
        if mode == "live":
            rows = rows[:1]
        playlist = deepcopy(old) if old else {}
        playlist["videos"] = rows
        # Only API responses can renew this clock. A download, export, failed
        # maintenance or an append of new videos must never rejuvenate old data.
        if all(row["youtubeVideoId"] in self.verified_ids for row in rows):
            playlist["verifiedAt"] = self.when
        playback = lambda videos: [(row["youtubeVideoId"], row.get("durationSeconds")) for row in videos]
        if mode == "cycle" and (not old or playback(old["videos"]) != playback(rows)):
            playlist["cycleStartsAt"] = anchor_update(old, rows, self.now) if old else self.when
        self.clean_titles(playlist["videos"])
        validate_playlist(playlist, mode)
        if not entry:
            reserved = [item.get("number", 0) for item in self.config["channels"]]
            number = channel.get("number") or max([item["number"] for item in self.index["channels"]] + reserved + [0]) + 1
            entry = {"id": channel["id"], "number": number, "name": channel["name"], "playbackMode": mode,
                     "file": f"channels/{channel['id']}.json"}
            self.index["channels"].append(entry)
        entry["name"] = channel["name"]
        if "number" in channel:
            entry["number"] = channel["number"]
        self.guide_metadata(channel, entry)
        save(path, playlist, compact=True)
        return "published"

    def run(self, mode="daily", selected=None, frequency=None):
        if mode == "update":
            mode = "daily"
        if mode == "names":
            return self.refresh_channel_names(selected)
        if mode == "presentation":
            return self.refresh_presentation(selected)
        if mode == "titles":
            return self.refresh_titles(selected)
        self.report = {"channels": {}}
        self.errors = []
        channels = self.selection(selected, frequency)
        self.progress("run_start", mode=mode, channelCount=len(channels))
        baselines = {channel["id"]: self.baseline(channel) for channel in channels}
        # El índice publicado deriva de la configuración. Borrar una configuración
        # retira el canal; active=false solamente pausa las consultas.
        configured = {channel["id"]: channel for channel in self.config["channels"]}
        removed = [entry for entry in self.index["channels"] if entry["id"] not in configured]
        self.index["channels"] = [entry for entry in self.index["channels"] if entry["id"] in configured]
        for entry in self.index["channels"]:
            channel = configured[entry["id"]]
            entry["name"] = channel["name"]
            entry["number"] = channel.get("number", entry["number"])
            self.guide_metadata(channel, entry)
        exhausted = False
        for channel_index, channel in enumerate(channels, 1):
            self.progress("channel_start", channel=channel["id"], channelIndex=channel_index,
                          channelCount=len(channels), sourceCount=len(channel["sources"]))
            used_before = self.api.used
            entry, path, old = baselines[channel["id"]]
            excluded = set(channel.get("excludedVideoIds", []))
            rows = [deepcopy(row) for row in old["videos"] if row["youtubeVideoId"] not in excluded] if old else []
            stats = {"at": self.when, "mode": mode, "result": "success", "sourcesScanned": 0,
                     "listPages": 0, "candidatesScanned": 0,
                     "discoveredIds": 0, "addedVideos": 0, "filteredVideos": 0, "checkedVideos": 0,
                     "removedVideos": len(old["videos"]) - len(rows) if old else 0, "errors": []}
            replace = channel.get("updateMode", "append") == "replace"
            if replace and mode == "daily":
                rows = []
            if mode == "daily" and channel.get("playbackMode", "cycle") == "live" and channel["sources"]:
                rows = []
                replace = True
            if exhausted:
                stats.update(result="skipped", errors=["Sin cuota disponible; canal pendiente."])
            else:
                try:
                    if mode == "daily":
                        try:
                            last_verified = date(old.get("verifiedAt", "")) if old else self.now
                            needs_verification = self.now - last_verified >= timedelta(days=7)
                        except (ValueError, TypeError, SyncError):
                            needs_verification = True
                        if old and needs_verification:
                            self.maintain(channel, rows, stats)
                        self.discover(channel, rows, stats)
                        if not replace:
                            self.fill_titles(rows, channel_id=channel["id"])
                    else:
                        self.maintain(channel, rows, stats)
                except SyncError as exc:
                    if str(exc) not in stats["errors"]:
                        stats["errors"].append(str(exc))
                    exhausted = isinstance(exc, QuotaError)
                if stats["errors"]:
                    stats["result"] = "partial"
            self.errors.extend(f"{channel['id']}: {error}" for error in stats["errors"])
            if replace and stats["result"] != "success":
                stats["publication"] = "held_incomplete" if old else "awaiting_complete_refresh"
                stats["addedVideos"] = stats["removedVideos"] = 0
            else:
                # En append se mezcla sólo el lote nuevo; en replace diario,
                # toda la lista reconstruida. El mantenimiento conserva el orden.
                if mode == "daily" and channel.get("videoOrder", "random") == "random":
                    new_start = 0 if replace else len(rows) - stats["addedVideos"]
                    added = rows[new_start:]
                    if len(added) > 1:
                        random.shuffle(added)
                        rows[new_start:] = added
                if replace and mode == "daily":
                    old_ids = {row["youtubeVideoId"] for row in old["videos"]} if old else set()
                    new_ids = {row["youtubeVideoId"] for row in rows}
                    stats["addedVideos"] = len(new_ids - old_ids)
                    stats["removedVideos"] = sum(row["youtubeVideoId"] not in new_ids for row in old["videos"]) if old else 0
                stats["publication"] = self.publish(channel, entry, path, old, rows, allow_empty=replace)
            stats["quotaUnits"] = self.api.used - used_before
            self.report["channels"][channel["id"]] = stats
            self.progress("channel_complete", channel=channel["id"], result=stats["result"],
                          publication=stats["publication"], listPages=stats["listPages"],
                          candidatesScanned=stats["candidatesScanned"], addedVideos=stats["addedVideos"],
                          removedVideos=stats["removedVideos"], errorCount=len(stats["errors"]))
        # Also remove expired/inactive copies when an API request fails. Never
        # substitute the publication time or filesystem mtime for verification.
        for current in list(self.index["channels"]):
            current_path = safe_file(self.root, current["file"])
            document = load(current_path)
            try:
                verified = date(document.get("verifiedAt", ""))
                fresh = self.now - METADATA_MAX_AGE < verified <= self.now + timedelta(minutes=5)
            except (ValueError, TypeError, SyncError):
                fresh = False
            if not fresh:
                self.index["channels"].remove(current)
                current_path.unlink(missing_ok=True)
                self.progress("metadata_expired", channel=current["id"])
        save(self.root / "channels.json", self.index, compact=True)
        for entry in removed:
            safe_file(self.root, entry["file"]).unlink(missing_ok=True)
        self.progress("run_complete", mode=mode, channelCount=len(channels),
                      result="partial" if self.errors else "success", errorCount=len(self.errors))
        return 2 if self.errors else 0

    def fill_titles(self, rows, refresh=False, channel_id=None):
        """Enriquece sin reordenar, filtrar ni cambiar duraciones/orígenes."""
        ids = list(dict.fromkeys(row["youtubeVideoId"] for row in rows
                                if refresh or not row.get("title") or not row.get("youtubeChannelName")))
        by_id = self.rows_by_id(rows)
        for start in range(0, len(ids), 50):
            items = self.api.videos(ids[start:start + 50])
            for identifier, item in items.items():
                for row in by_id[identifier]:
                    self.apply_video_metadata(row, item, refresh=refresh)
            self.progress("metadata_batch", channel=channel_id,
                          checkedVideos=min(start + 50, len(ids)), totalVideos=len(ids))

    @staticmethod
    def rows_by_id(rows):
        result = {}
        for row in rows:
            result.setdefault(row["youtubeVideoId"], []).append(row)
        return result

    def apply_video_metadata(self, row, item, refresh=True):
        if refresh and item is not None and item.get("snippet", {}).get("title") and item.get("snippet", {}).get("channelTitle"):
            self.verified_ids.add(row["youtubeVideoId"])
        snippet = item.get("snippet", {}) if item else {}
        title = snippet.get("title")
        if (refresh or not row.get("title")) and isinstance(title, str) and title.strip():
            row["title"] = normalize_title(title.strip()) or "Título no disponible"
        # The uploader's display name comes from this video's metadata, never
        # from the configured source (which may be another user's playlist).
        name = snippet.get("channelTitle")
        if (refresh or not row.get("youtubeChannelName")) and isinstance(name, str) and name.strip():
            row["youtubeChannelName"] = name.strip()

    @staticmethod
    def clean_titles(rows):
        for row in rows:
            if row.get("title"):
                row["title"] = normalize_title(row["title"]) or "Título no disponible"

    def guide_metadata(self, channel, entry):
        mode = channel.get("guideMode", "continuous" if self.rules(channel)["music"] else "schedule")
        if mode == "schedule":
            entry.pop("guideMode", None)
        else:
            entry["guideMode"] = mode

    def refresh_presentation(self, selected=None):
        entries = [entry for entry in self.index["channels"] if selected is None or entry["id"] == selected]
        if selected and not entries:
            raise SyncError("El canal seleccionado no está publicado.")
        for entry in entries:
            configured = next((channel for channel in self.config["channels"] if channel["id"] == entry["id"]), None)
            if configured:
                self.guide_metadata(configured, entry)
            path = safe_file(self.root, entry["file"])
            playlist = load(path)
            self.clean_titles(playlist["videos"])
            validate_playlist(playlist, entry["playbackMode"])
            save(path, playlist, compact=True)
        save(self.root / "channels.json", self.index, compact=True)
        return 0

    def refresh_channel_names(self, selected=None):
        """Completa sólo nombres; conserva exactamente el resto del catálogo."""
        entries = [entry for entry in self.index["channels"] if selected is None or entry["id"] == selected]
        if selected and not entries:
            raise SyncError("El canal seleccionado no está publicado.")
        exhausted = False
        total = named = 0
        for entry in entries:
            path = safe_file(self.root, entry["file"])
            playlist = load(path)
            rows = playlist["videos"]
            ids = list(dict.fromkeys(row["youtubeVideoId"] for row in rows if not row.get("youtubeChannelName")))
            by_id = self.rows_by_id(rows)
            if not exhausted:
                try:
                    for start in range(0, len(ids), 50):
                        items = self.api.videos(ids[start:start + 50])
                        for identifier, item in items.items():
                            name = item.get("snippet", {}).get("channelTitle") if item else None
                            for row in by_id[identifier]:
                                if not row.get("youtubeChannelName") and isinstance(name, str) and name.strip():
                                    row["youtubeChannelName"] = name.strip()
                except SyncError as exc:
                    self.errors.append(f"{entry['id']}: {exc}")
                    exhausted = isinstance(exc, QuotaError)
            validate_playlist(playlist, entry["playbackMode"])
            save(path, playlist, compact=True)
            count = sum(bool(row.get("youtubeChannelName")) for row in rows)
            total += len(rows)
            named += count
            print(f"Nombres {entry['id']}: {count}/{len(rows)}", flush=True)
        self.report["channelNames"] = {
            "at": self.when, "quotaUnits": self.api.used, "namedVideos": named,
            "totalVideos": total, "missingVideos": total - named, "errors": self.errors,
        }
        return 2 if self.errors else 0

    def refresh_titles(self, selected=None):
        entries = [entry for entry in self.index["channels"] if selected is None or entry["id"] == selected]
        if selected and not entries:
            raise SyncError("El canal seleccionado no está publicado.")
        for entry in entries:
            path = safe_file(self.root, entry["file"])
            playlist = load(path)
            try:
                self.fill_titles(playlist["videos"], refresh=True, channel_id=entry["id"])
            except SyncError as exc:
                self.errors.append(f"{entry['id']}: {exc}")
                # Los títulos ya verificados son seguros incluso si falla otro lote.
            validate_playlist(playlist, entry["playbackMode"])
            self.clean_titles(playlist["videos"])
            save(path, playlist, compact=True)
        save(self.root / "channels.json", self.index, compact=True)
        return 2 if self.errors else 0


def copy_backup(engine, app_root: Path):
    """Copia el catálogo validado a la APK; no descubre contenido ni usa la API."""
    if not (app_root / "app/src/main/AndroidManifest.xml").is_file():
        raise SyncError("--app-root debe señalar el proyecto Android que contiene app/src/main/AndroidManifest.xml.")
    target = app_root / "app/src/main/assets/catalog"
    if target.resolve() == engine.root.resolve():
        raise SyncError("El respaldo debe estar fuera del catálogo publicado.")
    playlists = [(entry, load(safe_file(engine.root, entry["file"]))) for entry in engine.index["channels"]]
    for entry, playlist in playlists:
        validate_playlist(playlist, entry["playbackMode"])
    for entry, playlist in playlists:
        save(safe_file(target, entry["file"]), playlist, compact=True)
    save(target / "channels.json", engine.index, compact=True)
    print(f"Respaldo copiado: {len(playlists)} canales. No se consultó YouTube.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["update", "daily", "maintenance", "names", "titles", "presentation", "validate", "backup"], default="update")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--channel", help="ID de un canal; omitido procesa todos los activos")
    selection.add_argument("--changed-since", help="Actualizar sólo configuraciones modificadas desde este SHA de Git")
    parser.add_argument("--frequency", choices=["daily", "weekly"], help="Filtro de frecuencia para ejecuciones programadas")
    parser.add_argument("--maintenance", action="store_true", help="Revisar todos los canales activos después de actualizar, con el mismo presupuesto")
    parser.add_argument("--app-root", type=Path, help="Proyecto Android de destino para --mode backup")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        if (args.changed_since or args.frequency or args.maintenance) and args.mode not in {"update", "daily"}:
            raise SyncError("--changed-since, --frequency y --maintenance sólo se usan con --mode update.")
        with lock(args.root):
            engine = Sync(args.root, None)
            if args.mode == "validate":
                for channel in engine.config["channels"]:
                    engine.baseline(channel)
                print("Configuración e índice válidos. No se consultó YouTube.")
                return 0
            if args.mode == "backup":
                if args.app_root is None:
                    raise SyncError("--mode backup requiere --app-root.")
                copy_backup(engine, args.app_root)
                return 0
            if args.mode == "presentation":
                result = engine.refresh_presentation(args.channel)
                print("Presentación actualizada sin consultar YouTube ni alterar la reproducción.")
                return result
            selected = changed_channels(engine.config, previous_config(args.root, args.changed_since)) if args.changed_since else args.channel
            targets = engine.selection(selected, args.frequency)
            maintenance_targets = engine.selection() if args.maintenance else []
            key = os.environ.get("YOUTUBE_API_KEY", "").strip()
            if not key and (targets or maintenance_targets or args.mode in {"names", "titles"}):
                raise SyncError("Falta YOUTUBE_API_KEY en el entorno de GitHub Actions Secrets.")
            engine.api = YouTube(key, engine.config.get("quotaBudget", 2500))
            result = engine.run(args.mode, selected, args.frequency)
            engine.emit_report(args.mode, result)
            if args.maintenance:
                maintenance_result = engine.run("maintenance")
                engine.emit_report("maintenance", maintenance_result)
                result = max(result, maintenance_result)
            return result
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Los tracebacks de urllib pueden incluir la API key en la URL.
        print("Error interno. Se conservaron los archivos ya guardados; ejecutar las pruebas antes de reintentar.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
