"""Shared structural checks for immutable Scout release bundles."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_image_config(archive: Path) -> dict:
    """Read Docker-save metadata and verify config blob is content addressed."""
    try:
        with tarfile.open(archive, mode="r:*") as bundle:
            members = bundle.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)):
                raise ValueError("Docker archive contains duplicate member names")
            manifest_members = [
                member for member in members if member.name == "manifest.json"
            ]
            if len(manifest_members) != 1 or not manifest_members[0].isfile():
                raise ValueError("Docker archive manifest.json is missing or invalid")
            manifest_file = bundle.extractfile(manifest_members[0])
            if manifest_file is None:
                raise ValueError("Docker archive manifest.json is missing")
            entries = json.load(manifest_file)
            if not isinstance(entries, list) or len(entries) != 1:
                raise ValueError(
                    "Docker archive must contain exactly one image variant"
                )
            config_path = entries[0].get("Config")
            if not isinstance(config_path, str):
                raise TypeError("Docker archive config path is invalid")
            relative = PurePosixPath(config_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Docker archive config path escapes its archive")
            config_members = [
                member for member in members if member.name == config_path
            ]
            if len(config_members) != 1 or not config_members[0].isfile():
                raise ValueError("Docker archive config blob is missing or invalid")
            config_file = bundle.extractfile(config_members[0])
            if config_file is None:
                raise ValueError("Docker archive config blob is missing")
            config_bytes = config_file.read()
    except (
        OSError,
        tarfile.TarError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as error:
        raise ValueError("invalid Docker image archive") from error

    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    if PurePosixPath(config_path).name != config_digest.removeprefix("sha256:"):
        raise ValueError("Docker image config filename does not match its digest")
    try:
        config = json.loads(config_bytes)
        rootfs = config["rootfs"]
        if not isinstance(rootfs.get("diff_ids"), list) or not rootfs["diff_ids"]:
            raise ValueError("Docker image rootfs metadata is missing")
        image_config = config["config"]
        return {
            "config_digest": config_digest,
            "os": config["os"],
            "architecture": config["architecture"],
            "entrypoint": image_config.get("Entrypoint"),
            "cmd": image_config.get("Cmd"),
            "user": image_config.get("User"),
            "working_dir": image_config.get("WorkingDir"),
            "runtime_config": image_config,
            "diff_ids": rootfs["diff_ids"],
            "repo_tags": entries[0].get("RepoTags") or [],
        }
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as error:
        raise ValueError("Docker image config metadata is invalid") from error


def architecture_for_platform(target_platform: str) -> tuple[str, str]:
    supported = {
        "linux/amd64": ("linux", "amd64"),
        "linux/arm64": ("linux", "arm64"),
    }
    try:
        return supported[target_platform]
    except KeyError as error:
        raise ValueError("release target must be linux/amd64 or linux/arm64") from error


def verify_wheelhouse(wheelhouse: Path, target_platform: str) -> None:
    _, architecture = architecture_for_platform(target_platform)
    arch_tags = {
        "amd64": {"x86_64", "amd64"},
        "arm64": {"aarch64", "arm64"},
    }[architecture]
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise ValueError("release bundle has no dependency wheels")
    for wheel in wheels:
        parts = wheel.name[:-4].rsplit("-", 1)
        if len(parts) != 2:
            raise ValueError(f"invalid wheel filename: {wheel.name}")
        platforms = parts[1].split(".")
        if "any" in platforms:
            continue
        if not any(
            (
                tag.startswith(("manylinux", "musllinux", "linux"))
                and any(tag.endswith(arch) for arch in arch_tags)
            )
            for tag in platforms
        ):
            raise ValueError(
                f"wheel {wheel.name} is incompatible with {target_platform}"
            )
