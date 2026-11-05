"""Load the test blend or create it once, then attach a local isolated bridge."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import bpy

STUDIO = Path(os.environ.get("AI_STUDIO_ROOT", Path(__file__).resolve().parents[2])).resolve()
WORK = STUDIO / "private"
RUNTIME = Path(os.environ.get("AI_STUDIO_RUNTIME_PRIVATE_DIR", WORK)).absolute()
assert RUNTIME.resolve() == RUNTIME and RUNTIME.is_relative_to(WORK), "The private runtime must stay inside this release data directory."
assert not any(part.casefold() == "outputs" for part in RUNTIME.parts), "A public release package must never hold runtime credentials or profiles."
JOB = os.environ.get("AI_STUDIO_BLENDER_JOB", "demo-001")
PORT = int(os.environ.get("AI_STUDIO_BLENDER_PORT", "9877"))
ADDON = Path(os.environ.get("AI_STUDIO_BLENDER_ADDON", str(Path(os.environ["APPDATA"]) / "Blender Foundation" / "Blender" / "5.2" / "scripts" / "addons" / "blender_mcp.py")))
JOB_PATH = Path(os.environ.get("AI_STUDIO_JOB_PATH", STUDIO / "jobs" / JOB)).absolute()
assert JOB_PATH.resolve() == JOB_PATH and JOB_PATH.is_relative_to(STUDIO / "jobs"), "The Blender member workspace must be inside this isolated studio."
SOURCE = JOB_PATH / "art" / "source.blend"
STATE = RUNTIME / f"blender-{PORT}-state.json"
existed = SOURCE.exists()

profile = RUNTIME / "blender-profiles" / str(PORT)
for variable, subdirectory in (
    ("BLENDER_USER_CONFIG", "config"),
    ("BLENDER_USER_SCRIPTS", "scripts"),
    ("BLENDER_USER_EXTENSIONS", "extensions"),
    ("BLENDER_USER_DATAFILES", "datafiles"),
):
    directory = Path(os.environ.get(variable, "")).resolve()
    assert directory == (profile / subdirectory).resolve(), f"{variable} must point inside this release's isolated private profile."

if existed:
    assert bpy.data.filepath and Path(bpy.data.filepath).resolve() == SOURCE.resolve(), "The existing test blend must be opened by the launcher."
    assert bpy.context.scene.get("ai_studio_isolated_test") is True, "Refusing to attach to a blend without the isolation marker."
    assert bpy.context.scene.get("ai_studio_job") == JOB, "The blend belongs to another test job."
else:
    assert not bpy.data.filepath, "A new test requires factory startup."

os.environ["BLENDERMCP_NO_UPDATE_CHECK"] = "1"
spec = importlib.util.spec_from_file_location("blender_mcp", str(ADDON))
addon = importlib.util.module_from_spec(spec)
sys.modules["blender_mcp"] = addon
spec.loader.exec_module(addon)
addon.register()

scene = bpy.context.scene
scene.blendermcp_auto_start_server = False
scene.blendermcp_port = PORT
for integration in ("polyhaven", "hyper3d", "hunyuan3d", "sketchfab", "polypizza", "tripo"):
    field = "blendermcp_use_" + integration
    if hasattr(scene, field):
        setattr(scene, field, False)

if not existed:
    scene.name = "AI_STUDIO_TEST_" + JOB.replace("-", "_")
    scene["ai_studio_isolated_test"] = True
    scene["ai_studio_job"] = JOB
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

server = addon.BlenderMCPServer(host="127.0.0.1", port=PORT)
bpy.types.blendermcp_server = server
server.start()
assert server.running, "The isolated Blender bridge failed to start."
scene.blendermcp_server_running = True

if not existed:
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(SOURCE), check_existing=False)

state = {
    "pid": os.getpid(), "host": "127.0.0.1", "port": PORT,
    "source": str(SOURCE), "job": JOB, "opened_existing": existed,
    "scene": scene.name, "object_count": len(scene.objects),
    "addon": str(ADDON), "protocol_version": addon.ADDON_PROTOCOL_VERSION,
    "blender_version": bpy.app.version_string, "ready": True,
    "user_config": os.environ.get("BLENDER_USER_CONFIG"),
}
STATE.parent.mkdir(parents=True, exist_ok=True)
STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
print("AI_STUDIO_ISOLATED_READY " + json.dumps(state, ensure_ascii=False), flush=True)
