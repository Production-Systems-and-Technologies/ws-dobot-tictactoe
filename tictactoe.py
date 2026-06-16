import argparse
import os
import queue
import threading
import tkinter as tk
from functools import partial
from tkinter import PhotoImage
from tkinter import ttk
from helpers.load_calibration import load_calibration
from helpers.robot_motion import RobotMotion
from helpers.game_logic import get_winner_and_cells, is_draw, AIPlayer

# ----------------------------------------------------------- #
# 0.  CONFIGURATION
# ----------------------------------------------------------- #

PORT                            = "/dev/ttyUSB0"
BOARD_ORIENTATION               = 0              # 0/90/180/270 used to rotate board mapping
APPROACH_OFFSET                 = 35             # mm above piece before descending
RETRACT_DISTANCE                = 12             # mm straight up after picking/placing
PLACE_OFFSET                    = 8              # mm offset for dropping pieces onto board
POSE_TOL_MM, POSE_POLL_S        = 1.0, 0.05      # mm, s
AI_AUTO_HOME_EVERY_GAMES        = 10
MOVEJ_VEL, MOVEJ_ACC            = 400.0, 450.0
MOVEL_XYZ_VEL, MOVEL_XYZ_ACC    = 120.0, 300.0

CAL = load_calibration("calib_points.json", place_offset=PLACE_OFFSET)
PICK_X              = CAL["PICK_X"]
RETURN_X            = CAL["RETURN_X"]
PICK_O              = CAL["PICK_O"]
RETURN_O            = CAL["RETURN_O"]
TTT_CELLS_PICK      = CAL["TTT_CELLS_PICK"]
TTT_CELLS_PLACE     = CAL["TTT_CELLS_PLACE"]
PRE_HOMING          = CAL.get("PRE_HOMING")

BOARD_SCALE = 3  # integer scaling factor for board images (1 = original size)


