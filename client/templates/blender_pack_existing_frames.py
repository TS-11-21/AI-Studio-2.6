# Minimal repair only: no rerender, object edits, game edits, marker changes or external .blend load.
# Bind exact current owned source.blend and actual bootstrap alias before passing to the guarded MCP.
import bpy
EXPECTED_SOURCE = __EXPECTED_SOURCE_LITERAL__
EXPECTED_JOB_ALIAS = __EXPECTED_JOB_LITERAL__
assert bpy.context.scene.get("ai_studio_isolated_test") is True, "Not the initialized isolated scene"
assert bpy.data.filepath.replace("\\", "/").casefold() == EXPECTED_SOURCE.replace("\\", "/").casefold(), "Another source.blend is open"
assert bpy.context.scene.get("ai_studio_job"), "Missing actual bootstrap marker"
if EXPECTED_JOB_ALIAS:
    assert bpy.context.scene.get("ai_studio_job") == EXPECTED_JOB_ALIAS, "Another bootstrap job is open"
loaded = []
for frame in range(1, 19):
    filename = bpy.path.abspath("//frames/frame_%04d.png" % frame)
    image = bpy.data.images.load(filename, check_existing=False)
    image.use_fake_user = True
    image.pack()
    assert image.packed_file and image.use_fake_user, "Image retention was not set"
    loaded.append(image.name)
assert len(loaded) == 18, "Incomplete existing frame set"
bpy.ops.wm.save_mainfile()
print({"saved_owned_source": True, "loaded_existing_frames": 18, "persistent_packing": "requires independent disk reload and PNG-byte comparison", "rerendered": False})
