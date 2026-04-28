# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Stdio MCP-style server and batch command runner for agent driven Blender automation.

This module intentionally has no third-party dependencies. It speaks a small
JSON-RPC 2.0 subset compatible with MCP-style clients and exposes Blender tools
that agents need for both data-level automation and fine grained UI control.

Start a persistent server from a compiled Blender with:

    blender --agent-mcp

Run a one-shot automation plan from a compiled Blender with:

    blender --background scene.blend --agent-run plan.json
    blender --background --agent-command '{"tool":"create_primitive","params":{"type":"cube"}}'

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


def _as_vector(value: Any, *, length: int, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise AgentMCPError(f"{name} must be a {length}-item list")
    return [float(item) for item in value]


def _tool_list(_: JsonDict) -> JsonDict:
    """List available built-in agent tools."""
    return {"tools": sorted(TOOLS.keys())}


def _tool_status(_: JsonDict) -> JsonDict:
    """Return Blender, file, scene, object, and runtime status."""
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
    """Open a .blend file. Params: filepath."""
    filepath = params.get("filepath")
    if not filepath:
        raise AgentMCPError("open_file requires filepath")
    bpy.ops.wm.open_mainfile(filepath=str(Path(filepath).expanduser()))
    return {"file": bpy.data.filepath}


def _tool_save_file(params: JsonDict) -> JsonDict:
    """Save the current .blend file. Params: optional filepath."""
    filepath = params.get("filepath") or bpy.data.filepath
    if not filepath:
        raise AgentMCPError("save_file requires filepath when the current file has not been saved")
    bpy.ops.wm.save_as_mainfile(filepath=str(Path(filepath).expanduser()))
    return {"file": bpy.data.filepath}


def _tool_select_object(params: JsonDict) -> JsonDict:
    """Select and activate an object. Params: name, optional type."""
    obj = _active_object(params.get("name"), params.get("type"))
    return {"selected": obj.name, "type": obj.type}


def _tool_delete_object(params: JsonDict) -> JsonDict:
    """Delete one object by name or the active object. Params: optional object/name."""
    obj = _active_object(params.get("object") or params.get("name"))
    name = obj.name
    bpy.data.objects.remove(obj, do_unlink=True)
    return {"deleted": name}


def _tool_set_mode(params: JsonDict) -> JsonDict:
    """Switch the active mesh/object mode. Params: mode, optional object."""
    mode = params.get("mode")
    if not mode:
        raise AgentMCPError("set_mode requires mode")
    return _set_mode(mode, params.get("object"))


def _tool_create_primitive(params: JsonDict) -> JsonDict:
    """Create a mesh primitive. Params: type, name, location, rotation, scale, primitive-specific options."""
    primitive = str(params.get("type", "cube")).lower()
    location = _as_vector(params.get("location", (0, 0, 0)), length=3, name="location")
    rotation = _as_vector(params.get("rotation", (0, 0, 0)), length=3, name="rotation")
    scale = _as_vector(params.get("scale", (1, 1, 1)), length=3, name="scale")

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
    elif primitive == "monkey":
        bpy.ops.mesh.primitive_monkey_add(size=params.get("size", 2), **kwargs)
    else:
        raise AgentMCPError(f"Unsupported primitive type {primitive!r}")

    obj = bpy.context.object
    if params.get("name"):
        obj.name = params["name"]
        obj.data.name = f"{obj.name}Mesh"
    return {"object": obj.name, "type": obj.type}


def _tool_transform_object(params: JsonDict) -> JsonDict:
    """Set object transform. Params: object, location, rotation, scale."""
    obj = _active_object(params.get("object"))
    if "location" in params:
        obj.location = _as_vector(params["location"], length=3, name="location")
    if "rotation" in params:
        obj.rotation_euler = _as_vector(params["rotation"], length=3, name="rotation")
    if "scale" in params:
        obj.scale = _as_vector(params["scale"], length=3, name="scale")
    return {"object": obj.name, "location": list(obj.location), "rotation": list(obj.rotation_euler), "scale": list(obj.scale)}


def _tool_add_material(params: JsonDict) -> JsonDict:
    """Create/assign a material. Params: object, name, color [r,g,b,a], roughness, metallic."""
    obj = _active_object(params.get("object"))
    name = params.get("name", "AgentMaterial")
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    material.use_nodes = True
    if "color" in params:
        color = _as_vector(params["color"], length=4, name="color")
        material.diffuse_color = color
        bsdf = material.node_tree.nodes.get("Principled BSDF") if material.node_tree else None
        if bsdf:
            bsdf.inputs["Base Color"].default_value = color
    bsdf = material.node_tree.nodes.get("Principled BSDF") if material.use_nodes and material.node_tree else None
    if bsdf:
        if "roughness" in params:
            bsdf.inputs["Roughness"].default_value = float(params["roughness"])
        if "metallic" in params:
            bsdf.inputs["Metallic"].default_value = float(params["metallic"])
    if material.name not in [slot.material.name for slot in obj.material_slots if slot.material]:
        obj.data.materials.append(material)
    return {"object": obj.name, "material": material.name}


def _tool_add_modifier(params: JsonDict) -> JsonDict:
    """Add a modifier and optional properties. Params: object, type, name, properties."""
    obj = _active_object(params.get("object"))
    mod_type = params.get("type")
    if not mod_type:
        raise AgentMCPError("add_modifier requires type")
    mod = obj.modifiers.new(params.get("name", mod_type.title()), mod_type.upper())
    for key, value in params.get("properties", {}).items():
        setattr(mod, key, value)
    return {"object": obj.name, "modifier": mod.name, "type": mod.type}


def _tool_apply_modifier(params: JsonDict) -> JsonDict:
    """Apply a modifier. Params: object, modifier."""
    obj = _active_object(params.get("object"))
    modifier = params.get("modifier")
    if not modifier:
        raise AgentMCPError("apply_modifier requires modifier")
    bpy.ops.object.modifier_apply(modifier=modifier)
    return {"object": obj.name, "applied": modifier}


def _tool_run_operator(params: JsonDict) -> JsonDict:
    """Run a bpy operator by name. Params: operator like object.shade_smooth, properties."""
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
    """Execute Python with bpy and Path in scope. Params: code."""
    code = params.get("code")
    if not code:
        raise AgentMCPError("python_exec requires code")
    stdout = io.StringIO()
    namespace = {"bpy": bpy, "Path": Path}
    with contextlib.redirect_stdout(stdout):
        exec(code, namespace, namespace)
    return {"stdout": stdout.getvalue()}


def _tool_mouse_move(params: JsonDict) -> JsonDict:
    """Move the mouse cursor in a Blender window. Params: x, y."""
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
    """Click in a Blender window. Params: button, x, y, modifiers."""
    button = str(params.get("button", "LEFTMOUSE")).upper()
    x = int(params.get("x", 0))
    y = int(params.get("y", 0))
    merged = {**params, "x": x, "y": y}
    _simulate_event(button, "PRESS", merged)
    _simulate_event(button, "RELEASE", merged)
    return {"button": button, "x": x, "y": y}


def _tool_mouse_drag(params: JsonDict) -> JsonDict:
    """Drag in a Blender window. Params: button, start/end or x/y/to_x/to_y, steps."""
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
    """Press and release a key in a Blender window. Params: key, modifiers."""
    key = str(params.get("key", "")).upper()
    if not key:
        raise AgentMCPError("key_press requires key")
    _simulate_event(key, "PRESS", params)
    _simulate_event(key, "RELEASE", params)
    return {"key": key}


def _tool_type_text(params: JsonDict) -> JsonDict:
    """Type text into a Blender window. Params: text."""
    text = params.get("text", "")
    for char in text:
        event_type = char.upper() if char.isalpha() else "TEXTINPUT"
        _simulate_event(event_type, "PRESS", {**params, "unicode": char})
        _simulate_event(event_type, "RELEASE", {**params, "unicode": char})
    return {"typed": text}


def _tool_screenshot(params: JsonDict) -> JsonDict:
    """Save a screenshot from the current Blender screen. Params: filepath, full."""
    filepath = params.get("filepath")
    if not filepath:
        raise AgentMCPError("screenshot requires filepath")
    bpy.ops.screen.screenshot(filepath=str(Path(filepath).expanduser()), full=params.get("full", True))
    return {"filepath": filepath}


def _tool_import_file(params: JsonDict) -> JsonDict:
    """Import a model file. Params: filepath, optional format obj/fbx/gltf/glb."""
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
    """Export a model file. Params: filepath, optional format obj/fbx/gltf/glb."""
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
    "delete_object": _tool_delete_object,
    "set_mode": _tool_set_mode,
    "create_primitive": _tool_create_primitive,
    "transform_object": _tool_transform_object,
    "add_material": _tool_add_material,
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


def call_tool(name: str, params: JsonDict | None = None) -> JsonDict:
    """Call one built-in tool by name."""
    if name not in TOOLS:
        raise AgentMCPError(f"Unknown tool/method {name!r}")
    return TOOLS[name](params or {})


def _normalize_command(command: Any) -> tuple[str, JsonDict]:
    if isinstance(command, str):
        return command, {}
    if not isinstance(command, dict):
        raise AgentMCPError("Each agent command must be a string or object")
    name = command.get("tool") or command.get("name") or command.get("method")
    if not name:
        raise AgentMCPError("Agent command object requires tool/name/method")
    params = command.get("params", command.get("arguments", {})) or {}
    if not isinstance(params, dict):
        raise AgentMCPError("Agent command params/arguments must be an object")
    return str(name), params


def run_plan(plan: Any) -> JsonDict:
    """Run a one-shot agent automation plan and return structured results."""
    if isinstance(plan, list):
        commands = plan
        save_as = None
    elif isinstance(plan, dict):
        if "tool" in plan or "name" in plan or "method" in plan:
            commands = [plan]
        else:
            commands = plan.get("commands") or plan.get("steps") or []
        save_as = plan.get("save_as") or plan.get("output") if isinstance(plan, dict) else None
    else:
        raise AgentMCPError("Agent plan must be a command object, command array, or object with commands/steps")

    if not isinstance(commands, list):
        raise AgentMCPError("Agent plan commands/steps must be an array")

    results = []
    for index, command in enumerate(commands):
        name, params = _normalize_command(command)
        result = call_tool(name, params)
        results.append({"index": index, "tool": name, "result": result})

    if save_as:
        results.append({"index": len(results), "tool": "save_file", "result": call_tool("save_file", {"filepath": save_as})})

    return {"ok": True, "results": results, "status": call_tool("status", {})}


def run_plan_file(filepath: str) -> JsonDict:
    """Load and run an agent automation JSON plan file."""
    plan_path = Path(filepath).expanduser()
    with plan_path.open("r", encoding="utf-8") as handle:
        plan = json.load(handle)
    result = run_plan(plan)
    result["plan_file"] = str(plan_path)
    return result


def _write_json(value: JsonDict) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _write_response(response: JsonDict) -> None:
    _write_json(response)


def _handle_request(request: JsonDict) -> JsonDict | None:
    if request.get("jsonrpc") != "2.0":
        raise AgentMCPError("Expected JSON-RPC 2.0 request")
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    # MCP-style wrappers.
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"serverInfo": {"name": "blender-agent-mcp", "version": "0.2"}, "capabilities": {"tools": {}}}}
    if method == "tools/list":
        tools = [{"name": name, "description": TOOLS[name].__doc__ or ""} for name in sorted(TOOLS.keys())]
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
    else:
        name = method
        arguments = params

    result = call_tool(name, arguments)
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
    parser = argparse.ArgumentParser(description="Run Blender's built-in agent automation entrypoint")
    parser.add_argument("--run", metavar="PLAN_JSON", help="Run a JSON automation plan once, print JSON result, and exit")
    parser.add_argument("--command", metavar="COMMAND_JSON", help="Run a single JSON command/plan once, print JSON result, and exit")
    args = parser.parse_args(argv)

    try:
        if args.run:
            _write_json(run_plan_file(args.run))
            return 0
        if args.command:
            _write_json(run_plan(json.loads(args.command)))
            return 0
        return serve()
    except Exception as ex:
        _write_json({"ok": False, "error": str(ex), "traceback": traceback.format_exc()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
