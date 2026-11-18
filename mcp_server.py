"""
MCP server (stdio) for managing Roblox developer products and game passes, and uploading images.
Cookie: ROBLOSECURITY env variable or cookie.txt next to the script.

Register in Claude Code:
    claude mcp add --scope user roblox-monetization -- python "C:/path/to/mcp_server.py"

The Roblox API can't delete developer products or game passes - disable_item takes them off sale instead.
"""

import contextlib
import functools
import io
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from copy_devproducts import KINDS, Roblox, Abort, copy_kind, grant_permissions, load_cookie, price_of, bool_str
from copy_devproducts import ANIM_DIR, RECEIVER_PORT, studio_script
from copy_devproducts import start_animation_receiver as start_receiver_server
from copy_devproducts import upload_animations as upload_animation_files
from copy_devproducts import upload_images as upload_image_files
from i18n import t

Kind = Literal["dev", "pass"]

server = MCPServer(
    "roblox-monetization",
    instructions=(
        "Manages Roblox developer products (kind='dev') and game passes (kind='pass'). "
        "universe_id is the experience's universe ID (from the Creator Dashboard URL), not the place ID. "
        "Roblox doesn't allow deleting - disable_item takes an item off sale. "
        "Before calling copy_items with dry_run=False, show the user the dry_run=True result first. "
        "upload_images uploads image files as decals; use the returned imageId (rbxassetid://<imageId>) "
        "for ImageLabels / renderImage, not the decalId. "
        "grant_asset_permissions lets an experience use assets owned by another group or user (animations, "
        "meshes, sounds); it needs edit rights on those assets. "
        "Animations from Studio (e.g. a model's AnimSaves): call start_animation_receiver, run the returned "
        "studio_script in Studio (Roblox Studio MCP execute_luau, or the command bar), then upload_animations. "
        "Upload animations to the group that owns the game (group_id), or they won't play in it."
    ),
)

_rbx: Roblox | None = None


def rbx() -> Roblox:
    global _rbx
    if _rbx is None:
        _rbx = Roblox(load_cookie(prompt=False))
    return _rbx


def tool(fn):
    # Roblox API errors / missing cookie should reach the model with their message, not as a generic crash
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (Abort, RuntimeError, OSError) as e:
            raise ToolError(str(e)) from e
    return server.tool()(wrapper)


def summary(kind: Kind, p: dict) -> dict:
    return {
        "id": p[KINDS[kind]["id_key"]],
        "name": p["name"],
        "price": price_of(p),
        "isForSale": p.get("isForSale"),
        "isManagedPricingEnabled": p.get("isManagedPricingEnabled"),
        "description": p.get("description") or "",
        "iconAssetId": p.get(KINDS[kind]["icon_key"]),
    }


def read_icon(icon_path: str | None) -> bytes | None:
    return Path(icon_path).read_bytes() if icon_path else None


@tool
def list_items(kind: Kind, universe_id: int) -> list[dict]:
    """Lists developer products ('dev') or game passes ('pass') in an experience: id, name, price, for sale,
    managed pricing."""
    return [summary(kind, p) for p in rbx().list_items(KINDS[kind], universe_id)]


@tool
def create_item(kind: Kind, universe_id: int, name: str, price: int, description: str = "",
                is_for_sale: bool = True, managed_pricing: bool = False,
                icon_path: str | None = None) -> dict:
    """Creates a developer product ('dev') or game pass ('pass'). icon_path: optional local PNG/JPG file."""
    fields = {
        "name": name,
        "description": description,
        "price": str(price),
        "isForSale": bool_str(is_for_sale),
        "isManagedPricingEnabled": bool_str(managed_pricing),
    }
    return summary(kind, rbx().create_item(KINDS[kind], universe_id, fields, read_icon(icon_path)))


@tool
def update_item(kind: Kind, universe_id: int, item_id: int, name: str | None = None,
                description: str | None = None, price: int | None = None,
                is_for_sale: bool | None = None, managed_pricing: bool | None = None,
                icon_path: str | None = None) -> str:
    """Changes selected fields of a developer product / game pass. Omitted fields stay unchanged."""
    fields = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if price is not None:
        fields["price"] = str(price)
    if is_for_sale is not None:
        fields["isForSale"] = bool_str(is_for_sale)
    if managed_pricing is not None:
        fields["isManagedPricingEnabled"] = bool_str(managed_pricing)
    if not fields and not icon_path:
        return "Nothing to change."
    rbx().update_item(KINDS[kind], universe_id, item_id, fields, read_icon(icon_path))
    return f"Updated {item_id}: {', '.join(fields) or 'icon'}"


