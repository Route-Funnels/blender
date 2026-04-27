# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Minimal stdio MCP-style server for agent driven Blender automation.

This module intentionally has no third-party dependencies. It speaks a small
JSON-RPC 2.0 subset compatible with MCP-style clients and exposes Blender tools
that agents need for both data-level automation and fine grained UI control.

Start from a compiled Blender with:

    blender --agent-mcp

For UI event tools, start Blender with a window and enable event simulation:

    blender --enable-event-simulate --agent-mcp

Background mode is useful for data tools, but mouse/key simulation requires a
window and will fail cleanly when no window is available.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

import bpy

JsonDict = dict[str, Any]


class AgentMCPError(RuntimeError):
    """Error reported to the JSON-RPC client."""


def _active_window():
    window = bpy.context.window
    if window is None and bpy.context.window_manager.windows:
        window = bpy.context.window_manager.windows[0]
    return window


def _active_object(name: str | None = None, required_type: str | None = None):
    obj = bpy.data.objects.get(name) if name else bpy.context.view_layer.objects.active
    if obj is None and required_type:
        obj = next((candidate for candidate in bpy.context.scene.objects if candidate.type == required_type), None)
    if obj is None:
        raise AgentMCPError("No active object; pass an object name or create/select an object first")
    if required_type and obj.type != required_type:
        raise AgentMCPError(f"Object {obj.name!r} has type {obj.type!r}, expected {required_type!r}")
    bpy.ops.object.mode_set(mode="OBJECT") if bpy.ops.object.mode_set.poll() else None
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return obj


def _set_mode(mode: str, object_name: str | None = None) -> JsonDict:
    mode = mode.upper()
    required_type = "MESH" if mode in {"EDIT", "SCULPT"} else None
    obj = _active_object(object_name, required_type=required_type)
    bpy.ops.object.mode_set(mode=mode)
    return {"object": obj.name, "mode": bpy.context.object.mode}


def _tool_list(_: JsonDict) -> JsonDict:
    return {"tools": sorted(TOOLS.keys())}


def _tool_status(_: JsonDict) -> JsonDict:
    obj = bpy.context.object
    window = _active_window()
    return {
        "blender_version": bpy.app.version_string,
        "background": bpy.app.background,
        "event_simulate_enabled": bool(getattr(bpy.app, "use_event_simulate", False)),
        "has_window": window is not None,
        "file": bpy.data.filepath,
        "scene": bpy.context.scene.name if bpy.context.scene else None,
        "active_object": obj.name if obj else None,
        "active_object_type": obj.type if obj else None,
        "mode": obj.mode if obj else None,
        "objects": [obj.name for obj in bpy.context.scene.objects],
    }


def _tool_open_file(params: JsonDict) -> JsonDict:
    filepath = params.get("filepath")
    if not filepath:
        raise AgentMCPError("open_file requires filepath")
    bpy.ops.wm.open_mainfile(filepath=str(Path(filepath).expanduser()))
    return {"file": bpy.data.filepath}


def _tool_save_file(params: JsonDict) -> JsonDict:
    filepath = params.get("filepath") or bpy.data.filepath
    if not filepath:
        raise AgentMCPError("save_file requires filepath when the current file has not been saved")
    bpy.ops.wm.save_as_mainfile(filepath=str(Path(filepath).expanduser()))
    return {"file": bpy.data.filepath}


def _tool_select_object(params: JsonDict) -> JsonDict:
    obj = _active_object(params.get("name"), params.get("type"))
    return {"selected": obj.name, "type": obj.type}


def _tool_set_mode(params: JsonDict) -> JsonDict:
    mode = params.get("mode")
    if not mode:
        raise AgentMCPError("set_mode requires mode")
    return _set_mode(mode, params.get("object"))


def _tool_create_primitive(params: JsonDict) -> JsonDict:
    primitive = str(params.get("type", "cube")).lower()
    location = params.get("location", (0, 0, 0))
    rotation = params.get("rotation", (0, 0, 0))
    scale = params.get("scale", (1, 1, 1))

    kwargs = {"location": location, "rotation": rotation, "scale": scale}
    if primitive == "cube":
        bpy.ops.mesh.primitive_cube_add(size=params.get("size", 2), **kwargs)
    elif primitive in {"uv_sphere", "sphere"}:
        bpy.ops.mesh.primitive_uv_sphere_add(
            segments=params.get("segments", 64),
            ring_count=params.get("ring_count", 32),
            radius=params.get("radius", 1),
            **kwargs,
        )
    elif primitive == "ico_sphere":
        bpy.ops.mesh.primitive_ico_sphere_add(
            subdivisions=params.get("subdivisions", 3), radius=params.get("radius", 1), **kwargs
        )
    elif primitive == "plane":
        bpy.ops.mesh.primitive_plane_add(size=params.get("size", 2), **kwargs)
    elif primitive == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=params.get("vertices", 32), radius=params.get("radius", 1), depth=params.get("depth", 2), **kwargs
        )
    elif primitive == "cone":
        bpy.ops.mesh.primitive_cone_add(
            vertices=params.get("vertices", 32),
            radius1=params.get("radius1", 1),
            radius2=params.get("radius2", 0),
            depth=params.get("depth", 2),
            **kwargs,
        )
    else:
        raise AgentMCPError(f"Unsupported primitive type {primitive!r}")

    obj = bpy.context.object
    if params.get("name"):
        obj.name = params["name"]
        obj.data.name = f"{obj.name}Mesh"
    return {"object": obj.name, "type": obj.type}


