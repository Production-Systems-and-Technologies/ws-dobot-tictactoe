#!/usr/bin/env python3
# calibrate_robot.py - Dobot Magician terminal calibration tool
#
# Keys
#   1-9        select slot                 PgUp / PgDn   step +/-1 (or +/-0.1 below 1)
#   arrows     jog X/Y                     z / Z         jog Z up/down
#   r / R      jog R                       g             go to selected saved point
#   x          toggle vacuum
#   Enter      save current pose           q / Esc       quit and save JSON
#
# The file calib_points.json will contain the nine positions required for the main tictactoe.py.

import curses, json, time
from pathlib import Path
from dobot_python.dobot import Dobot

PORT          = "/dev/ttyUSB0"              # Port used to communicate with Dobot
STEP_DEFAULT  = 2                           # Default step size (2 mm)
TEST_APPROACH_OFFSET = 30.0                 # Safe height for testing saved points
POSE_MATCH_TOL_MM = 2.0                     # Tolerance for detecting PRE_HOMING
CALIB_FILE    = Path("calib_points.json")

POSITIONS = ["PICK_X", "RETURN_X",
            "PICK_O", "RETURN_O",
            "TL_CORNER", "BL_CORNER",
            "BR_CORNER", "TR_CORNER",
            "PRE_HOMING"]

# ───────────────────────── drawing helper ──────────────────────────
def _fmt_slot(val):
    """Return a nicely aligned “[  +X   +Y   +Z   +R ]”  or “---”."""
    if val is None:
        return "---"
    x, y, z, r = val
    # Format specifier: +8.2f -> sign always shown, width=8, 2 decimals
    return F"[{x:+8.2f} {y:+8.2f} {z:+8.2f} {r:+8.2f}]"


def _same_xyz(a, b, tol=POSE_MATCH_TOL_MM):
    return all(abs(float(a[i]) - float(b[i])) <= tol for i in range(3))


def paint(win, step, idx, calib, pose_line):
    win.erase()         
    win.addstr(0, 0,
            f"|Step={step:.1f}| |Arrows :XY| |z/Z:up/down| |r/R:±R|"
            f"|PgUp/PgDn:step size| |1-9:slot| |enter:save| |g:test| |x:vac| |q:quit|")
    
    # Draw each calibration positions slot, one per row
    for i, name in enumerate(POSITIONS):
        txt  = _fmt_slot(calib.get(name))
        mark = "👉" if i == idx else "  "
        win.addstr(2 + i, 0, f"{mark} {i+1}. {name:<10}: {txt}")
        win.clrtoeol()
    win.addstr(3 + len(POSITIONS), 0, pose_line);  win.clrtoeol()
    win.refresh()