@tool
def disable_item(kind: Kind, universe_id: int, item_id: int) -> str:
    """'Deletes' a developer product / game pass by taking it off sale (the Roblox API has no real delete)."""
    rbx().update_item(KINDS[kind], universe_id, item_id, {"isForSale": "false"})
    return f"{item_id} taken off sale."


@tool
def copy_items(kind: Literal["dev", "pass", "all"], source_universe_id: int, target_universe_id: int,
               dry_run: bool = True, copy_icons: bool = False) -> str:
    """Copies / syncs developer products and/or game passes from source to target (matched by name): creates
    missing ones and updates differing price/description/for sale/managed pricing. Returns the log and the
    old ID -> new ID mapping."""
    if source_universe_id == target_universe_id:
        raise Abort(t("same_game"))
    log = io.StringIO()
    # stdout is the MCP protocol channel - capture copy_kind's log into a string
    with contextlib.redirect_stdout(log):
        for key in (["dev", "pass"] if kind == "all" else [kind]):
            copy_kind(rbx(), key, source_universe_id, target_universe_id, dry_run, copy_icons,
                      delay=1.0, interactive=False)
    return log.getvalue()


@tool
def upload_images(paths: list[str], group_id: int | None = None) -> list[dict]:
    """Uploads image files (png/jpg/bmp/tga) as decals - give files or folders (a folder's images, not its
    subfolders). Returns, per file: name (file name without extension), decalId and imageId. Use imageId as
    rbxassetid://<imageId> for ImageLabel.Image / renderImage. group_id uploads to a group instead of the
    cookie's account. A mapping is also saved to lists/images_<time>.json / .lua."""
    log = io.StringIO()
    # stdout is the MCP protocol channel - capture the progress log
    with contextlib.redirect_stdout(log):
        return upload_image_files(rbx(), paths, group_id)


@tool
def start_animation_receiver(root: str = "game.ReplicatedStorage", port: int = RECEIVER_PORT) -> dict:
    """Starts a local receiver (127.0.0.1) for KeyframeSequences from Roblox Studio and returns the Luau to run
    there. root is the Studio instance to send from (every KeyframeSequence under it, e.g.
    game.ReplicatedStorage.Assets.Generals); files are saved as <Model>__<Sequence>.rbxm in the returned folder.
    Studio must be allowed to reach 127.0.0.1: the Studio MCP's HTTP allowlist for execute_luau, or Game Settings
    -> Security -> Allow HTTP Requests for the command bar. Then call upload_animations."""
    start_receiver_server(port)
    return {"url": f"http://127.0.0.1:{port}/", "folder": str(ANIM_DIR), "studio_script": studio_script(root, port)}


@tool
def upload_animations(paths: list[str] | None = None, group_id: int | None = None) -> list[dict]:
    """Uploads KeyframeSequences saved as .rbxm / .rbxmx (files or folders; default: the receiver's folder) as
    Animation assets. Returns, per file: name and animationId (rbxassetid://<animationId> for
    Animation.AnimationId). group_id: the group that owns the game - an animation plays only in its owner's
    experiences. A mapping is also saved to lists/animations_<time>.json / .lua."""
    log = io.StringIO()
    # stdout is the MCP protocol channel - capture the progress log
    with contextlib.redirect_stdout(log):
        return upload_animation_files(rbx(), paths or [], group_id)


@tool
def grant_asset_permissions(subject_id: int, asset_ids: list[str],
                            subject_type: Literal["universe", "group", "user"] = "universe",
                            dry_run: bool = False) -> dict:
    """Grants the 'Use' permission on assets (animations, meshes, sounds, ...) to an experience, so it can use
    assets owned by another group or user - the fix for 'Requester does not have permission for Asset'.
    subject_id is the universe ID (or group / user ID for subject_type). asset_ids: asset IDs as strings, or
    text / Lua files and folders to read IDs from. Returns the IDs that succeeded and the errors."""
    log = io.StringIO()
    # stdout is the MCP protocol channel - capture the progress log
    with contextlib.redirect_stdout(log):
        return grant_permissions(rbx(), subject_type, subject_id, asset_ids, dry_run=dry_run)


if __name__ == "__main__":
    server.run()
