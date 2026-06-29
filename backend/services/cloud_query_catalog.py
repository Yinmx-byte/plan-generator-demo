"""Config-driven catalog for cloud query tools."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "cloud_query_catalog.yaml"


class CloudQueryCatalogError(ValueError):
    """Raised when a requested product or metric cannot be resolved."""


def _norm(value: str | None) -> str:
    return (value or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")


@lru_cache(maxsize=1)
def load_cloud_query_catalog() -> dict[str, Any]:
    with CATALOG_PATH.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    data.setdefault("defaults", {})
    data.setdefault("products", {})
    return data


def get_catalog_defaults() -> dict[str, Any]:
    return dict(load_cloud_query_catalog().get("defaults", {}))


def resolve_product(product: str = "ecs") -> tuple[str, dict[str, Any]]:
    """Resolve a product key from code, display name, or aliases."""
    catalog = load_cloud_query_catalog()
    products = catalog.get("products", {})
    wanted = _norm(product) or "ecs"
    for key, config in products.items():
        candidates = [
            key,
            config.get("product_code", ""),
            config.get("product_name", ""),
            *(config.get("aliases") or []),
        ]
        if wanted in {_norm(item) for item in candidates}:
            return key, dict(config)
    raise CloudQueryCatalogError(f"Unsupported cloud product: {product}")


def resolve_metric(product: str, metric: str = "cpu_usage") -> tuple[str, dict[str, Any]]:
    """Resolve a product metric from a stable key, raw metric name, or aliases."""
    product_key, product_config = resolve_product(product)
    metrics = product_config.get("metrics") or {}
    wanted = _norm(metric) or "cpuusage"
    for key, config in metrics.items():
        candidates = [
            key,
            config.get("metric_name", ""),
            config.get("display_name", ""),
            *(config.get("aliases") or []),
        ]
        if wanted in {_norm(item) for item in candidates}:
            return key, dict(config)
    raise CloudQueryCatalogError(f"Unsupported metric for {product_key}: {metric}")


def product_name_for_code(product_code: str) -> str:
    """Return configured product display name for a Resource Center product code."""
    normalized = _norm(product_code)
    for _key, config in load_cloud_query_catalog().get("products", {}).items():
        if normalized == _norm(config.get("product_code", "")):
            return str(config.get("product_name") or product_code)
    return product_code


def product_code_from_resource_type(resource_type: str) -> str:
    """Extract product code from Resource Center resource type, e.g. ACS::ECS::Instance."""
    parts = [part for part in (resource_type or "").split("::") if part]
    if len(parts) >= 2 and parts[0].upper() == "ACS":
        return parts[1].upper()
    return "UNKNOWN"
