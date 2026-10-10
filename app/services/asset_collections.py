from __future__ import annotations

import json
from pathlib import Path
import re
from uuid import uuid4

from app.core.file_lock import InterProcessFileLock
from app.services.handoff import build_asset_handoff
from app.services.json_io import atomic_write_json
from app.services.scanner import read_manifest_readiness


def read_collections(path: Path) -> list[dict]:
    """Read local references without initializing storage or repairing bad data."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("collections_unreadable") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("collections"), list):
        raise ValueError("collections_unreadable")
    collections = payload["collections"]
    seen_ids, seen_names = set(), set()
    for item in collections:
        if not isinstance(item, dict) or set(item) != {"id", "name", "asset_ids"}:
            raise ValueError("collections_unreadable")
        if not isinstance(item["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", item["id"]):
            raise ValueError("collections_unreadable")
        name = _collection_name(item["name"])
        members = item["asset_ids"]
        if not isinstance(members, list) or any(not _asset_id(value) for value in members):
            raise ValueError("collections_unreadable")
        if item["id"] in seen_ids or name.casefold() in seen_names or len(set(members)) != len(members):
            raise ValueError("collections_unreadable")
        seen_ids.add(item["id"])
        seen_names.add(name.casefold())
    return collections


def _collection_name(value) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 64 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_collection_name")
    return value.strip()


def _asset_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def save_collection(path: Path, name: str, *, asset_id: str = "", collection_id: str = "") -> dict:
    name = _collection_name(name)
    if not isinstance(asset_id, str) or (asset_id and not _asset_id(asset_id)):
        raise ValueError("invalid_asset_id")
    if not isinstance(collection_id, str) or (collection_id and not re.fullmatch(r"[0-9a-f]{32}", collection_id)):
        raise ValueError("invalid_collection_id")
    if collection_id and asset_id:
        raise ValueError("invalid_collection_member")
    lock = InterProcessFileLock(path.with_suffix(".lock"))
    if not lock.acquire(timeout=0):
        raise TimeoutError("collections_busy")
    try:
        collections = read_collections(path)
        collection = next((item for item in collections if item["id"] == collection_id), None)
        if collection_id and collection is None:
            raise ValueError("collection_not_found")
        if any(_collection_name(item["name"]).casefold() == name.casefold() and item["id"] != collection_id for item in collections):
            raise ValueError("collection_name_exists")
        if collection is None:
            collection = {"id": uuid4().hex, "name": name, "asset_ids": [asset_id] if asset_id else []}
            collections.append(collection)
        else:
            collection["name"] = name
        atomic_write_json(path, {"version": 1, "collections": collections})
        return collection
    finally:
        lock.release()


def dissolve_collection(path: Path, collection_id: str) -> None:
    lock = InterProcessFileLock(path.with_suffix(".lock"))
    if not lock.acquire(timeout=0):
        raise TimeoutError("collections_busy")
    try:
        collections = read_collections(path)
        remaining = [item for item in collections if item["id"] != collection_id]
        if len(remaining) == len(collections):
            raise ValueError("collection_not_found")
        atomic_write_json(path, {"version": 1, "collections": remaining})
    finally:
        lock.release()


def set_collection_asset(path: Path, collection_id: str, asset_id: str, *, included: bool) -> None:
    if not _asset_id(asset_id) or not isinstance(included, bool):
        raise ValueError("invalid_collection_member")
    lock = InterProcessFileLock(path.with_suffix(".lock"))
    if not lock.acquire(timeout=0):
        raise TimeoutError("collections_busy")
    try:
        collections = read_collections(path)
        collection = next((item for item in collections if item["id"] == collection_id), None)
        if collection is None:
            raise ValueError("collection_not_found")
        members = collection["asset_ids"]
        if included and asset_id not in members:
            members.append(asset_id)
        elif not included and asset_id in members:
            members.remove(asset_id)
        atomic_write_json(path, {"version": 1, "collections": collections})
    finally:
        lock.release()


def collection_handoff(path: Path, collection_id: str, service, *, base_url: str) -> dict:
    collection = next((item for item in read_collections(path) if item["id"] == collection_id), None)
    if collection is None:
        raise ValueError("collection_not_found")
    members = set(collection["asset_ids"])
    bundles = [bundle for bundle in service.list_bundles(status="ready") if bundle["id"] in members]
    if not bundles:
        raise ValueError("collection_has_no_ready_assets")
    readiness = read_manifest_readiness(service.manifest_path)
    if readiness["manifest_generation"] != service.manifest_generation:
        raise ValueError("collection_catalog_changed")
    payload = build_asset_handoff(bundles, base_url=base_url, manifest_generation=service.manifest_generation)
    payload["collection"] = {"id": collection["id"], "name": collection["name"], "member_count": len(members),
                             "omitted_count": len(members) - len(bundles)}
    return payload