def _tool_transform_object(params: JsonDict) -> JsonDict:
    obj = _active_object(params.get("object"))
    if "location" in params:
        obj.location = params["location"]
    if "rotation" in params:
        obj.rotation_euler = params["rotation"]
    if "scale" in params:
        obj.scale = params["scale"]
    return {"object": obj.name, "location": list(obj.location), "rotation": list(obj.rotation_euler), "scale": list(obj.scale)}


def _tool_add_modifier(params: JsonDict) -> JsonDict:
    obj = _active_object(params.get("object"))
    mod_type = params.get("type")
    if not mod_type:
        raise AgentMCPError("add_modifier requires type")
    mod = obj.modifiers.new(params.get("name", mod_type.title()), mod_type.upper())
    for key, value in params.get("properties", {}).items():
        setattr(mod, key, value)
    return {"object": obj.name, "modifier": mod.name, "type": mod.type}


def _tool_apply_modifier(params: JsonDict) -> JsonDict:
    obj = _active_object(params.get("object"))
    modifier = params.get("modifier")
    if not modifier:
        raise AgentMCPError("apply_modifier requires modifier")
    bpy.ops.object.modifier_apply(modifier=modifier)
    return {"object": obj.name, "applied": modifier}


def _tool_run_operator(params: JsonDict) -> JsonDict:
    op_name = params.get("operator")
    if not op_name or "." not in op_name:
        raise AgentMCPError("run_operator requires an operator like 'object.shade_smooth'")
    module_name, attr_name = op_name.split(".", 1)
    module = getattr(bpy.ops, module_name, None)
    op = getattr(module, attr_name, None) if module else None
    if op is None:
        raise AgentMCPError(f"Unknown operator {op_name!r}")
    result = op(**params.get("properties", {}))
    return {"operator": op_name, "result": list(result) if isinstance(result, set) else str(result)}


def _tool_python_exec(params: JsonDict) -> JsonDict:
    code = params.get("code")
    if not code:
        raise AgentMCPError("python_exec requires code")
    stdout = io.StringIO()
    namespace = {"bpy": bpy, "Path": Path}
    with contextlib.redirect_stdout(stdout):
        exec(code, namespace, namespace)
    return {"stdout": stdout.getvalue()}


def _tool_mouse_move(params: JsonDict) -> JsonDict:
    window = _active_window()
    if window is None:
        raise AgentMCPError("mouse_move requires a Blender window; background mode has no mouse")
    x = int(params.get("x", 0))
    y = int(params.get("y", 0))
    window.cursor_warp(x, y)
    return {"x": x, "y": y}


def _simulate_event(event_type: str, value: str, params: JsonDict):
    window = _active_window()
    if window is None:
        raise AgentMCPError("event simulation requires a Blender window")
    if not getattr(bpy.app, "use_event_simulate", False):
        raise AgentMCPError("event simulation is disabled; start Blender with --enable-event-simulate")
    return window.event_simulate(
        type=event_type,
        value=value,
        unicode=params.get("unicode"),
        x=int(params.get("x", 0)),
        y=int(params.get("y", 0)),
        shift=bool(params.get("shift", False)),
        ctrl=bool(params.get("ctrl", False)),
        alt=bool(params.get("alt", False)),
        oskey=bool(params.get("oskey", False)),
        hyper=bool(params.get("hyper", False)),
    )


def _tool_mouse_click(params: JsonDict) -> JsonDict:
    button = str(params.get("button", "LEFTMOUSE")).upper()
    x = int(params.get("x", 0))
    y = int(params.get("y", 0))
    merged = {**params, "x": x, "y": y}
    _simulate_event(button, "PRESS", merged)
    _simulate_event(button, "RELEASE", merged)
    return {"button": button, "x": x, "y": y}


def _tool_mouse_drag(params: JsonDict) -> JsonDict:
    button = str(params.get("button", "LEFTMOUSE")).upper()
    start = params.get("start", [params.get("x", 0), params.get("y", 0)])
    end = params.get("end", [params.get("to_x", 0), params.get("to_y", 0)])
    steps = max(1, int(params.get("steps", 8)))
    _simulate_event(button, "PRESS", {**params, "x": start[0], "y": start[1]})
    for step in range(1, steps + 1):
        t = step / steps
        x = round(start[0] + (end[0] - start[0]) * t)
        y = round(start[1] + (end[1] - start[1]) * t)
        _simulate_event("MOUSEMOVE", "NOTHING", {**params, "x": x, "y": y})
    _simulate_event(button, "RELEASE", {**params, "x": end[0], "y": end[1]})
    return {"button": button, "start": start, "end": end, "steps": steps}