class MockDobot:
    """Small stand-in for GUI/game development without connected hardware."""

    def __init__(self, port="mock", vel=50.0, acc=50.0):
        self.port = port
        self.vel = vel
        self.acc = acc
        self.coord_vel = (0.0, 0.0, 0.0, 0.0)
        self.common_ratio = (0.0, 0.0)
        self.suction_on = False
        self.pose = (200.0, 0.0, 50.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def connected(self):
        return True

    def get_pose(self):
        return self.pose

    def _set_pose(self, x, y, z, r):
        self.pose = (float(x), float(y), float(z), float(r), 0.0, 0.0, 0.0, 0.0)

    def move_joint(self, x, y, z, r, *, wait=False):
        self._set_pose(x, y, z, r)

    def move_linear(self, x, y, z, r, *, wait=False):
        self._set_pose(x, y, z, r)

    def move_linear_rel(self, dx, dy, dz, dr, *, wait=False):
        x, y, z, r = self.pose[:4]
        self._set_pose(x + dx, y + dy, z + dz, r + dr)

    def set_motion_params(self, vel, acc, *, queue=False):
        self.vel = vel
        self.acc = acc

    def set_ptp_coordinate_params(self, xyz_vel, r_vel, xyz_acc, r_acc, *, queue=False):
        self.coord_vel = (xyz_vel, r_vel, xyz_acc, r_acc)

    def set_ptp_common_params(self, vel_ratio, acc_ratio, *, queue=False):
        self.common_ratio = (vel_ratio, acc_ratio)

    def set_suction(self, on, *, wait=False):
        self.suction_on = bool(on)

    def clear_alarms(self):
        pass

    def home(self, *, wait=False):
        pass

# ----------------------------------------------------------- #
# 1. Additional Helpers
# ----------------------------------------------------------- #

def map_gui_to_robot(gui_row: int, gui_col: int, rotation: int = 0):
    """helper to rotate GUI (row,col) to robot (row,col) based on BOARD_ORIENTATION."""
    x, y = gui_col, 2 - gui_row

    if   rotation == 0:   robot_r, robot_c = y,         x
    elif rotation == 90:  robot_r, robot_c = 2 - x,     y
    elif rotation == 180: robot_r, robot_c = 2 - y, 2 - x
    elif rotation == 270: robot_r, robot_c = x,      2 - y
    else:
        raise ValueError("rotation must be 0, 90, 180, or 270")

    return robot_r, robot_c


# ----------------------------------------------------------- #
# 2. TKINTER GUI (Robot Tic-Tac-Toe)
# ----------------------------------------------------------- #

class TicTacToeGUI(tk.Tk):
    def __init__(self, port='/dev/ttyUSB0', vel=50.0, acc=50.0, robot=None, mock=False):
        super().__init__()
        if robot is None:
            from dobot_python.dobot import Dobot
            robot = Dobot(port, vel, acc)
        self.robot = robot
        self.robot_motions = RobotMotion(
            self.robot,
            approach_offset=APPROACH_OFFSET,
            retract_distance=RETRACT_DISTANCE,
            pose_tol_mm=POSE_TOL_MM,
            pose_poll_s=POSE_POLL_S,
            joint_vel=MOVEJ_VEL,
            joint_acc=MOVEJ_ACC,
            linear_xyz_vel=MOVEL_XYZ_VEL,
            linear_xyz_acc=MOVEL_XYZ_ACC,
        )
        self.title("Robot Tic-Tac-Toe" + (" (Mock)" if mock else ""))
        self.geometry("960x1350")
        self.resizable(False, False)
        
        # Colors for updating labels
        self.PLAYER_COLORS = {'X': 'red', 'O': 'blue'}
        
        # Load images for the buttons (empty cells, X, O, rotation arrows)
        self.load_images()

        # Initialize game variables
        self.board = [["", "", ""],
                      ["", "", ""],
                      ["", "", ""]]
        
        self.current_player = "X" # Game always starts with player X
        self.game_over = False
        self.busy = False

        # Initialize result counters
        self.x_wins = 0
        self.o_wins = 0
        self.draws = 0

        # Keeping count of what round it is for movement routines
        self.x_picked = 0
        self.o_picked = 0

        # Initialize AI settings
        self.game_mode = tk.StringVar(value="PvP")
        self.ai_difficulty = tk.StringVar(value="easy")
        self.ai2_difficulty = tk.StringVar(value="easy")
        self.ai_player = AIPlayer(player='O', difficulty=self.ai_difficulty.get())
        self.ai2_player = AIPlayer(player='X', difficulty=self.ai2_difficulty.get())
        self.aivai_active = False
        self._after_ids = set()
        self._mode_generation = 0
        self._pending_mode_change = None
        self._robot_task_active = False
        self._robot_results = queue.Queue()
        self.aivai_games_since_home = 0
        self.home_pending = False

        # Build UI
        self.create_top_area()
        self.create_settings_ui()
        self.create_message_area()
        self.create_main_board()
        self.create_bottom_area()
        self.after(50, self._poll_robot_results)

    # ----------------------
    #    UI CREATION
    # ----------------------
    def load_images(self):
        try:
            base_empty = PhotoImage(file="assets/empty.png")
            base_x     = PhotoImage(file="assets/x.png")
            base_o     = PhotoImage(file="assets/o.png")

            # Scale board tiles by BOARD_SCALE (must be an int)
            s = BOARD_SCALE
            if s > 1:
                self.empty_cell_img = base_empty.zoom(s, s)
                self.x_img          = base_x.zoom(s, s)
                self.o_img          = base_o.zoom(s, s)
            else:
                # fallback to original size if scale = 1
                self.empty_cell_img = base_empty
                self.x_img          = base_x
                self.o_img          = base_o

        except tk.TclError as e:
            print(f"Error loading images: {e}")
            self.empty_cell_img = None
            self.x_img = None
            self.o_img = None


    def create_top_area(self):
        self.top_frame = tk.Frame(self)
        self.top_frame.pack(pady=5)

        # Current Player Display
        self.player_frame = tk.Frame(self.top_frame)
        self.player_frame.pack(pady=5)

        self.current_player_text = tk.Label(self.player_frame, text="Current Player: ", font=("Impact", 28))
        self.current_player_text.pack(side=tk.LEFT)

        self.current_player_symbol = tk.Label(self.player_frame, text="X", font=("Helvetica", 28, "bold"), fg='red')
        self.current_player_symbol.pack(side=tk.LEFT)
        self.update_current_player_label()

        # Separator
        separator = ttk.Separator(self.top_frame, orient='horizontal')
        separator.pack(fill='x', pady=5)

        # Results Frame
        self.results_frame = tk.Frame(self.top_frame)
        self.results_frame.pack(pady=5)

        self.results_label = tk.Label(self.results_frame, text=self.get_results_text(), font=("Helvetica", 18, "bold"))
        self.results_label.pack(side=tk.LEFT, padx=5)

        self.reset_stats_button = tk.Button(self.results_frame, text="Reset Stats", command=self.reset_stats)
        self.reset_stats_button.pack(side=tk.LEFT, padx=10)

        separator2 = ttk.Separator(self.top_frame, orient='horizontal')
        separator2.pack(fill='x', pady=5)

    def create_settings_ui(self):
        self.settings_frame = tk.Frame(self)
        self.settings_frame.pack(pady=5)

        # Game Mode / Difficulty
        settings_side_frame = tk.Frame(self.settings_frame)
        settings_side_frame.pack()

        # Game mode frame
        game_mode_frame = tk.Frame(settings_side_frame)
        game_mode_frame.grid(row=0, column=0, padx=10, pady=2, sticky='nw')

        game_mode_label = tk.Label(game_mode_frame, text="Select Game Mode:", font=("Helvetica", 11, "bold"))
        game_mode_label.pack(anchor='w')

        modes = [("Player vs Player", "PvP"), ("Player vs AI", "PvAI"), ("AI vs AI", "AivAI")]
        for text, mode in modes:
            rb = tk.Radiobutton(game_mode_frame, text=text, variable=self.game_mode, value=mode, command=self.on_mode_change)
            rb.pack(anchor='w')

        # AI1 difficulty frame
        self.ai_difficulty_frame = tk.Frame(settings_side_frame)
        self.ai_difficulty_frame.grid(row=0, column=1, padx=10, pady=2, sticky='nw')

        ai_difficulty_label = tk.Label(self.ai_difficulty_frame, text="AI Difficulty (O)", font=("Helvetica", 11, "bold"))
        ai_difficulty_label.pack(anchor='w')

        difficulties = [("Easy", "easy"), ("Medium", "medium"), ("Hard", "hard")]
        for text, difficulty in difficulties:
            rb = tk.Radiobutton(self.ai_difficulty_frame, text=text, variable=self.ai_difficulty,
                                 value=difficulty, command=self.on_o_difficulty_change)
            rb.pack(anchor='w')

        # AI2 difficulty frame (hidden unless AivAI)
        self.ai_difficulty_secondary_frame = tk.Frame(settings_side_frame)
        ai_difficulty_secondary_label = tk.Label(self.ai_difficulty_secondary_frame, text="AI Difficulty (X)", font=("Helvetica", 11, "bold"))
        ai_difficulty_secondary_label.pack(anchor='w')

        for text, difficulty in difficulties:
            rb = tk.Radiobutton(self.ai_difficulty_secondary_frame, text=text, variable=self.ai2_difficulty,
                                 value=difficulty, command=self.on_x_difficulty_change)
            rb.pack(anchor='w')

        self.ai_difficulty_secondary_frame.grid(row=0, column=2, padx=10, pady=2, sticky='nw')
        self.ai_difficulty_secondary_frame.grid_remove()

        separator3 = ttk.Separator(self.settings_frame, orient='horizontal')
        separator3.pack(fill='x', pady=3)

        # Toggle AI settings based on initial mode
        self.toggle_ai_settings()

    def create_message_area(self):
        self.message_frame = tk.Frame(self)
        self.message_frame.pack(pady=5)
        self.result_label = tk.Label(self.message_frame, text="", font=("Helvetica", 16))
        self.result_label.pack()

    def create_main_board(self):
        self.main_board_frame = tk.Frame(self)
        self.main_board_frame.pack(pady=3)

        # Board frame
        self.board_frame = tk.Frame(self.main_board_frame)
        self.board_frame.pack(side=tk.LEFT, padx=5)

        self.buttons = []
        for r in range(3):
            row_buttons = []
            for c in range(3):
                btn = tk.Button(self.board_frame, image=self.empty_cell_img, command=partial(self.cell_clicked, r, c))
                btn.grid(row=r, column=c, padx=2, pady=2)
                row_buttons.append(btn)
            self.buttons.append(row_buttons)

    def create_bottom_area(self):
        self.bottom_frame = tk.Frame(self)
        self.bottom_frame.pack(pady=0)

        control_frame = tk.Frame(self.bottom_frame)
        control_frame.pack(pady=8)

        reset_button = tk.Button(control_frame, text="New Game", command=self.reset_game, width=10)
        reset_button.pack(side=tk.LEFT, padx=5)

        cleanup_button = tk.Button(control_frame, text="Cleanup Board", command=self.cleanup_on_button, width=12)
        cleanup_button.pack(side=tk.LEFT, padx=5)

        start_button = tk.Button(control_frame, text="Start Game", command=self.start_game, width=10)
        start_button.pack(side=tk.LEFT, padx=5)

    # ----------------------
    #   ROBOT / GAME LOGIC
    # ----------------------

    # -- scheduling helpers for AIvAI timers ---------------------------------
    def _after(self, ms, func):
        """Schedule and remember the 'after' ID for later cancellation."""
        aid = self.after(ms, func)
        self._after_ids.add(aid)
        return aid

    def _cancel_afters(self):
        """Cancel and clear all remembered 'after' timers (safe to call anytime)."""
        for aid in list(self._after_ids):
            try:
                self.after_cancel(aid)
            except Exception:
                pass
            self._after_ids.discard(aid)

    def _board_has_pieces(self):
        return any(cell for row in self.board for cell in row)

    def _sync_board_buttons(self):
        if self.game_over or self.busy or self._robot_task_active or self.game_mode.get() == "AivAI":
            self.disable_board_buttons()
        else:
            self.enable_board_buttons()

    def _start_robot_task(self, status, work, on_success=None, on_error=None):
        if self._robot_task_active:
            self.update_status("Robot is busy. Try again after the current move.")
            return False

        self._robot_task_active = True
        self.busy = True
        self.disable_board_buttons()
        self.update_status(status)

        def runner():
            error = None
            try:
                work()
            except Exception as exc:
                error = exc
            self._robot_results.put((on_success, on_error, error))

        threading.Thread(target=runner, daemon=True).start()
        return True

    def _poll_robot_results(self):
        while True:
            try:
                on_success, on_error, error = self._robot_results.get_nowait()
            except queue.Empty:
                break
            self._finish_robot_task(on_success, on_error, error)
        self.after(50, self._poll_robot_results)

    def _finish_robot_task(self, on_success, on_error, error):
        self._robot_task_active = False
        self.busy = False

        if error is None:
            if on_success:
                on_success()
        else:
            if on_error:
                on_error(error)
            else:
                self.update_status(f"Robot error: {error}")

        if self._pending_mode_change and not self._robot_task_active:
            self._apply_pending_mode_change()
        elif not self._robot_task_active:
            self._sync_board_buttons()

    def _queue_mode_change_after_task(self, mode):
        self._mode_generation += 1
        self._cancel_afters()
        self.aivai_active = False
        self._pending_mode_change = mode
        self.toggle_ai_settings()
        self.disable_board_buttons()
        self.update_status(f"Switching to {mode} after the current robot move.")

    def _apply_pending_mode_change(self):
        mode = self._pending_mode_change
        self._pending_mode_change = None
        self._start_mode_after_cleanup(mode)

    def _start_mode_after_cleanup(self, mode):
        if self._board_has_pieces():
            self.cleanup_board(on_complete=lambda mode=mode: self._start_mode(mode))
        else:
            self._start_mode(mode)

    def _start_mode(self, mode):
        self._cancel_afters()
        self.aivai_active = False
        self._reset_game_state(f"{mode} mode selected. New game started.")
        self.toggle_ai_settings()
        self.home_pending = False

        if mode == "AivAI":
            self.aivai_games_since_home = 0
            self.disable_board_buttons()
            self._after(100, self.start_aivai_game)
        else:
            self.enable_board_buttons()

    def _reset_game_state(self, message="New game started."):
        self.game_over = False
        self.board = [["", "", ""], ["", "", ""], ["", "", ""]]
        self.current_player = "X"
        self.ai_player.memo = {}
        self.ai2_player.memo = {}
        self.x_picked = 0
        self.o_picked = 0
        self.result_label.config(text="")
        self.update_current_player_label()

        for r in range(3):
            for c in range(3):
                self.buttons[r][c].config(image=self.empty_cell_img, bg=self.cget("bg"))

        self.update_status(message, transient=True)
        self._sync_board_buttons()

    def attempt_move(self, row, col, piece, on_complete=None):
        """
        Consolidates the logic for making a move:
          1) Checks if cell is empty
          2) Robot picks & places the piece in the background
          3) Applies board/UI state when the robot task succeeds
        Returns True if move succeeded, False if invalid.
        """
        if self.board[row][col] != "":
            self.update_status("Invalid move. Try again.", transient=True)
            return False

        pick_number = self.x_picked + 1 if piece == "X" else self.o_picked + 1
        robot_r, robot_c = map_gui_to_robot(row, col, BOARD_ORIENTATION)
        place_position = TTT_CELLS_PLACE[robot_r][robot_c]

        def work():
            if piece == "X":
                pick_position = PICK_X
            else:
                pick_position = PICK_O

            if pick_number == 4:
                self.robot_motions.special_pick(pick_position)
            else:
                self.robot_motions.pick_object(pick_position, mode='pickup')
            self.robot_motions.place_object(place_position)

        def success():
            self.board[row][col] = piece
            if piece == "X":
                self.x_picked = pick_number
                self.buttons[row][col].config(image=self.x_img)
            else:
                self.o_picked = pick_number
                self.buttons[row][col].config(image=self.o_img)

            winner, winning_cells = get_winner_and_cells(self.board)
            if winner:
                self.game_over = True
                self.highlight_winning_line(winning_cells)
                self.show_result(f"Player {winner} wins!")
            elif is_draw(self.board):
                self.game_over = True
                self.show_result("It's a Draw!")
            else:
                self.current_player = "O" if self.current_player == "X" else "X"
                self.update_current_player_label()

            if on_complete:
                on_complete(True)

        def error(exc):
            self.update_status(f"Robot error during move: {exc}")
            if on_complete:
                on_complete(False)

        return self._start_robot_task(f"Moving {piece}...", work, success, error)

    def cleanup_board(self, on_complete=None):
        """
        Robot collects each piece from the board (considering rotation) and
        returns them to the respective slide. Resets board state.
        """
        board_snapshot = [row[:] for row in self.board]
        if not any(cell for row in board_snapshot for cell in row):
            self._reset_game_state()
            if on_complete:
                on_complete()
            return True

        def work():
            for r in range(3):
                for c in range(3):
                    piece = board_snapshot[r][c]
                    if piece != "":
                        robot_r, robot_c = map_gui_to_robot(r, c, BOARD_ORIENTATION)
                        pick_position = TTT_CELLS_PICK[robot_r][robot_c]

                        self.robot_motions.pick_object(pick_position, mode='cleanup')
                        if piece == "X":
                            self.robot_motions.place_object(RETURN_X)
                        else:
                            self.robot_motions.place_object(RETURN_O)

        def success():
            self._reset_game_state()
            if on_complete:
                on_complete()

        return self._start_robot_task("Cleaning board...", work, success)

    # ----------------------
    #      EVENT HANDLERS
    # ----------------------

    def cell_clicked(self, row, col):
        """Human clicks a square (PvP or PvAI)."""
        if self.game_over or self.busy or self._robot_task_active or self.game_mode.get() == "AivAI":
            return
        if self.board[row][col] != "":
            self.update_status("Invalid move. Try again.", transient=True)
            return

        self._human_move(row, col)

    def _human_move(self, row, col):
        """Starts one human move, then continues after robot motion finishes."""
        generation = self._mode_generation
        started = self.attempt_move(
            row,
            col,
            self.current_player,
            on_complete=lambda ok, generation=generation: self._after_human_move(ok, generation),
        )
        if not started:
            self.busy = False
            self._sync_board_buttons()
        return

    def _after_human_move(self, ok, generation):
        if not ok or generation != self._mode_generation or self._pending_mode_change:
            return

        if self.game_over:
            self._sync_board_buttons()
            return

        if self.game_mode.get() == "PvP":
            self.after_idle(self._release_human_turn)
        elif self.game_mode.get() == "PvAI" and self.current_player == "O":
            self.busy = True
            self.disable_board_buttons()
            self._after(80, lambda generation=generation: self.ai_move(generation))
        else:
            self.busy = False
            self._sync_board_buttons()

    def _release_human_turn(self):
        """Enables board after robot has certainly finished the previous move."""
        self.busy = False
        self._sync_board_buttons()

    def ai_move(self, generation=None):
        if generation is None:
            generation = self._mode_generation
        if self.game_over or generation != self._mode_generation or self._pending_mode_change:
            return
        move = self.ai_player.get_move(self.board)
        if move is None:
            self.update_status("AI has no moves. Board full or error.")
            self.busy = False
            self._sync_board_buttons()
            return

        r, c = move
        self.attempt_move(
            r,
            c,
            self.ai_player.player,
            on_complete=lambda ok, generation=generation: self._after_ai_move(ok, generation),
        )

    def _after_ai_move(self, ok, generation):
        if not ok or generation != self._mode_generation or self._pending_mode_change:
            return
        self.busy = False
        self._sync_board_buttons()

    def start_game(self):
        """
        Called when pressing 'Start Game'. Initiates AivAI or resets for PvP/PvAI.
        """
        mode = self.game_mode.get()
        if self._robot_task_active:
            self._queue_mode_change_after_task(mode)
            return

        self._mode_generation += 1
        if mode in ["PvP", "PvAI", "AivAI"]:
            self._start_mode_after_cleanup(mode)
        else:
            self.show_result("Please select a valid game mode.")


    def start_aivai_game(self):
        """
        Initiates the AI vs AI game loop.
        """
        if self.aivai_active or self.game_mode.get() != "AivAI" or self._robot_task_active:
            return
        self.aivai_active = True
        self.disable_board_buttons()
        self.update_status("AI vs AI game started.", transient=True)
        self._after(100, lambda generation=self._mode_generation: self.aivai_move(generation))


    def aivai_move(self, generation=None):
        """
        AI vs AI move sequence (ticks via Tk 'after').
        """
        if generation is None:
            generation = self._mode_generation

        # Stop if mode changed or manually deactivated
        if (not self.aivai_active or self.game_mode.get() != "AivAI" or
                generation != self._mode_generation or self._pending_mode_change):
            return

        if self.game_over:
            self._after(2000, self.cleanup_board_automatically)
            self.aivai_active = False
            return

        current_ai = self.ai2_player if self.current_player == "X" else self.ai_player
        move = current_ai.get_move(self.board)
        if move:
            r, c = move
            self.attempt_move(
                r,
                c,
                current_ai.player,
                on_complete=lambda ok, generation=generation: self._after_aivai_move(ok, generation),
            )
        else:
            self.update_status(f"AI {current_ai.player} has no moves.", transient=True)

    def _after_aivai_move(self, ok, generation):
        if (not ok or generation != self._mode_generation or self._pending_mode_change or
                not self.aivai_active or self.game_mode.get() != "AivAI"):
            return

        if not self.game_over:
            self._after(10, lambda generation=generation: self.aivai_move(generation))


    # ----------------------
    #      UI UPDATES
    # ----------------------

    def highlight_winning_line(self, winning_cells):
        """
        Highlights the three winning cells in green.
        """
        if not winning_cells:
            return
        for (r, c) in winning_cells:
            self.buttons[r][c].config(bg="lightgreen")

    def update_current_player_label(self):
        color = self.PLAYER_COLORS.get(self.current_player, 'black')
        self.current_player_symbol.config(text=self.current_player, fg=color)

    def update_status(self, message, transient=False, delay=2000):
        """
        Updates the status message. If transient, clears after a delay.
        """
        self.result_label.config(text=message)
        if transient:
            self.after(delay, lambda: self.result_label.config(text=""))

    def show_result(self, message):
        """
        Displays final result and updates stats if needed.
        """
        self.result_label.config(text=message)
        if "wins" in message:
            # e.g., "Player X wins!"
            winner = message.split(" ")[1]
            self.update_results(winner)
        elif "Draw" in message:
            self.update_results("Draw")

        # In AI vs AI, auto-clean & restart
        if self.game_mode.get() == "AivAI" and self.aivai_active and not self._pending_mode_change:
            self._after(2000, self.cleanup_board_automatically)


    def toggle_ai_settings(self):
        """
        Enables or disables AI difficulty frames based on game mode.
        """
        mode = self.game_mode.get()
        if mode == "PvAI":
            # Enable AI1, hide AI2
            for child in self.ai_difficulty_frame.winfo_children():
                if isinstance(child, tk.Radiobutton):
                    child.config(state=tk.NORMAL)
            self.ai_difficulty_secondary_frame.grid_remove()
        elif mode == "AivAI":
            # Enable both AI difficulties
            for child in self.ai_difficulty_frame.winfo_children():
                if isinstance(child, tk.Radiobutton):
                    child.config(state=tk.NORMAL)
            for child in self.ai_difficulty_secondary_frame.winfo_children():
                if isinstance(child, tk.Radiobutton):
                    child.config(state=tk.NORMAL)
            self.ai_difficulty_secondary_frame.grid()
        else:
            # Disable & hide AI difficulties
            self.ai_difficulty.set("easy")
            self.ai2_difficulty.set("easy")
            for child in self.ai_difficulty_frame.winfo_children():
                if isinstance(child, tk.Radiobutton):
                    child.config(state=tk.DISABLED)
            for child in self.ai_difficulty_secondary_frame.winfo_children():
                if isinstance(child, tk.Radiobutton):
                    child.config(state=tk.DISABLED)
            self.ai_difficulty_secondary_frame.grid_remove()

    def on_mode_change(self):
        """
        Select a mode. The Start Game button actually starts it.
        """
        mode = self.game_mode.get()
        if self._robot_task_active:
            self._mode_generation += 1
            self._cancel_afters()
            self.aivai_active = False
            self._pending_mode_change = None
            self.toggle_ai_settings()
            self.disable_board_buttons()
            self.update_status(f"{mode} selected. Waiting for current robot move to finish.")
            return

        self._mode_generation += 1
        self._cancel_afters()
        self.aivai_active = False
        self.busy = False
        self.home_pending = False
        self.toggle_ai_settings()

        if mode == "AivAI":
            self.disable_board_buttons()
        elif not self.game_over:
            self.enable_board_buttons()

        self.update_status(f"{mode} selected. Press Start Game.", transient=True)



    def on_o_difficulty_change(self):
        difficulty = self.ai_difficulty.get()
        self.ai_player = AIPlayer(player='O', difficulty=difficulty)
        self.update_status(f"AI O difficulty set to {difficulty}", transient=True)

    def on_x_difficulty_change(self):
        # Only relevant if the game mode is AivAI, but you can decide how to handle it
        difficulty2 = self.ai2_difficulty.get()
        self.ai2_player = AIPlayer(player='X', difficulty=difficulty2)
        self.update_status(f"AI X difficulty set to {difficulty2}", transient=True)
   

    # ----------------------
    #    GAME MANAGEMENT
    # ----------------------

    def reset_game(self):
        """
        Resets the game state (board, UI, AI memo).
        """
        if self._robot_task_active:
            self._queue_mode_change_after_task(self.game_mode.get())
            return

        self._mode_generation += 1
        self._cancel_afters()
        self.aivai_active = False
        self._reset_game_state()

    def cleanup_on_button(self):
        """
        Called when the user presses 'Cleanup Board' button.
        """
        if self._robot_task_active:
            self._queue_mode_change_after_task(self.game_mode.get())
            return
        self._mode_generation += 1
        self._cancel_afters()
        self.aivai_active = False
        self.cleanup_board()

    def cleanup_board_automatically(self):
        """
        Cleans up the board after AI vs AI finishes, then restarts AI vs AI (if still in that mode).
        """
        self.aivai_active = False
        self.cleanup_board(on_complete=self._restart_aivai_after_cleanup)

    def _restart_aivai_after_cleanup(self):
        if self.game_mode.get() != "AivAI" or self._pending_mode_change:
            return

        self.aivai_games_since_home += 1
        if PRE_HOMING is not None and self.aivai_games_since_home >= AI_AUTO_HOME_EVERY_GAMES:
            self.home_pending = True

        if self.home_pending and PRE_HOMING is not None:
            self.home_pending = False
            self._start_robot_task(
                "Homing Dobot...",
                lambda: self.robot_motions.home_safely(PRE_HOMING),
                self._after_safe_home,
            )
            return

        self._after(100, self.start_aivai_game)

    def _after_safe_home(self):
        self.aivai_games_since_home = 0
        if self.game_mode.get() == "AivAI" and not self._pending_mode_change:
            self._after(100, self.start_aivai_game)


    def get_results_text(self):
        return f"X Wins: {self.x_wins} | O Wins: {self.o_wins} | Draws: {self.draws}"

    def update_results(self, winner):
        if winner == "X":
            self.x_wins += 1
        elif winner == "O":
            self.o_wins += 1
        elif winner == "Draw":
            self.draws += 1
        self.results_label.config(text=self.get_results_text())

    def reset_stats(self):
        self.x_wins = 0
        self.o_wins = 0
        self.draws = 0
        self.results_label.config(text=self.get_results_text())
        self.update_status("Player stats cleared.", transient=True)

    def disable_board_buttons(self):
        for row in self.buttons:
            for btn in row:
                btn.config(state=tk.DISABLED)

    def enable_board_buttons(self):
        for row in self.buttons:
            for btn in row:
                btn.config(state=tk.NORMAL)

# ----------------------------------------------------------- #
# 6. MAIN LAUNCH
# ----------------------------------------------------------- #

def gui_main(argv=None):
    parser = argparse.ArgumentParser(description="Robot Tic-Tac-Toe")
    parser.add_argument("--port", default=os.environ.get("DOBOT_PORT", PORT))
    parser.add_argument("--mock", action="store_true", default=os.environ.get("TICTACTOE_MOCK_ROBOT") == "1")
    args = parser.parse_args(argv)

    robot = MockDobot(args.port, vel=200, acc=150) if args.mock else None
    try:
        app = TicTacToeGUI(port=args.port, vel=200, acc=150, robot=robot, mock=args.mock)
    except RuntimeError as exc:
        if args.mock:
            raise
        raise SystemExit(
            f"{exc}\n\n"
            "No hardware / UI test mode:\n"
            "  python .\\tictactoe.py --mock\n\n"
            "Real hardware mode needs pyserial and the correct port, for example:\n"
            "  python .\\tictactoe.py --port COM3"
        )
    app.mainloop()

if __name__ == "__main__":
    gui_main()
