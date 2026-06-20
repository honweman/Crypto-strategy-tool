from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    ADMIN = "admin"
    TRADER = "trader"
    ANALYST = "analyst"
    VIEWER = "viewer"


VIEW_DASHBOARD = "dashboard:view"
VIEW_BACKTESTS = "backtests:view"
RUN_BACKTESTS = "backtests:run"
MANAGE_OWN_API_KEYS = "api_keys:manage_self"
MANAGE_ALL_API_KEYS = "api_keys:manage_all"
VIEW_STRATEGIES = "strategies:view"
MANAGE_OWN_STRATEGIES = "strategies:manage_self"
MANAGE_ALL_STRATEGIES = "strategies:manage_all"
VIEW_MONITORING = "monitoring:view"
VIEW_OWN_TRADES = "trades:view_self"
VIEW_ALL_TRADES = "trades:view_all"
EXECUTE_PAPER_TRADES = "trading:execute_paper"
EXECUTE_LIVE_TRADES = "trading:execute_live"
MANAGE_USERS = "users:manage"
MANAGE_SYSTEM = "system:manage"


ROLE_PERMISSIONS: dict[str, set[str]] = {
    Role.VIEWER.value: {
        VIEW_DASHBOARD,
        VIEW_BACKTESTS,
        VIEW_STRATEGIES,
        VIEW_MONITORING,
        VIEW_OWN_TRADES,
    },
    Role.ANALYST.value: {
        VIEW_DASHBOARD,
        VIEW_BACKTESTS,
        RUN_BACKTESTS,
        VIEW_STRATEGIES,
        MANAGE_OWN_STRATEGIES,
        VIEW_MONITORING,
        VIEW_OWN_TRADES,
    },
    Role.TRADER.value: {
        VIEW_DASHBOARD,
        VIEW_BACKTESTS,
        RUN_BACKTESTS,
        MANAGE_OWN_API_KEYS,
        VIEW_STRATEGIES,
        MANAGE_OWN_STRATEGIES,
        VIEW_MONITORING,
        VIEW_OWN_TRADES,
        EXECUTE_PAPER_TRADES,
    },
    Role.ADMIN.value: {
        VIEW_DASHBOARD,
        VIEW_BACKTESTS,
        RUN_BACKTESTS,
        MANAGE_OWN_API_KEYS,
        MANAGE_ALL_API_KEYS,
        VIEW_STRATEGIES,
        MANAGE_OWN_STRATEGIES,
        MANAGE_ALL_STRATEGIES,
        VIEW_MONITORING,
        VIEW_OWN_TRADES,
        VIEW_ALL_TRADES,
        EXECUTE_PAPER_TRADES,
        EXECUTE_LIVE_TRADES,
        MANAGE_USERS,
        MANAGE_SYSTEM,
    },
}

ALL_PERMISSIONS: set[str] = set().union(*ROLE_PERMISSIONS.values())


def normalize_role(role: str) -> str:
    normalized = role.strip().lower()
    if normalized not in ROLE_PERMISSIONS:
        raise ValueError(f"unknown role: {role}")
    return normalized


def normalize_permissions(permissions: list[str] | set[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for permission in permissions or []:
        value = str(permission).strip()
        if value not in ALL_PERMISSIONS:
            raise ValueError(f"unknown permission: {permission}")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized


def permissions_for_role(role: str) -> set[str]:
    return set(ROLE_PERMISSIONS.get(role, set()))


def permissions_for_user(
    role: str,
    grants: list[str] | set[str] | tuple[str, ...] | None = None,
    revokes: list[str] | set[str] | tuple[str, ...] | None = None,
) -> set[str]:
    granted = set(normalize_permissions(grants))
    revoked = set(normalize_permissions(revokes))
    return (permissions_for_role(role) | granted) - revoked


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())


def has_user_permission(user: dict, permission: str) -> bool:
    return permission in permissions_for_user(
        user.get("role", ""),
        user.get("permission_grants", []),
        user.get("permission_revokes", []),
    )