# ───────────────────────────── main loop ───────────────────────────
def calibrate_robot(scr):
    scr.keypad(True)
    scr.timeout(60)
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)

    # Load existing calibration file
    try:
        calib = json.loads(CALIB_FILE.read_text())
    except Exception:
        calib = {}
    if "PRE_HOMING" not in calib and "PRE_HOME" in calib:
        calib["PRE_HOMING"] = calib["PRE_HOME"]

    robot      = Dobot(PORT)
    pose       = list(robot.get_pose()[:4])
    step       = float(STEP_DEFAULT)
    slot_idx   = 0
    dx=dy=dz=dr = 0.0
    suction_on = False
    idle_counter = 0

    # Pre-build the first live-pose line and draw everything
    pose_line = f"Live Pose: |X:{pose[0]:7.2f}| |Y:{pose[1]:7.2f}| |Z:{pose[2]:7.2f}| |R={pose[3]:6.2f}|"
    paint(scr, step, slot_idx, calib, pose_line)

    try:
        while True:
            key = scr.getch()

            # filter out stray mouse events
            if key == curses.KEY_MOUSE:
                try:
                    curses.getmouse()
                except curses.error:
                    pass
                key = -1

            # -------- key handling ----------------------------------------
                
            # Number keys select which slot we're working on.
            if ord("1") <= key <= ord(str(len(POSITIONS))):
                slot_idx = key - ord("1")

            # PgUp / PgDn: adjust step size up/down (1 mm steps above 1 mm, 0.1 mm below)
            elif key == curses.KEY_PPAGE:
                if step >= 1:
                    step += 1
                else:
                    step = round(step + 0.1, 2)
            elif key == curses.KEY_NPAGE:
                if step > 1:
                    step -= 1
                    if step < 1: step = 1.0
                else:
                    step = max(0.1, round(step - 0.1, 2))

            # Robot movements in X/Y/Z/R
            elif key == curses.KEY_LEFT:   dx = +step
            elif key == curses.KEY_RIGHT:  dx = -step
            elif key == curses.KEY_UP:     dy = -step
            elif key == curses.KEY_DOWN:   dy = +step
            elif key == ord("z"):          dz = +step
            elif key == ord("Z"):          dz = -step
            elif key == ord("r"):          dr = -step
            elif key == ord("R"):          dr = +step

            # Enter: save the current pose into the selected slot
            elif key in (10, 13, curses.KEY_ENTER):
                real = robot.get_pose()[:4]
                calib[POSITIONS[slot_idx]] = [round(v,2) for v in real]
                pose       = list(real)
                pose_line  = f"Saved → {POSITIONS[slot_idx]}"
                idle_counter = 0

            # Go to the currently selected saved pose through a safe approach height.
            elif key in (ord("g"), ord("G")):
                selected_name = POSITIONS[slot_idx]
                saved = calib.get(selected_name)
                if saved is None:
                    pose_line = f"No saved pose for {selected_name}"
                    idle_counter = 0
                else:
                    try:
                        x, y, z, r = saved
                        robot.interface.clear_queue()
                        robot.interface.start_queue()
                        current = robot.get_pose()[:4]
                        pre_homing = calib.get("PRE_HOMING")
                        if pre_homing is None or not _same_xyz(current, pre_homing):
                            robot.move_linear_rel(0, 0, TEST_APPROACH_OFFSET, 0, wait=True)
                        if selected_name == "PRE_HOMING":
                            robot.move_joint(x, y, z, r, wait=True)
                            pose = [x, y, z, r]
                            pose_line = f"At {selected_name}: |X:{x:7.2f}| |Y:{y:7.2f}| |Z:{z:7.2f}| |R={r:6.2f}|"
                        else:
                            robot.move_joint(x, y, z + TEST_APPROACH_OFFSET, r, wait=True)
                            robot.move_linear(x, y, z, r, wait=True)
                            pose = [x, y, z, r]
                            pose_line = f"At {selected_name}: |X:{x:7.2f}| |Y:{y:7.2f}| |Z:{z:7.2f}| |R={r:6.2f}|"
                    except Exception as exc:
                        robot.clear_alarms()
                        pose_line = f"Error testing {selected_name}: {exc}"
                    idle_counter = 0

            # X: toggle the suction cup immediately
            elif key in (ord("x"), ord("X")):
                suction_on = not suction_on
                robot.set_suction(suction_on)
                pose_line = f"Suction {'ON' if suction_on else 'OFF'}"
                idle_counter = 0

            # q or Esc: exit the loop
            elif key in (ord("q"), ord("Q"), 27):
                break

            # -------- execute jog ------------------------------------------
            if any((dx,dy,dz,dr)):
                try:
                    # clear any queued motions so this one runs instantly
                    robot.interface.clear_queue()
                    robot.interface.start_queue()
                    robot.move_linear_rel(dx,dy,dz,dr)
                    # update our local cache so the pose_line is instantaneous
                    pose[0]+=dx; pose[1]+=dy; pose[2]+=dz; pose[3]+=dr
                    pose_line = f"Live Pose: |X:{pose[0]:7.2f}| |Y:{pose[1]:7.2f}| |Z:{pose[2]:7.2f}| |R={pose[3]:6.2f}|"
                except RuntimeError as exc:
                    robot.clear_alarms()
                    pose_line = f"⚠ {exc}"
                dx=dy=dz=dr=0
                idle_counter = 0

            # periodic true pose refresh
            idle_counter += 1
            if idle_counter >= 20:
                pose = list(robot.get_pose()[:4])
                pose_line = f"Live Pose: |X:{pose[0]:7.2f}| |Y:{pose[1]:7.2f}| |Z:{pose[2]:7.2f}| |R={pose[3]:6.2f}|"
                idle_counter = 0

            paint(scr, step, slot_idx, calib, pose_line)
            # if no key pressed, throttle the loop
            if key == -1:
                time.sleep(0.02)
    finally:
        CALIB_FILE.write_text(json.dumps({k: calib.get(k) for k in POSITIONS if k in calib}, indent=2))
        robot.interface.serial.close()

# ────────────────────────────── entry point ────────────────────────
def main():
    curses.wrapper(calibrate_robot)

if __name__ == "__main__":
    main()