def _tool_key_press(params: JsonDict) -> JsonDict:
    key = str(params.get("key", "")).upper()
    if not key:
        raise AgentMCPError("key_press requires key")
    _simulate_event(key, "PRESS", params)
    _simulate_event(key, "RELEASE", params)
    return {"key": key}


def _tool_type_text(params: JsonDict) -> JsonDict:
    text = params.get("text", "")
    for char in text:
        event_type = char.upper() if char.isalpha() else "TEXTINPUT"
        _simulate_event(event_type, "PRESS", {**params, "unicode": char})
        _simulate_event(event_type, "RELEASE", {**params, "unicode": char})
    return {"typed": text}


def _tool_screenshot(params: JsonDict) -> JsonDict:
    filepath = params.get("filepath")
    if not filepath:
        raise AgentMCPError("screenshot requires filepath")
    bpy.ops.screen.screenshot(filepath=str(Path(filepath).expanduser()), full=params.get("full", True))
    return {"filepath": filepath}


def _tool_import_file(params: JsonDict) -> JsonDict:
    filepath = params.get("filepath")
    fmt = str(params.get("format", Path(filepath or "").suffix.lstrip(".")).lower())
    if not filepath:
        raise AgentMCPError("import_file requires filepath")
    if fmt in {"obj"}:
        bpy.ops.wm.obj_import(filepath=filepath)
    elif fmt in {"fbx"}:
        bpy.ops.import_scene.fbx(filepath=filepath)
    elif fmt in {"gltf", "glb"}:
        bpy.ops.import_scene.gltf(filepath=filepath)
    else:
        raise AgentMCPError(f"Unsupported import format {fmt!r}")
    return {"imported": filepath}


def _tool_export_file(params: JsonDict) -> JsonDict:
    filepath = params.get("filepath")
    fmt = str(params.get("format", Path(filepath or "").suffix.lstrip(".")).lower())
    if not filepath:
        raise AgentMCPError("export_file requires filepath")
    if fmt in {"obj"}:
        bpy.ops.wm.obj_export(filepath=filepath)
    elif fmt in {"fbx"}:
        bpy.ops.export_scene.fbx(filepath=filepath)
    elif fmt in {"gltf", "glb"}:
        bpy.ops.export_scene.gltf(filepath=filepath)
    else:
        raise AgentMCPError(f"Unsupported export format {fmt!r}")
    return {"exported": filepath}


TOOLS: dict[str, Callable[[JsonDict], JsonDict]] = {
    "list_tools": _tool_list,
    "status": _tool_status,
    "open_file": _tool_open_file,
    "save_file": _tool_save_file,
    "select_object": _tool_select_object,
    "set_mode": _tool_set_mode,
    "create_primitive": _tool_create_primitive,
    "transform_object": _tool_transform_object,
    "add_modifier": _tool_add_modifier,
    "apply_modifier": _tool_apply_modifier,
    "run_operator": _tool_run_operator,
    "python_exec": _tool_python_exec,
    "mouse_move": _tool_mouse_move,
    "mouse_click": _tool_mouse_click,
    "mouse_drag": _tool_mouse_drag,
    "key_press": _tool_key_press,
    "type_text": _tool_type_text,
    "screenshot": _tool_screenshot,
    "import_file": _tool_import_file,
    "export_file": _tool_export_file,
}


def _write_response(response: JsonDict) -> None:
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _handle_request(request: JsonDict) -> JsonDict | None:
    if request.get("jsonrpc") != "2.0":
        raise AgentMCPError("Expected JSON-RPC 2.0 request")
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    # MCP-style wrappers.
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"serverInfo": {"name": "blender-agent-mcp", "version": "0.1"}, "capabilities": {"tools": {}}}}
    if method == "tools/list":
        tools = [{"name": name, "description": TOOLS[name].__doc__ or ""} for name in sorted(TOOLS.keys())]
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
    else:
        name = method
        arguments = params

    if name not in TOOLS:
        raise AgentMCPError(f"Unknown tool/method {name!r}")
    result = TOOLS[name](arguments)
    if method == "tools/call":
        result = {"content": [{"type": "text", "text": json.dumps(result)}]}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve() -> int:
    while True:
        line = sys.stdin.readline()
        if line == "":
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = _handle_request(request)
        except Exception as ex:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32000,
                    "message": str(ex),
                    "data": traceback.format_exc(),
                },
            }
        if response is not None:
            _write_response(response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Blender's built-in agent MCP stdio server")
    parser.parse_args(argv)
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
