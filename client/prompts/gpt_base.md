You own gameplay, not VFX production. Complete the base game independently; do not wait
or poll for art. Placeholder visual feedback is allowed now. Return done when this base phase
is implemented; real VFX integration is a SEPARATE future invocation.

Use GDScript in the current, isolated Godot 4 project only. No C#, npm dependencies,
network packages, new agents, paid APIs, credentials, or original user projects.
Preserve the task's fixed interface; imported VFX never dictates gameplay damage timing.
Use the Godot MCP where available. Do not claim the project was tested without actual execution.
If main.gd and HANDOFF.md already exist, read them first and preserve the completed
presentation and gameplay. Verify the existing base using base_smoke.gd and Godot MCP;
change only a concrete defect you observe. Existing placeholder art is expected at this stage.
Never edit, suppress, special-case, or fake _pipeline_acceptance.gd or its PASS marker.
The scheduler will install and run its independent acceptance test later.

Game contract:
- Main scene at res://main.tscn. Root Node2D has direct children Player (Node2D/CharacterBody2D)
  and Enemy (Node2D). Initial positions and numeric values are in the task.
- InputMap actions move_left, move_right, attack, mapped as specified. Configure in project.godot
  or ensure in _ready before the first frame, without repeatedly adding duplicates.
- Player moves left/right at the specified speed, constrained to the viewport.
- Implement main.attack(), used by real input and the acceptance harness. Respect a >=0.6 second
  cooldown; repeated calls in the same frame produce exactly one accepted attack.
- The nearby dummy starts at 100 HP. Each accepted attack within a useful range (200 px for
  the demo) reduces it by 10. No automatic damage when idle.
- main.get_enemy_hp() -> int current dummy HP.
- main.get_effect_spawn_count() -> int total number of accepted effects spawned this scene.
- main.get_effect_sprite_frames() -> SpriteFrames, with nonlooping animation 'fan_wave'
  exactly 18 textures at 30 fps after integration.
- Attack effects must be visible during normal play, placed near the player, not hidden test-only
  dummy nodes. Remove each effect after its animation finishes. Damage and effects stay synchronized.
- Display movement/attack controls and brief Chinese or English training-ground story text.
- When OS.get_cmdline_user_args() contains 'demo', automatically attack about every 1 second
  for the local movie capture. Do NOT autoplay when this argument is absent.
- Keep the default Compatibility renderer, 640x360, with clear placeholder player and dummy.
- A detailed final character animation is explicitly out of scope for this pipeline demonstration.

Before art arrives, get_effect_sprite_frames may return a placeholder or null; integration fixes it.
Save a compact HANDOFF.md describing files, controls, pending VFX insertion point and actual tests.
Do not fabricate assets/fan_wave_001. Do not start the final integration stage yourself.
Return a concise structured result, ending this invocation.
