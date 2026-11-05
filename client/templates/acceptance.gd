extends SceneTree
# Deterministic smoke test for the starter contract, NOT general-purpose game QA.
var failures: Array[String] = []

func _init() -> void:
    call_deferred("_run")

func check(condition: bool, message: String) -> void:
    if not condition:
        failures.append(message)
        push_error("PIPELINE_CHECK_FAILED: " + message)

func _run() -> void:
    var packed = load("res://main.tscn")
    if not packed is PackedScene:
        check(false, "main.tscn is not a PackedScene")
        _finish()
        return
    var main = packed.instantiate()
    root.add_child(main)
    current_scene = main
    await process_frame
    await physics_frame
    for method in ["attack", "get_enemy_hp", "get_effect_spawn_count", "get_effect_sprite_frames"]:
        check(main.has_method(method), "Missing root method: " + method)
    check(main.get_node_or_null("Player") is Node2D, "Missing Player Node2D")
    check(main.get_node_or_null("Enemy") is Node2D, "Missing Enemy Node2D")
    check(InputMap.has_action("move_right"), "Missing move_right input action")
    if not failures.is_empty():
        _finish()
        return
    var player = main.get_node("Player")
    var start_x: float = player.position.x
    Input.action_press("move_right")
    await create_timer(0.15).timeout
    Input.action_release("move_right")
    await physics_frame
    check(player.position.x > start_x + 1.0, "Real input did not move Player")
    check(main.get_enemy_hp() == 100, "Dummy HP must start at 100")
    check(main.get_effect_spawn_count() == 0, "No spontaneous effects without demo mode")
    var sprite_frames = main.get_effect_sprite_frames()
    check(sprite_frames is SpriteFrames, "Must expose the real integrated SpriteFrames")
    if sprite_frames is SpriteFrames:
        check(sprite_frames.has_animation("fan_wave"), "Missing fan_wave animation")
        if sprite_frames.has_animation("fan_wave"):
            check(sprite_frames.get_frame_count("fan_wave") == 18, "Expected 18 animation frames")
            check(abs(sprite_frames.get_animation_speed("fan_wave") - 30.0) < 0.01, "Expected 30 fps")
            check(not sprite_frames.get_animation_loop("fan_wave"), "Attack VFX must not loop")
            for i in range(sprite_frames.get_frame_count("fan_wave")):
                check(sprite_frames.get_frame_texture("fan_wave", i) is Texture2D, "Frame texture is missing")
    main.attack()
    await create_timer(0.8).timeout
    check(main.get_enemy_hp() == 90, "First attack must cause exactly 10 damage")
    check(main.get_effect_spawn_count() == 1, "First attack must spawn exactly one effect")
    main.attack()
    main.attack()
    await create_timer(0.8).timeout
    check(main.get_enemy_hp() == 80, "Cooldown must reject the duplicate attack")
    check(main.get_effect_spawn_count() == 2, "Duplicate attack incorrectly spawned another effect")
    _finish()

func _finish() -> void:
    if failures.is_empty():
        print("PIPELINE_ACCEPTANCE_PASS")
        quit(0)
    else:
        print("PIPELINE_ACCEPTANCE_FAIL: " + str(failures))
        quit(1)
